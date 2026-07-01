# MinIO Worker

A generic, **webhook-driven helper container** for the MinIO stack. It receives MinIO
bucket-notification webhooks, enqueues them into a durable queue, and a background worker
drains that queue to do work — with retry, exponential backoff, per-target rate limiting,
coalescing, and dead-lettering.

It is **optional** and disabled by default. Enable it with the Docker Compose `worker`
profile (`COMPOSE_PROFILES=worker`). See the repo root [README](../../README.md) and
[.env.example](../../.env.example).

**Current job:** CDN cache purge for **Cloudflare** and **Bunny CDN** on object
create/delete, so a long CDN edge TTL can be combined with fresh-on-update content. It is
built as a plugin framework so future webhook-driven jobs are additive.

## Architecture

```
 MinIO ──webhook (auth)──▶ POST /webhook ──▶ handler ──▶ durable queue ──▶ worker ──▶ provider(s)
 server                    (Flask/waitress)  (event→jobs) (pending/,dead/)  (drain)    CF / Bunny
```

- **Ingress** (`server.py`): authenticates each request against `WEBHOOK_AUTH_TOKEN`
  (raw `Authorization` value, not `Bearer`), routes to a handler, enqueues, returns `200`
  fast. Returns `4xx` only for auth/parse errors (so MinIO drops poison messages) and
  `503` on enqueue failure (so MinIO's own `queue_dir` retries).
- **Durable queue** (`jobqueue.py`): one JSON file per job under `QUEUE_DIR/pending/`,
  written with `fsync` + atomic rename (crash-safe). Exhausted jobs move to `dead/`.
- **Worker** (`worker.py`): a single daemon thread drains due jobs, coalesces + batches
  per provider, rate-limits, purges, then delete-on-success / backoff-retry / dead-letter.
- **Providers** (`providers/`) and **handlers** (`handlers/`) are the two extension seams.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/webhook` | MinIO events (default source `minio`). |
| `POST` | `/webhook/<source>` | Events for a specific handler source. |
| `GET`  | `/healthz` | Liveness (process up). |
| `GET`  | `/readyz` | Readiness (queue writable + worker alive). |

## Configuration (environment variables)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `WEBHOOK_AUTH_TOKEN` | **yes** | — | Shared secret; MinIO sends it as the raw `Authorization` header. Masked in logs. |
| `PUBLIC_BASE_URL` | yes\* | — | e.g. `https://assets.example.com`. Base for path-style purge URLs. |
| `CF_API_TOKEN` | for CF | — | Cloudflare token, scope **Zone → Cache Purge** only. Masked. |
| `CF_ZONE_ID` | for CF | — | Cloudflare zone id. |
| `BUNNY_API_KEY` | for Bunny | — | Bunny **Account API key** (sent as the `AccessKey` header). Masked. |
| `CF_PUBLIC_BASE_URL` | no | `PUBLIC_BASE_URL` | Per-provider base override (if Cloudflare fronts a different hostname). |
| `BUNNY_PUBLIC_BASE_URL` | no | `PUBLIC_BASE_URL` | Per-provider base override for Bunny. |
| `HOST_MAP_JSON` | no | `""` | `{"bucket":"https://host"}` per-bucket base override. |
| `LISTEN_ADDR` | no | `0.0.0.0:8080` | HTTP bind address. |
| `QUEUE_DIR` | no | `/data/queue` | Durable queue root (mount a volume). |
| `MAX_RETRIES` | no | `10` | Attempts before dead-letter. |
| `RETRY_BASE_SECONDS` | no | `2` | Exponential backoff base. |
| `RETRY_MAX_SECONDS` | no | `300` | Backoff cap. |
| `BATCH_SIZE` | no | `30` | Max URLs per purge call (clamped to each provider's limit). |
| `BATCH_WAIT_MS` | no | `500` | Coalesce dwell window (drain poll interval). |
| `LOG_LEVEL` | no | `INFO` | `rich` log level. |

\* **Provider auto-enable:** Cloudflare turns on when `CF_API_TOKEN` **and** `CF_ZONE_ID`
are set; Bunny when `BUNNY_API_KEY` is set. Both can run simultaneously. The container
**fails fast at startup** if `WEBHOOK_AUTH_TOKEN` is missing, no provider is configured,
or no base URL is available.

## Provider notes

- **Cloudflare** — `POST /zones/{zone}/purge_cache`, `Authorization: Bearer`, `{"files":[…]}`,
  up to **30 URLs/request**. Success requires HTTP 200 and `{"success": true}`.
- **Bunny** — `POST https://api.bunny.net/purge?url=…`, header `AccessKey`, **one URL/call**.

Purge is idempotent, so delivery is at-least-once (a crash between "purged" and "removed"
merely re-purges — harmless).

## Extending

The worker is a plugin framework — new work is additive, no core edits.

**Add a purge target (provider):** create `providers/<name>.py` with a class exposing
`name`, `batch_limit`, `rate_capacity`, `rate_refill`, `enabled()` and
`purge(urls) -> PurgeResult`, plus a `build(cfg, log, http)` factory. The registry
auto-discovers it and enables it when its credentials are present.

**Add an event source (handler):** create `handlers/<name>.py` with a `SOURCE` string and
`handle(payload, ctx) -> list[job]` (pure translation, no I/O). It is reachable at
`POST /webhook/<SOURCE>`. If it introduces a new job `action`, register one executor in
`worker.py`'s `_executors` map.

A job is a plain dict:
`{"action", "path", "bucket", "providers", "attempts", "next_try_ts", "created_ts"}`.

## License

[MIT](../../LICENSE) - BAUER GROUP
