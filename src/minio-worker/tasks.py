"""Huey tasks — the durable queue plus the batched purge worker.

The broker/backend is chosen by ``HUEY_BACKEND`` (``sqlite`` | ``redis``): the same task
code runs on SQLite-in-container now and Redis later (flip the env, add a Redis service,
scale consumer replicas).

Flow:
  - the receiver (main.py) calls ``enqueue_purges(jobs)``, which buffers URLs into a durable
    per-provider outbox and schedules a debounced ``flush`` task;
  - the ``huey_consumer`` runs ``flush(provider)``, which drains due URLs in batches —
    Cloudflare up to ``BATCH_SIZE`` per call, and same-prefix Bunny bursts collapsed into a
    single wildcard purge — with per-provider rate limiting, exponential backoff, and
    dead-lettering.

Batching is what keeps call volume sane at scale (e.g. 100k deletes/h): one Cloudflare
call covers 30 URLs instead of 30 calls, and a Bunny wildcard covers a whole prefix.
"""

import json
import os
import random
import socket
import time

import httpx
from huey import RedisHuey, SqliteHuey, crontab

from config import load_config
from log import setup_logging
from outbox import Outbox
from providers import discover as discover_providers
from ratelimit import TokenBucket

cfg = load_config()
log = setup_logging(cfg.log_level, secrets=cfg.secret_values())

_FLUSH_MAX_BATCHES = 40  # bound work per flush run so tasks stay short


def _make_huey():
    """SQLite in-container by default; Redis when HUEY_BACKEND=redis (results off).

    ``redis`` is bundled in the image, so switching backends is just an env change.
    """
    if cfg.huey_backend == "redis":
        if not cfg.redis_url:
            log.error("configuration error: HUEY_BACKEND=redis requires REDIS_URL")
            raise SystemExit(1)
        return RedisHuey("minio-worker", url=cfg.redis_url, results=False)
    os.makedirs(cfg.queue_dir, exist_ok=True)
    return SqliteHuey(filename=os.path.join(cfg.queue_dir, "huey.db"), results=False)


huey = _make_huey()

# Providers + one rate-limiter each (calls/sec), shared across consumer worker threads.
_http = httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), headers={"User-Agent": "minio-worker"})
_providers = {p.name: p for p in discover_providers(cfg, log, _http)}
_buckets = {name: TokenBucket(p.rate_capacity, p.rate_refill) for name, p in _providers.items()}
_outbox = Outbox(os.path.join(cfg.queue_dir, "outbox.db"))


def enabled_provider_names() -> tuple:
    return tuple(_providers)


def _validate() -> None:
    """Fail fast on misconfiguration — runs in both the receiver and the consumer."""
    problems = []
    if not cfg.webhook_auth_token:
        problems.append("WEBHOOK_AUTH_TOKEN is required")
    if not _providers:
        problems.append("no CDN provider configured (set CF_API_TOKEN+CF_ZONE_ID and/or BUNNY_API_KEY)")
    if not (cfg.public_base_url or cfg.cf_public_base_url or cfg.bunny_public_base_url or cfg.host_map):
        problems.append("PUBLIC_BASE_URL is required (or a per-provider / HOST_MAP_JSON base)")
    if problems:
        for p in problems:
            log.error(f"configuration error: {p}")
        raise SystemExit(1)


_validate()


def _dead_letter(url: str, provider_name: str, detail: str) -> None:
    """Persist an exhausted/non-retryable purge for inspection; never drop silently."""
    dead = os.path.join(cfg.queue_dir, "dead")
    os.makedirs(dead, exist_ok=True)
    item = {"url": url, "provider": provider_name, "detail": detail, "ts": time.time()}
    with open(os.path.join(dead, f"{time.time():015.6f}-{provider_name}.json"), "w", encoding="utf-8") as f:
        json.dump(item, f)
    log.error(f"dead-lettered {url} via {provider_name}: {detail}")


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _build_units(provider_name: str, due: list) -> list:
    """Group due ``(url, attempts)`` into purge units: ``(purge_url, [(url, attempts), ...])``.

    For Bunny, when >= ``BUNNY_WILDCARD_THRESHOLD`` URLs share a directory prefix they are
    collapsed into a single ``prefix/*`` wildcard purge covering all of them.
    """
    if provider_name != "bunny" or cfg.bunny_wildcard_threshold <= 0:
        return [(u, [(u, a)]) for (u, a) in due]

    groups: dict = {}
    for (u, a) in due:
        groups.setdefault(u.rsplit("/", 1)[0], []).append((u, a))
    units = []
    for prefix, items in groups.items():
        if len(items) >= cfg.bunny_wildcard_threshold:
            units.append((f"{prefix}/*", items))
        else:
            units.extend((u, [(u, a)]) for (u, a) in items)
    return units


def enqueue_purges(jobs) -> int:
    """Receiver side: buffer purge URLs per provider and trigger a debounced flush."""
    touched = set()
    added = 0
    for job in jobs:
        for provider_name in job.get("providers", []):
            if provider_name not in _providers:
                continue
            url = f"{cfg.base_for(provider_name, job['bucket'])}/{job['path']}"
            _outbox.add(provider_name, url)
            touched.add(provider_name)
            added += 1
    for provider_name in touched:
        if _outbox.mark_flush(provider_name):
            flush.schedule(args=(provider_name,), delay=cfg.batch_wait_ms / 1000.0)
    return added


@huey.task()
def flush(provider_name: str):
    """Drain due outbox URLs for one provider in batches; reschedule while work remains."""
    provider = _providers.get(provider_name)
    if provider is None:
        return
    _outbox.clear_flush(provider_name)

    now = time.time()
    due = _outbox.due(provider_name, now, cfg.batch_size * _FLUSH_MAX_BATCHES)
    if due:
        units = _build_units(provider_name, due)
        limit = max(1, min(provider.batch_limit, cfg.batch_size))  # CF caps at 30; BATCH_SIZE may lower it
        for unit_batch in _chunks(units, limit):
            purge_urls = [pu for (pu, _cov) in unit_batch]
            covered = [item for (_pu, cov) in unit_batch for item in cov]  # [(url, attempts)]
            _buckets[provider_name].take(1)
            result = provider.purge(purge_urls)

            if result.ok:
                _outbox.delete(provider_name, [u for (u, _a) in covered])
                log.info(f"purged {len(covered)} object(s) via {provider_name} in 1 API call")
                continue

            retry_urls = [u for (u, a) in covered if result.retryable and a + 1 < cfg.max_retries]
            dead = [(u, a) for (u, a) in covered if u not in retry_urls]
            if retry_urls:
                attempt = max(a for (_u, a) in covered)
                delay = min(cfg.retry_max_seconds, cfg.retry_base_seconds * (2 ** attempt))
                delay += random.uniform(0, delay * 0.1)
                _outbox.bump(provider_name, retry_urls, now + delay)
                log.warning(
                    f"purge via {provider_name} failed ({result.detail}); "
                    f"{len(retry_urls)} url(s) retry in {delay:.0f}s"
                )
            for (u, _a) in dead:
                _dead_letter(u, provider_name, result.detail)
            _outbox.delete(provider_name, [u for (u, _a) in dead])

    # Reschedule (single-flight via the flag) while anything remains: due leftovers or backoff.
    if _outbox.has_pending(provider_name) and _outbox.mark_flush(provider_name):
        flush.schedule(args=(provider_name,), delay=cfg.batch_wait_ms / 1000.0)


@huey.periodic_task(crontab(minute="*"))
def heartbeat():
    """Refresh a per-container liveness marker for the consumer's healthcheck.

    Runs only on the consumer (periodic tasks fire in ``huey_consumer``). A stale file
    means the consumer died or hung, which the container healthcheck reports as unhealthy.
    """
    try:
        path = os.path.join(cfg.queue_dir, f"consumer.alive.{socket.gethostname()}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass
