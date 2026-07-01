#!/usr/bin/env python3
"""minio-worker entrypoint — dispatches on WORKER_MODE.

  WORKER_MODE=receiver (default) -> the HTTP webhook receiver (waitress).
  WORKER_MODE=consumer           -> the huey_consumer task runner.

Both roles run from the same image; compose sets WORKER_MODE per service. The built-in
image healthcheck (healthcheck.py) reads the same variable, so no manual compose
healthcheck is needed.
"""

import os
import sys


def _run_consumer():
    workers = os.environ.get("WORKER_CONCURRENCY", "4")
    # Replace this process with the Huey consumer (tini stays PID 1 and reaps it).
    os.execvp("huey_consumer", ["huey_consumer", "tasks.huey", "-w", str(workers), "-k", "thread"])


def _run_receiver() -> int:
    from waitress import serve

    import tasks  # noqa: F401 — import runs config validation + broker/provider init (fail-fast)
    from config import Context
    from handlers import discover as discover_handlers
    from server import create_app

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


def main() -> int:
    mode = os.environ.get("WORKER_MODE", "receiver").strip().lower()
    if mode == "consumer":
        _run_consumer()  # execvp -> replaces the process, does not return
        return 0
    return _run_receiver()


if __name__ == "__main__":
    sys.exit(main())
