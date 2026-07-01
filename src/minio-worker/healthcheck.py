#!/usr/bin/env python3
"""Built-in container healthcheck for both worker roles (no compose config needed).

Reports healthy if EITHER the HTTP endpoint answers (the receiver) OR the consumer's
heartbeat file is fresh (the consumer). The same image ``HEALTHCHECK`` therefore works for
both the ``python main.py`` and ``huey_consumer`` containers — a dead receiver (HTTP down,
no heartbeat) or a hung/dead consumer (stale heartbeat) is reported unhealthy.

stdlib only, so no extra dependency.
"""

import os
import socket
import sys
import time
import urllib.request

_MODE = os.environ.get("WORKER_MODE", "").strip().lower()
_PORT = os.environ.get("LISTEN_ADDR", "0.0.0.0:8080").rpartition(":")[2] or "8080"
_QUEUE_DIR = os.environ.get("QUEUE_DIR", "/data/queue")
_HEARTBEAT_MAX_AGE = 180  # seconds


def _http_ok() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{_PORT}/healthz", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _heartbeat_fresh() -> bool:
    path = os.path.join(_QUEUE_DIR, f"consumer.alive.{socket.gethostname()}")
    try:
        return (time.time() - os.path.getmtime(path)) < _HEARTBEAT_MAX_AGE
    except OSError:
        return False


if __name__ == "__main__":
    if _MODE == "receiver":
        ok = _http_ok()
    elif _MODE == "consumer":
        ok = _heartbeat_fresh()
    else:  # unset -> auto-detect (works for either role)
        ok = _http_ok() or _heartbeat_fresh()
    sys.exit(0 if ok else 1)
