"""Durable, crash-safe filesystem job queue.

Design (deliberately dependency-light — no DB, no broker):
  QUEUE_DIR/pending/<created_ts>-<seq>-<hash>.json   one job per file
  QUEUE_DIR/dead/<same-name>.json                    exhausted retries (kept for inspection)

Durability: write to a temp file, ``flush`` + ``fsync``, then atomic ``os.replace``
into place — an interrupted write never leaves a half-item. Delete-on-success.

A job is a plain dict (kept JSON-serializable on purpose)::

    {"action": "purge_url", "path": "<bucket>/<url-encoded-key>", "bucket": "<bucket>",
     "providers": ["cloudflare", "bunny"], "attempts": 0,
     "next_try_ts": 0.0, "created_ts": <epoch>}

NOTE: module is named ``jobqueue`` (not ``queue``) so it does not shadow the stdlib
``queue`` module that ``waitress`` imports internally.
"""

import hashlib
import json
import os
from pathlib import Path


class Queue:
    def __init__(self, root: str):
        self.root = Path(root)
        self.pending = self.root / "pending"
        self.dead = self.root / "dead"
        for folder in (self.pending, self.dead):
            folder.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def writable(self) -> bool:
        return os.access(self.pending, os.W_OK)

    @staticmethod
    def _hash(job: dict) -> str:
        return hashlib.sha1(
            f"{job.get('action')}:{job.get('path')}".encode()
        ).hexdigest()[:12]

    def _name(self, job: dict, h: str) -> str:
        self._seq += 1
        return f"{job.get('created_ts', 0):015.6f}-{self._seq:06d}-{h}.json"

    def _atomic_write(self, folder: Path, name: str, job: dict) -> None:
        tmp = folder / f".{name}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, folder / name)  # atomic on POSIX and Windows

    def put(self, job: dict) -> bool:
        """Enqueue a job. Returns False if an identical job is already pending (dedupe)."""
        h = self._hash(job)
        if any(self.pending.glob(f"*-{h}.json")):
            return False
        self._atomic_write(self.pending, self._name(job, h), job)
        return True

    def due(self, now: float) -> list[tuple[Path, dict]]:
        """Return ``(path, job)`` for all pending jobs whose ``next_try_ts`` has passed."""
        items = []
        for path in sorted(self.pending.glob("*.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if job.get("next_try_ts", 0) <= now:
                items.append((path, job))
        return items

    def ack(self, path: Path) -> None:
        """Remove a completed job."""
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def retry(self, path: Path, job: dict) -> None:
        """Persist an updated job (bumped attempts / next_try_ts) in place."""
        self._atomic_write(path.parent, path.name, job)

    def dead_letter(self, path: Path, job: dict) -> None:
        """Move a job that exhausted retries (or is non-retryable) to ``dead/``."""
        self._atomic_write(self.dead, path.name, job)
        self.ack(path)
