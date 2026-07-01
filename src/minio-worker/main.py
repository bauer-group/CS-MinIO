#!/usr/bin/env python3
"""minio-worker — a generic, webhook-driven helper container for the MinIO stack.

Current job: CDN cache purge (Cloudflare + Bunny) on MinIO object create/delete.
It is a plugin framework — new webhook sources are ``handlers/`` modules and new purge
targets are ``providers/`` modules; both self-register. See README.md.

Startup fails fast on misconfiguration (no auth token, no provider, no base URL) so the
container never boots half-configured.
"""

import sys

import httpx
from waitress import serve

from config import Context, load_config
from handlers import discover as discover_handlers
from jobqueue import Queue
from log import setup_logging
from providers import discover as discover_providers
from server import create_app
from worker import Worker


def main() -> int:
    cfg = load_config()
    log = setup_logging(cfg.log_level, secrets=cfg.secret_values())
    log.info("minio-worker starting")

    http = httpx.Client(
        timeout=httpx.Timeout(15.0, connect=5.0),
        headers={"User-Agent": "minio-worker"},
    )
    providers = discover_providers(cfg, log, http)

    problems = []
    if not cfg.webhook_auth_token:
        problems.append("WEBHOOK_AUTH_TOKEN is required")
    if not providers:
        problems.append(
            "no CDN provider configured (set CF_API_TOKEN+CF_ZONE_ID and/or BUNNY_API_KEY)"
        )
    if not (cfg.public_base_url or cfg.cf_public_base_url
            or cfg.bunny_public_base_url or cfg.host_map):
        problems.append("PUBLIC_BASE_URL is required (or a per-provider / HOST_MAP_JSON base)")
    if problems:
        for p in problems:
            log.error(f"configuration error: {p}")
        return 1

    queue = Queue(cfg.queue_dir)
    if not queue.writable():
        log.error(f"queue dir is not writable: {cfg.queue_dir}")
        return 1

    handlers = discover_handlers(log)
    if not handlers:
        log.error("no handlers registered")
        return 1

    ctx = Context(cfg=cfg, enabled_providers=tuple(p.name for p in providers))

    worker = Worker(cfg, queue, providers, log)
    worker.start()

    app = create_app(
        cfg, ctx, handlers, queue, log,
        ready_check=lambda: queue.writable() and worker.is_alive(),
    )
    log.info(
        f"listening on {cfg.listen_host}:{cfg.listen_port} "
        f"(providers: {', '.join(p.name for p in providers)})"
    )
    serve(app, host=cfg.listen_host, port=cfg.listen_port, threads=8, _quiet=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
