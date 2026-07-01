# Implementation Spec — CDN Purge via MinIO Bucket Notifications

**Audience:** developer implementing this in `Container-Solution/MinIO`.
**Status:** ready to implement.
**Goal:** when an object in MinIO changes, the CDN (Cloudflare) edge cache for that
object's public URL is invalidated automatically — so we can run a **long edge TTL** and
still serve fresh content.

> **Implementation status (delivered).** This spec is implemented with three refinements:
> (1) the relay is a **generic, reusable container named `minio-worker`** (`src/minio-worker/`),
> built as a small **plugin framework** (handlers + providers), not a single-purpose
> `minio-cf-purge`; (2) it purges **both Cloudflare *and* Bunny CDN**, auto-enabling each
> provider when its credentials are present; (3) it is **optional**, gated behind the Docker
> Compose profile **`worker`** (`COMPOSE_PROFILES=worker`). The init task
> `06_notifications.py` is implemented as specified (targets + bucket/event bindings, with
> single-bucket / prefix-suffix filter / all-buckets granularity). See
> [src/minio-worker/README.md](../src/minio-worker/README.md) and
> [src/minio-init/README.md](../src/minio-init/README.md) for the authoritative, as-built
> reference; the sections below capture the original design rationale.

---

## 1. Background & Motivation

The stack serves public S3 objects through Cloudflare (path-style, e.g.
`https://assets.covalida.com/<bucket>/<key>`). To profit from CDN caching we want a **long
edge TTL**, but assets are updated **under the same filename** (no cache-busting URLs). The
only robust way to combine "long TTL" with "fresh on update" is **purge-on-change**.

The **object store is the single choke point** every write passes through (SDK, `mc`,
console, batch imports). Triggering the purge there — rather than in each application — is
therefore the only *reliable* option.

MinIO cannot call Cloudflare directly (it only has generic notification targets and knows
nothing about the Cloudflare API). So we build:

1. A **MinIO webhook target + event bindings**, fully declared in `init.json` (extends the
   existing init container).
2. A **new purge-relay container** that receives MinIO webhooks and calls the Cloudflare
   Purge API (with queuing, retry, and `rich` logging).

```
 write (any client)
        │
        ▼
   ┌─────────┐   s3:ObjectCreated/Removed     ┌──────────────┐   POST /zones/{id}/purge_cache
   │  MinIO  │ ─────────webhook (auth)───────►│ purge-relay  │ ──────────────────────────────► Cloudflare
   │ server  │   (queue_dir: store & forward) │ queue+retry  │      { "files": [ "<url>" ] }
   └─────────┘                                └──────────────┘
```

Two independent reliability layers: MinIO's `queue_dir` (store-and-forward if the relay is
down) **and** the relay's own durable queue + retry (if the Cloudflare API is down).

---

## 2. Scope / Deliverables

| # | Deliverable | Location (suggested) |
|---|-------------|----------------------|
| A | New init task: notifications (target + event bindings) | `src/minio-init/tasks/06_notifications.py` |
| B | `init.json` schema extension + example + README update | `config/`, `src/minio-init/README.md` |
| C | New generic **minio-worker** container (was: purge-relay) | `src/minio-worker/` |
| D | Compose wiring (relay service + volumes + healthcheck) | `docker-compose-*.yml` |
| E | `.env.example` additions + top-level README note | repo root |

---

## 3. Design Decisions (fixed — do not change without discussion)

1. **Everything declarative in `init.json`.** Both the webhook *target* (endpoint, auth
   token, queue dir) and the *event bindings* (which buckets, which events) live in
   `init.json`. One source of truth beats two half-places.
2. **Restart only when a target actually changed.** Registering/altering a `notify_webhook`
   target requires `mc admin service restart`. The init task must detect whether the desired
   target config **differs** from the running config and restart **only then** — never
   unconditionally (the init runs on every container start and must stay non-disruptive).
3. **Event bindings never require a restart** and must be idempotent (check before add).
4. **`"buckets": ["*"]` means all buckets** — resolved dynamically at run time.
5. The relay is a **separate container**, owns queuing/retry/logging, and holds the external
   secrets (Cloudflare token + zone id). MinIO never sees Cloudflare credentials.

---

## 4. Component A — Init Task: `06_notifications.py`

### 4.1 `init.json` schema

Add a new top-level key `notifications`. Each entry defines **one webhook target and its
bucket/event bindings** together (keeps target and events co-located):

```json
{
  "notifications": [
    {
      "id": "cfpurge",
      "type": "webhook",
      "endpoint": "http://minio-cf-purge:8080/webhook",
      "auth_token": "${WEBHOOK_AUTH_TOKEN}",
      "queue_dir": "/data/.minio-events",
      "queue_limit": 100000,
      "buckets": ["*"],
      "events": ["put", "delete"],
      "prefix": "",
      "suffix": ""
    }
  ]
}
```

| Field         | Type          | Default        | Notes |
|---------------|---------------|----------------|-------|
| `id`          | string        | *(required)*   | Target id → ARN `arn:minio:sqs::<id>:webhook`. `[A-Za-z0-9_-]`. |
| `type`        | string        | `"webhook"`    | Only `webhook` for now (leave room for others). |
| `endpoint`    | string        | *(required)*   | Relay URL reachable from the MinIO container. |
| `auth_token`  | string        | `""`           | Shared secret; sent by MinIO as `Authorization` header. Use `${ENV}`. |
| `queue_dir`   | string        | `""`           | Server-side store-and-forward dir (on a MinIO volume). Strongly recommended. |
| `queue_limit` | number        | `100000`       | Max queued events. |
| `buckets`     | string[]      | `["*"]`        | `"*"` = all buckets (resolve at run time). |
| `events`      | string[]      | `["put","delete"]` | `mc event` names: `put`, `delete`, `get`, `replica`, `ilm`, … |
| `prefix`      | string        | `""`           | Optional object-key prefix filter. |
| `suffix`      | string        | `""`           | Optional object-key suffix filter. |

> `${ENV}` resolution already happens centrally in `main.py` (`resolve_config_values`), so
> `auth_token` supports secrets out of the box — no extra work in the task.

### 4.2 Task module contract

Follow the existing plugin pattern (see `01_buckets.py`):

```python
TASK_NAME = "Notifications"
TASK_DESCRIPTION = "Configure bucket notification targets and event bindings"
CONFIG_KEY = "notifications"

def run(items: list, console, **kwargs) -> dict:
    ...
    return {"changed": bool, "message": str}   # or {"skipped": True, "message": ...}
```

### 4.3 Behaviour (algorithm)

For the whole `notifications` list:

**Phase 1 — Targets (may require restart):**
1. For each entry, build the desired key/values for `notify_webhook:<id>`
   (`endpoint`, `auth_token`, `queue_dir`, `queue_limit`, and `enable=on`).
2. Read the current config: `mc admin config get <alias> notify_webhook:<id>`
   (parse the `--json` output; treat "not set" as empty).
3. **Compare** desired vs current (normalise: ignore unset/default fields, string-compare
   values). If **identical → do nothing** for this target.
4. If **different** → `mc admin config set <alias> notify_webhook:<id> endpoint=... auth_token=... queue_dir=... queue_limit=...`
   and set a module-level `restart_required = True`.
5. After all entries: **if `restart_required`** → `mc admin service restart <alias>`, then
   **wait for the server to become healthy again** (reuse the health-check loop pattern from
   `main.py:wait_for_minio`, poll `/minio/health/live`, timeout from `MINIO_WAIT_TIMEOUT`).
   If not required → skip restart entirely.

**Phase 2 — Event bindings (no restart, idempotent):**
6. Resolve buckets: if `buckets == ["*"]`, list all buckets via `mc ls --json <alias>`
   (bucket name = `key` field, strip trailing `/`). Otherwise use the explicit list.
7. For each bucket, read existing bindings: `mc event ls --json <alias>/<bucket>`.
8. Build the ARN `arn:minio:sqs::<id>:webhook`. If a binding for that ARN with the **same
   events/prefix/suffix** already exists → skip. Otherwise
   `mc event add <alias>/<bucket> <arn> --event <csv-events> [--prefix p] [--suffix s]`.
9. (Optional, keep simple) do **not** remove bindings that are not in the config — additive
   only, like lifecycle rules today.

**Return:** `{"changed": <any set/add happened>, "message": "<n> target(s), <m> binding(s), restart=<yes/no>"}`.

### 4.4 Edge cases & guardrails

- **Target ARN must exist before `mc event add`.** Because we set the target and restart in
  Phase 1 *before* Phase 2, the ARN is active when we bind. If `restart_required` was false
  because the target already existed and is unchanged, the ARN is already active — fine.
- If `mc event add` fails with "already exists" despite the `event ls` check (race / different
  formatting), treat it as **non-fatal** (log dim, count as unchanged).
- Validate `id` characters; reject/skip invalid ids with a clear warning.
- If `endpoint` is empty → skip entry with a warning (misconfiguration).
- Keep all output in the existing `rich` style (`[green]+`, `[dim]=`, `[yellow]Warning`).

### 4.5 Idempotency test

Running the init container **twice** with unchanged config must print **no changes** in
Phase 1 and **no restart** (critical — otherwise every deploy restarts MinIO).

---

## 5. Component C — Worker Container (`src/minio-worker`, implemented as `minio-worker` + Bunny)

A small, long-running HTTP service. Runtime style consistent with the init container:
**Python 3.14 (alpine), `rich` logging, non-root, `tini`.**

### 5.1 Responsibilities

1. Expose `POST /webhook` — receive MinIO event payloads.
2. **Authenticate** each request against a shared secret.
3. Parse events → derive the public URL(s) of changed objects.
4. **Enqueue** purge work durably (survives restart).
5. A worker drains the queue and calls the **Cloudflare Purge API** with **retry + backoff**;
   dead-letters after N attempts.
6. Expose `GET /healthz` and `GET /readyz`.
7. Structured, readable logs via `rich`.

### 5.2 Configuration (environment variables)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CF_API_TOKEN` | yes | — | Cloudflare token, scope **Zone → Cache Purge only** (least privilege). Secret. |
| `CF_ZONE_ID` | yes | — | Zone id of the public domain. |
| `PUBLIC_BASE_URL` | yes | — | e.g. `https://assets.covalida.com`. Used to build purge URLs. |
| `WEBHOOK_AUTH_TOKEN` | yes | — | Shared secret; must equal the MinIO target `auth_token`. |
| `LISTEN_ADDR` | no | `0.0.0.0:8080` | Bind address. |
| `QUEUE_DIR` | no | `/data/queue` | Durable queue dir (mount a volume). |
| `MAX_RETRIES` | no | `10` | Attempts before dead-letter. |
| `RETRY_BASE_SECONDS` | no | `2` | Exponential backoff base. |
| `RETRY_MAX_SECONDS` | no | `300` | Backoff cap. |
| `BATCH_SIZE` | no | `30` | Purge URLs per API call (Cloudflare allows ≤ 30 files/request on non-Enterprise). |
| `BATCH_WAIT_MS` | no | `500` | Coalesce window before flushing a batch. |
| `LOG_LEVEL` | no | `INFO` | `rich`-formatted logging. |
| `HOST_MAP_JSON` | no | `""` | Optional JSON `{ "bucket": "https://host" }` to override `PUBLIC_BASE_URL` per bucket (see 5.5). |

> The relay holds Cloudflare credentials; **MinIO and the init container never do.**

### 5.3 Webhook contract (MinIO → relay)

- MinIO sends `Authorization: <auth_token>` (raw value, not `Bearer`). Reject with `401` if
  it does not match `WEBHOOK_AUTH_TOKEN`.
- Body is JSON (single event or `{"Records":[...]}`). Relevant fields per record:
  - `eventName` — e.g. `s3:ObjectCreated:Put`, `s3:ObjectRemoved:Delete`.
  - `s3.bucket.name` — bucket.
  - `s3.object.key` — **URL-encoded** object key (decode it, e.g. `%20`, `+`).
- Respond **`200` immediately after enqueue** (do not call Cloudflare inline — keep the
  webhook fast so MinIO's queue doesn't back up). Return `4xx` only for auth/parse errors so
  MinIO retries transient `5xx`.

### 5.4 Purge URL construction

```
url = f"{base}/{bucket}/{quote(object_key)}"
```
where `base = HOST_MAP_JSON.get(bucket) or PUBLIC_BASE_URL`. Path-style, matches how
Cloudflare cached it. Purge both on create **and** delete (delete stops the edge from
serving a stale/removed object).

### 5.5 Multiple hostnames / buckets

Everything currently runs under one host (`assets.covalida.com`), so `PUBLIC_BASE_URL` is
enough. `HOST_MAP_JSON` is the escape hatch if some buckets are later served under a
different hostname — keep it optional.

### 5.6 Cloudflare Purge API

```
POST https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/purge_cache
Authorization: Bearer {CF_API_TOKEN}
Content-Type: application/json

{ "files": ["https://assets.covalida.com/iam/logo.png", ...] }   # ≤ 30 per request
```
- On `2xx` + `{"success": true}` → mark batch done (delete from queue).
- On `429`/`5xx`/network error → retry with exponential backoff (`RETRY_BASE_SECONDS` …
  `RETRY_MAX_SECONDS`). After `MAX_RETRIES` → move to `dead-letter/` and log an error.
- Respect Cloudflare rate limits (purge-by-URL has per-plan daily/second caps). Batching +
  coalescing (5.2) keeps call volume low.

### 5.7 Durable queue (suggested implementation)

Keep it dependency-light and crash-safe:
- `QUEUE_DIR/pending/` — one JSON file per purge item (`{url, attempts, next_try_ts}`),
  filename = monotonic counter or hash (dedupe identical URLs within a short window).
- `QUEUE_DIR/dead/` — items that exhausted retries.
- A single worker thread: scan `pending/`, pick due items, coalesce up to `BATCH_SIZE`,
  purge, delete-on-success / bump-attempts-on-failure. `fsync` on write for durability.
- Alternative: SQLite (`WAL`) if the dev prefers transactional semantics. Either is fine;
  no external broker.

Purge is **idempotent** (purging the same URL twice is harmless) → at-least-once delivery is
acceptable; do not over-engineer exactly-once.

### 5.8 Container

- Base `python:3.14-alpine`, `pip install rich` + one HTTP lib (Flask **or** FastAPI+uvicorn
  — dev's choice; stdlib `http.server` is acceptable but Flask is cleaner) + `requests`
  (or `httpx`).
- Non-root user (uid 1000), `tini` as entrypoint (match init container Dockerfile).
- `EXPOSE 8080`. `HEALTHCHECK` hitting `/healthz`.
- OCI labels consistent with the other images.

### 5.9 Security

- CF token scoped to **Zone → Cache Purge** only (nothing else) — safe even on a shared zone.
- Shared `WEBHOOK_AUTH_TOKEN` between MinIO target and relay; reject unauthenticated posts.
- Relay only needs egress to `api.cloudflare.com` and ingress from the MinIO container.
- Never log the CF token or the auth token (mask in logs).

---

## 6. Component D — Compose Wiring

Add to the compose files (and consumers inherit the pattern):

```yaml
  minio-cf-purge:
    image: ghcr.io/bauer-group/cs-minio/minio-cf-purge:${CF_PURGE_VERSION:-latest}
    restart: unless-stopped
    environment:
      - CF_API_TOKEN=${CF_PURGE_API_TOKEN}
      - CF_ZONE_ID=${CF_ZONE_ID}
      - PUBLIC_BASE_URL=${S3_PUBLIC_BASE_URL}         # e.g. https://assets.covalida.com
      - WEBHOOK_AUTH_TOKEN=${WEBHOOK_AUTH_TOKEN}
      - QUEUE_DIR=/data/queue
    volumes:
      - cf-purge-queue:/data/queue
    networks: [ local ]

  # minio-server: mount a volume for the notification queue_dir referenced in init.json
  #   volumes:
  #     - minio-events:/data/.minio-events
```

- The init container's `notifications[].endpoint` points at `http://minio-cf-purge:8080/webhook`.
- `notifications[].queue_dir` must be on a **MinIO** volume (server-side store-and-forward).
- No Cloudflare env on `minio-server` or `minio-init`.

---

## 7. Component E — `.env.example` additions

```env
# Cloudflare CDN purge relay (minio-cf-purge)
CF_PURGE_API_TOKEN=            # Cloudflare token, scope: Zone > Cache Purge (least privilege)
CF_ZONE_ID=                    # Zone id of the public domain
S3_PUBLIC_BASE_URL=https://assets.example.com
WEBHOOK_AUTH_TOKEN=            # shared secret MinIO <-> relay (openssl rand -hex 32)
CF_PURGE_VERSION=latest
```

---

## 8. Acceptance Criteria

1. **Idempotent init:** run init twice, unchanged config → **no restart**, no target/binding
   changes reported.
2. **Target change triggers exactly one restart:** change `endpoint`/`auth_token` → init sets
   config and restarts once, then binds events.
3. **All-buckets binding:** with `"buckets": ["*"]`, every existing bucket has the ARN bound
   after init (verify `mc event ls`).
4. **End-to-end purge:** upload/overwrite an object under a cached path → within seconds the
   relay logs a successful purge and a subsequent `curl -I` shows `Cf-Cache-Status: MISS`
   then `HIT` (fresh object).
5. **Relay durability:** stop the relay, upload objects (MinIO `queue_dir` buffers), start the
   relay → buffered events are delivered and purged.
6. **CF outage handling:** simulate Cloudflare `5xx` → relay retries with backoff, eventually
   succeeds; exhausted items land in `dead/` and are logged, not lost silently.
7. **Auth:** POST to `/webhook` without/with wrong token → `401`, no purge.
8. **No secret leakage:** CF token / auth token never appear in logs.

---

## 9. Test Plan (manual)

```bash
# 1. Bind check
mc event ls localminio/iam

# 2. E2E
mc cp ./logo.png localminio/iam/logo.png            # triggers put
docker logs minio-cf-purge                            # -> "purged https://assets.../iam/logo.png"
curl -sI https://assets.covalida.com/iam/logo.png | grep -i cf-cache-status   # MISS then HIT

# 3. Delete
mc rm localminio/iam/logo.png                         # triggers delete -> purge

# 4. Relay down buffering
docker stop minio-cf-purge && mc cp ./a.png localminio/iam/a.png
docker start minio-cf-purge                           # buffered event delivered
```

---

## 10. Out of Scope (for this iteration)

- Removing stale event bindings not present in config (additive only, like lifecycle rules).
- Non-webhook notification targets (Kafka/NATS/…): schema leaves room via `type`, but not
  implemented now.
- Per-object cache-tag purging / purge-by-tag (URL purge is sufficient here).
- Browser-cache invalidation — impossible via purge; browser TTL stays short by design.

---

## 11. Notes for the CDN side (context, not this repo)

- Cloudflare only caches `Vary: Accept-Encoding`; MinIO sends `Vary: Origin`. With a **fixed
  Edge TTL (Override)** cache rule, Cloudflare caches anyway — no header edit needed (and
  `Vary` cannot be modified via Transform Rules).
- With this purge relay in place, the **edge TTL can be long** (hours/day) while the **browser
  TTL stays short** (~1 min) — purge invalidates the edge, not browsers.
