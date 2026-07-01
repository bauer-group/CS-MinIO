#!/usr/bin/env python3
"""minio-worker receiver — the HTTP entrypoint of the worker.

Importing ``tasks`` builds the Huey broker + providers and validates the config
(fail-fast). This process only receives webhooks and enqueues purge tasks; the
``huey_consumer`` process (same image, different command) executes them.

It is a plugin framework — new webhook sources are ``handlers/`` modules and new purge
targets are ``providers/`` modules; both self-register. See README.md.
"""

import sys

from waitress import serve

import tasks  # noqa: F401 — import runs config validation + broker/provider init (fail-fast)
from config import Context
from handlers import discover as discover_handlers
from server import create_app


def main() -> int:
    cfg, log = tasks.cfg, tasks.log
    log.info("minio-worker receiver starting")

    handlers = discover_handlers(log)
    if not handlers:
        log.error("no handlers registered")
        return 1

    ctx = Context(cfg=cfg, enabled_providers=tasks.enabled_provider_names())
    app = create_app(cfg, ctx, handlers, log)
    log.info(
        f"listening on {cfg.listen_host}:{cfg.listen_port} "
        f"(backend: {cfg.huey_backend}, providers: {', '.join(ctx.enabled_providers)})"
    )
    serve(app, host=cfg.listen_host, port=cfg.listen_port, threads=8, _quiet=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
