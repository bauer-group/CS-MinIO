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
    # Launch Huey in-process rather than execvp'ing the `huey_consumer` script.
    # That script calls ConsumerConfig.setup_logger(), which unconditionally attaches a
    # SECOND, unfiltered StreamHandler to the "huey" logger — bypassing the redaction +
    # heartbeat filters on our root RichHandler and double-printing every framework line.
    # By running the consumer ourselves and skipping setup_logger, huey's records simply
    # propagate to the root handler (single format, filters applied). The consumer installs
    # its own SIGTERM/SIGINT handlers, so container stop still shuts down gracefully.
    from huey.consumer_options import ConsumerConfig

    import tasks  # noqa: F401 — installs root logging (filters) + validates config (fail-fast)

    workers = int(os.environ.get("WORKER_CONCURRENCY", "4"))
    config = ConsumerConfig(workers=workers, worker_type="thread")
    config.validate()
    tasks.huey.create_consumer(**config.values).run()


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
        _run_consumer()  # blocks until the consumer shuts down (SIGTERM/SIGINT)
        return 0
    return _run_receiver()


if __name__ == "__main__":
    sys.exit(main())
