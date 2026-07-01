"""Huey task definitions — the durable queue and the async purge worker.

The broker/backend is chosen by ``HUEY_BACKEND`` (``sqlite`` | ``redis``) so the exact
same task code runs on SQLite-in-container now and Redis later (flip the env, add a
Redis service, scale consumer replicas).

Split of responsibilities:
  - the receiver process (main.py) imports this module and enqueues ``purge(...)`` tasks;
  - the ``huey_consumer`` process imports this module and executes them.

Retry strategy: one task per (object, provider). On a retryable failure the task
re-enqueues itself with an incremented ``attempt`` and an exponential ``delay`` via
``purge.schedule(...)`` — explicit and bounded, independent of Huey's internal retry
counter. Non-retryable failures (or exhausted attempts) are dead-lettered.
"""

import json
import os
import random
import time

import httpx
from huey import RedisHuey, SqliteHuey

from config import load_config
from log import setup_logging
from providers import discover as discover_providers
from ratelimit import TokenBucket

cfg = load_config()
log = setup_logging(cfg.log_level, secrets=cfg.secret_values())


def _make_huey():
    """SQLite in-container by default; Redis when HUEY_BACKEND=redis (results off)."""
    if cfg.huey_backend == "redis":
        if not cfg.redis_url:
            log.error("configuration error: HUEY_BACKEND=redis requires REDIS_URL")
            raise SystemExit(1)
        return RedisHuey("minio-worker", url=cfg.redis_url, results=False)
    os.makedirs(cfg.queue_dir, exist_ok=True)
    return SqliteHuey(filename=os.path.join(cfg.queue_dir, "huey.db"), results=False)


huey = _make_huey()

# Providers + one rate-limiter each, shared across the consumer's worker threads.
_http = httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), headers={"User-Agent": "minio-worker"})
_providers = {p.name: p for p in discover_providers(cfg, log, _http)}
_buckets = {name: TokenBucket(p.rate_capacity, p.rate_refill) for name, p in _providers.items()}


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


def _dead_letter(path: str, bucket: str, provider_name: str, detail: str) -> None:
    """Persist an exhausted/non-retryable purge for inspection; never drop silently."""
    dead = os.path.join(cfg.queue_dir, "dead")
    os.makedirs(dead, exist_ok=True)
    item = {"path": path, "bucket": bucket, "provider": provider_name, "detail": detail, "ts": time.time()}
    with open(os.path.join(dead, f"{time.time():015.6f}-{provider_name}.json"), "w", encoding="utf-8") as f:
        json.dump(item, f)
    log.error(f"dead-lettered {bucket} object via {provider_name} after {cfg.max_retries} attempt(s): {detail}")


@huey.task()
def purge(path: str, bucket: str, provider_name: str, attempt: int = 0):
    """Purge one object URL from one provider; re-enqueue with backoff on transient failure."""
    provider = _providers.get(provider_name)
    if provider is None:
        log.warning(f"provider '{provider_name}' not enabled; dropping {bucket} object")
        return

    url = f"{cfg.base_for(provider_name, bucket)}/{path}"
    _buckets[provider_name].take(1)
    result = provider.purge([url])

    if result.ok:
        log.info(f"purged {url} via {provider_name}")
        return

    if result.retryable and attempt < cfg.max_retries:
        delay = min(cfg.retry_max_seconds, cfg.retry_base_seconds * (2 ** attempt))
        delay += random.uniform(0, delay * 0.1)  # jitter
        log.warning(
            f"purge {url} via {provider_name} failed ({result.detail}); "
            f"retry {attempt + 1}/{cfg.max_retries} in {delay:.0f}s"
        )
        purge.schedule(args=(path, bucket, provider_name, attempt + 1), delay=delay)
        return

    _dead_letter(path, bucket, provider_name, result.detail)
