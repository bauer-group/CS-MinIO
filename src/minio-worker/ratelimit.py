"""A tiny thread-safe token bucket — one instance per CDN provider.

The worker calls ``take(n)`` before each purge call so we stay under each provider's
rate limit proactively (Bunny publishes no hard number, so defaults are conservative).
Reactive backoff on ``429`` remains the safety net in the worker loop.
"""

import threading
import time


class TokenBucket:
    def __init__(self, capacity: float, refill_per_sec: float):
        self._capacity = float(capacity)
        self._refill = float(refill_per_sec)
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self, n: int = 1) -> None:
        """Block until ``n`` tokens are available, then consume them."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._refill
                )
                self._updated = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait = deficit / self._refill if self._refill > 0 else 0.1
            time.sleep(min(wait, 1.0))
