"""Durable coalescing store for batched CDN purges (stdlib sqlite3, WAL).

MinIO delivers one object per webhook, so to batch we buffer purge URLs here (deduped
per provider) and let the flush task drain them in batches — Cloudflare up to 30 URLs
per call, and same-prefix Bunny bursts collapsed into a single wildcard purge.

Retry state (attempts / next_try) lives per row. A per-provider ``flush_flag`` gives
single-flight scheduling: only one flush is queued per provider at a time.

Shared by the receiver (writes URLs) and the consumer (drains them) via the queue
volume — SQLite WAL handles the cross-process access at this write rate.
"""

import os
import sqlite3
import threading


class Outbox:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._local = threading.local()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self._path, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def _init(self) -> None:
        c = self._conn()
        c.execute(
            "CREATE TABLE IF NOT EXISTS outbox("
            "provider TEXT, url TEXT, attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, "
            "PRIMARY KEY(provider, url))"
        )
        c.execute("CREATE INDEX IF NOT EXISTS ix_due ON outbox(provider, next_try)")
        c.execute("CREATE TABLE IF NOT EXISTS flush_flag(provider TEXT PRIMARY KEY, pending INTEGER DEFAULT 0)")
        c.commit()

    def add(self, provider: str, url: str) -> None:
        c = self._conn()
        with self._lock:
            c.execute("INSERT OR IGNORE INTO outbox(provider, url) VALUES(?, ?)", (provider, url))
            c.commit()

    def mark_flush(self, provider: str) -> bool:
        """Atomically claim the flush slot; True if the caller should schedule a flush."""
        c = self._conn()
        with self._lock:
            c.execute("INSERT OR IGNORE INTO flush_flag(provider, pending) VALUES(?, 0)", (provider,))
            cur = c.execute("UPDATE flush_flag SET pending=1 WHERE provider=? AND pending=0", (provider,))
            c.commit()
            return cur.rowcount == 1

    def clear_flush(self, provider: str) -> None:
        c = self._conn()
        with self._lock:
            c.execute("UPDATE flush_flag SET pending=0 WHERE provider=?", (provider,))
            c.commit()

    def due(self, provider: str, now: float, limit: int) -> list[tuple[str, int]]:
        """Return up to ``limit`` ``(url, attempts)`` whose backoff has elapsed."""
        c = self._conn()
        return c.execute(
            "SELECT url, attempts FROM outbox WHERE provider=? AND next_try<=? ORDER BY next_try LIMIT ?",
            (provider, now, limit),
        ).fetchall()

    def delete(self, provider: str, urls: list[str]) -> None:
        if not urls:
            return
        c = self._conn()
        with self._lock:
            c.executemany("DELETE FROM outbox WHERE provider=? AND url=?", [(provider, u) for u in urls])
            c.commit()

    def bump(self, provider: str, urls: list[str], next_try: float) -> None:
        if not urls:
            return
        c = self._conn()
        with self._lock:
            c.executemany(
                "UPDATE outbox SET attempts=attempts+1, next_try=? WHERE provider=? AND url=?",
                [(next_try, provider, u) for u in urls],
            )
            c.commit()

    def has_pending(self, provider: str) -> bool:
        c = self._conn()
        return c.execute("SELECT 1 FROM outbox WHERE provider=? LIMIT 1", (provider,)).fetchone() is not None
