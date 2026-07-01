# MinIO Worker

A generic, **webhook-driven helper** for the MinIO stack. It receives MinIO
bucket-notification webhooks, enqueues them into a **durable task queue** ([Huey](https://huey.readthedocs.io/)),
and a consumer drains that queue — with retry, exponential backoff, per-target rate
limiting, and dead-lettering.

It runs as **two services from one image**, selected by the `WORKER_MODE` env:

- **`minio-worker`** (`WORKER_MODE=receiver`) — the HTTP **receiver** (Flask/waitress): authenticate → translate → enqueue. Fast and independently scalable; it does no purging itself.
- **`minio-worker-consumer`** (`WORKER_MODE=consumer`) — the **`huey_consumer`** process that executes the queued tasks.

The entrypoint dispatches on `WORKER_MODE`, and a **built-in image healthcheck** uses it too — so compose needs no manual `healthcheck` for either service.

The queue backend is chosen by `HUEY_BACKEND`: **`sqlite`** (self-contained, in-container)
now, or **`redis`** later for scale-out — the same task code runs on both.

It is **optional** and disabled by default. Enable it with the Docker Compose `worker`
profile (`COMPOSE_PROFILES=worker`). See the repo root [README](../../README.md) and
[.env.example](../../.env.example).

**Current job:** CDN cache purge for **Cloudflare** and **Bunny CDN** on object
create/delete, so a long CDN edge TTL can be combined with fresh-on-update content. It is
a plugin framework so future webhook-driven jobs are additive.

## Quick start

The worker is off by default. To enable CDN cache purge for the stack:

1. In `.env`, activate the profile and set the required variables:

   ```env
   COMPOSE_PROFILES=worker
   WEBHOOK_AUTH_TOKEN=<openssl rand -hex 32>
   S3_PUBLIC_BASE_URL=https://assets.example.com
   # A provider auto-enables when its credentials are set (Cloudflare and/or Bunny):
   CF_PURGE_API_TOKEN=...
   CF_ZONE_ID=...
   BUNNY_API_KEY=...
   ```

2. Add a `notifications` block to your init config so MinIO forwards object events to the
   worker — see [`config/minio-init.example.json`](../../config/minio-init.example.json)
   and [the init README](../minio-init/README.md#bucket-notifications).

3. Start the stack as usual; the `minio-worker` receiver and `minio-worker-consumer` start with the `worker` profile active.

## Architecture

```
 MinIO ─webhook─▶ receiver ─buffer─▶ outbox ─flush─▶ consumer ─batched─▶ provider(s)
 server           Flask/waitress    SQLite          huey_consumer        CF / Bunny
```

- **Receiver** (`server.py` + `main.py`): authenticates each request against
  `WEBHOOK_AUTH_TOKEN` (raw `Authorization`, not `Bearer`), routes to a handler, **buffers**
  the changed-object URLs into a durable per-provider outbox, and schedules a debounced
  `flush`. Returns `200` fast; `4xx` only for auth/parse errors (so MinIO drops poison
  messages) and `503` on enqueue failure (so MinIO's own `queue_dir` retries).
- **Outbox** (`outbox.py`): a small SQLite table that coalesces and de-duplicates URLs and
  holds per-URL retry backoff. Huey (`QUEUE_DIR/huey.db`, WAL, or Redis) carries the `flush`
  tasks between the two processes.
- **Consumer** (`huey_consumer tasks.huey`): runs `flush(provider)` — drains due URLs in
  **batches** (Cloudflare up to `BATCH_SIZE` per call; same-prefix Bunny bursts collapsed
  into a single wildcard purge), rate-limited per provider, with exponential backoff and
  dead-lettering. Batching keeps call volume sane at scale (one Cloudflare call covers 30
  URLs instead of 30 calls).
- **Providers** (`providers/`) and **handlers** (`handlers/`) are the two extension seams.

## Endpoints (receiver)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/webhook` | MinIO events (default source `minio`). |
| `POST` | `/webhook/<source>` | Events for a specific handler source. |
| `GET`  | `/healthz` | Liveness. |
| `GET`  | `/readyz` | Readiness. |

## Configuration (environment variables)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `WEBHOOK_AUTH_TOKEN` | **yes** | — | Shared secret; MinIO sends it as the raw `Authorization` header. Masked in logs. |
| `PUBLIC_BASE_URL` | yes\* | — | e.g. `https://assets.example.com`. Base for path-style purge URLs. |
| `CF_API_TOKEN` | for CF | — | Cloudflare token, scope **Zone → Cache Purge** only. Masked. |
| `CF_ZONE_ID` | for CF | — | Cloudflare zone id. |
| `BUNNY_API_KEY` | for Bunny | — | Bunny **Account API key** (sent as the `AccessKey` header). Masked. |
| `CF_PUBLIC_BASE_URL` / `BUNNY_PUBLIC_BASE_URL` | no | `PUBLIC_BASE_URL` | Per-provider base override (different hostnames per CDN). |
| `HOST_MAP_JSON` | no | `""` | `{"bucket":"https://host"}` per-bucket base override. |
| `LISTEN_ADDR` | no | `0.0.0.0:8080` | Receiver HTTP bind address. |
| `QUEUE_DIR` | no | `/data/queue` | Holds the SQLite queue DB and dead-letters (mount a volume). |
| `HUEY_BACKEND` | no | `sqlite` | `sqlite` (in-container) or `redis` (scale-out). |
| `REDIS_URL` | if redis | — | e.g. `redis://redis:6379/0`. |
| `WORKER_MODE` | no | `receiver` | `receiver` or `consumer` — selects the role (compose sets it per service). |
| `WORKER_CONCURRENCY` | no | `4` | Consumer worker threads (the entrypoint passes this to `huey_consumer -w`). |
| `MAX_RETRIES` | no | `10` | Attempts before dead-letter. |
| `RETRY_BASE_SECONDS` / `RETRY_MAX_SECONDS` | no | `2` / `300` | Exponential backoff base / cap. |
| `BATCH_SIZE` | no | `30` | Max URLs per Cloudflare purge call. |
| `BATCH_WAIT_MS` | no | `500` | Coalesce window before a flush (higher = more batching). |
| `BUNNY_WILDCARD_THRESHOLD` | no | `0` | Collapse ≥N same-prefix Bunny purges into one wildcard call (0 = off). |
| `LOG_LEVEL` | no | `INFO` | `rich` log level. |

\* **Provider auto-enable:** Cloudflare turns on when `CF_API_TOKEN` **and** `CF_ZONE_ID`
are set; Bunny when `BUNNY_API_KEY` is set. Both can run simultaneously. Both processes
**fail fast at startup** if `WEBHOOK_AUTH_TOKEN` is missing, no provider is configured, or
no base URL is available.

## Provider notes

- **Cloudflare** — `POST /zones/{zone}/purge_cache`, `Authorization: Bearer`, `{"files":[…]}`,
  **batched up to 30 URLs per call**. Success requires HTTP 200 and `{"success": true}`.
- **Bunny** — `POST https://api.bunny.net/purge?url=…`, header `AccessKey`, one URL per call;
  with `BUNNY_WILDCARD_THRESHOLD` set, same-prefix bursts collapse into one `…/prefix/*` call
  (which purges the whole prefix at the edge — may evict unchanged objects too).

Purge is idempotent, so delivery is at-least-once (a crash between "purged" and "acked"
merely re-purges — harmless).

## Provider setup (get your credentials)

The worker only *calls* the purge APIs — the CDN itself must serve your objects, and you
must create the credentials below. The URL it purges is `{PUBLIC_BASE_URL}/{bucket}/{key}`
(path-style), so the CDN has to front MinIO at that hostname.

### Cloudflare

1. **Serve MinIO through Cloudflare** — point the public hostname (e.g. `assets.example.com`)
   at your MinIO origin with the DNS record **proxied** (orange cloud). Under **Caching → Cache
   Rules**, add a rule that **overrides both TTLs**:
   - **Edge TTL → Override to** a **long** value, e.g. **1 day** — safe because the worker
     purges the edge on every change;
   - **Browser TTL → Override to** a **short** value, **≤ 5 minutes** — browsers can't be
     purged, so they must expire on their own.
2. **API token** → `CF_PURGE_API_TOKEN` (least privilege): **My Profile → API Tokens → Create
   Token**; use the **"Purge Cache"** template, or a custom token with **Permissions: Zone ·
   Cache Purge · Purge** and **Zone Resources · Include · your zone**. Copy it (shown once).
3. **Zone ID** → `CF_ZONE_ID`: open the domain in the dashboard → **Overview** → scroll to the
   **API** section (bottom of the right sidebar) → copy the **Zone ID**.

### Bunny

1. **Serve MinIO through a Bunny Pull Zone** — create a Pull Zone whose Origin is your MinIO
   endpoint, and use its hostname as `S3_PUBLIC_BASE_URL` (or the per-provider
   `BUNNY_PUBLIC_BASE_URL`). Set a **long edge Cache Expiration** (e.g. 1 day) and keep the
   **browser** `Cache-Control` **max-age ≤ 5 minutes** (from the object headers, or override it
   in the Pull Zone).
2. **Account API key** → `BUNNY_API_KEY`: dashboard → **profile menu (top-right) → Account
   Settings → API Key** (`https://dash.bunny.net/account/api-key`). This is the **account-level**
   key sent as the `AccessKey` header — the per-URL purge endpoint needs it, not a pull-zone key.
3. **Wildcard purge** (for `BUNNY_WILDCARD_THRESHOLD`): Bunny's URL purge supports `…/prefix/*`
   with no extra setup.

### TTL strategy

Run a **long edge TTL** (e.g. **1 day**) so almost every request is served from cache, and a
**short browser TTL** (**≤ 5 minutes**) so viewers recover quickly. This is safe *only because*
the worker purges: a change invalidates the **edge instantly**, and the browser TTL is the only
staleness a viewer can still see — bounded to a few minutes. **Browsers cannot be purged, so
never give them a long TTL.**

| Layer | TTL | Why |
| ----- | --- | --- |
| CDN edge | **long** (e.g. 1 day) | Purged on every object change → always fresh. |
| Browser | **short** (≤ 5 min) | Can't be purged → must expire on its own. |

## Scaling

- **SQLite (default)** keeps everything self-contained: the receiver and consumer share the
  queue DB on the `minio-worker-queue` volume. Tune throughput with `WORKER_CONCURRENCY`.
- **Redis** unlocks horizontal scale-out: set `HUEY_BACKEND=redis` + `REDIS_URL`, add a
  Redis service, then run multiple consumer replicas
  (`docker compose --profile worker up -d --scale minio-worker-consumer=N`). `redis` is
  bundled in the image, so no rebuild is needed — just the env change.
- At very high volume the binding constraint is the **CDN API rate**, not the queue.
  **Batching** (Cloudflare ≤30 URLs/call, Bunny wildcard collapse) keeps call volume low, and
  the per-provider token buckets self-throttle so a backlog is held durably rather than
  getting the account throttled. Tune the coalesce window with `BATCH_WAIT_MS`.

## Extending

The worker is a plugin framework — new work is additive.

**Add a purge target (provider):** create `providers/<name>.py` with a class exposing
`name`, `batch_limit`, `rate_capacity`, `rate_refill`, `enabled()` and
`purge(urls) -> PurgeResult`, plus a `build(cfg, log, http)` factory. The registry
auto-discovers it and enables it when its credentials are present.

**Add an event source (handler):** create `handlers/<name>.py` with a `SOURCE` string and
`handle(payload, ctx) -> list[job]` (pure translation, no I/O). It is reachable at
`POST /webhook/<SOURCE>`.

**Add a new job type:** define another `@huey.task` in `tasks.py` and enqueue it from the
receiver.

## Operations & troubleshooting

- **Two services:** `<STACK>_WORKER` (receiver) and `<STACK>_WORKER_CONSUMER` (consumer).
- **Health:** a **built-in image healthcheck** (`healthcheck.py`) covers both roles via
  `WORKER_MODE` — the receiver's `GET /healthz`, or the consumer's per-minute heartbeat file
  (`QUEUE_DIR/consumer.alive.<host>`). No compose healthcheck is needed; a dead receiver or a
  hung/dead consumer is reported unhealthy. Manual check:
  `docker exec <STACK>_WORKER curl -sf http://localhost:8080/healthz`.
- **Confirm a purge:** change an object, then look for `purged N object(s) via <provider>` in
  the **consumer** logs; a following `curl -I <public-url>` should show fresh content.
- **Queue / dead-letters:** Huey uses `QUEUE_DIR/huey.db` and the coalescing buffer is
  `QUEUE_DIR/outbox.db`; exhausted or non-retryable purges are written to `QUEUE_DIR/dead/*.json`
  and logged at ERROR — never dropped silently. Inspect with
  `docker exec <STACK>_WORKER_CONSUMER ls -la /data/queue/dead`.
- **Common issues:**
  - *A service exits immediately* — a required variable is missing; the startup log names it.
  - *Every webhook returns 401* — the MinIO target `auth_token` and the worker's
    `WEBHOOK_AUTH_TOKEN` differ.
  - *Purges return 401/403* — wrong/insufficient CDN credentials (Cloudflare needs
    **Zone → Cache Purge**; Bunny needs the **Account API key**).
  - *Events enqueue but nothing purges* — the **consumer** isn't running, or it has no egress
    to the CDN API.

## License

[MIT](../../LICENSE) - BAUER GROUP
