"""Background worker: drains the durable queue to CDN providers.

One daemon thread. Each tick: pick due jobs, group by ``action`` (an executor map —
extend it to add new job types), and for ``purge_url`` coalesce due jobs per provider,
dedupe URLs, chunk to the provider's batch limit, rate-limit, purge, then reconcile:
delete-on-success / bump-attempts-with-backoff / dead-letter.

Multi-provider is tracked per job so a job that succeeded on one provider but got a
``429`` on another only retries the still-pending provider (purge is idempotent).
"""

import random
import threading
import time

from ratelimit import TokenBucket


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class Worker(threading.Thread):
    def __init__(self, cfg, queue, providers, log):
        super().__init__(daemon=True, name="worker")
        self._cfg = cfg
        self._queue = queue
        self._providers = {p.name: p for p in providers}
        self._log = log
        self._stop = threading.Event()
        self._buckets = {
            p.name: TokenBucket(p.rate_capacity, p.rate_refill) for p in providers
        }
        # action -> executor. Add a new job type by registering another entry here.
        self._executors = {"purge_url": self._run_purge_url}

    def stop(self):
        self._stop.set()

    def run(self):
        poll = max(0.05, self._cfg.batch_wait_ms / 1000.0)
        self._log.info("worker thread started")
        while not self._stop.is_set():
            worked = False
            try:
                worked = self._tick()
            except Exception as e:  # a drain loop must never die
                self._log.error(f"worker tick failed: {e}")
            if not worked:
                self._stop.wait(poll)

    def _tick(self) -> bool:
        now = time.time()
        due = self._queue.due(now)
        if not due:
            return False
        by_action: dict = {}
        for path, job in due:
            by_action.setdefault(job.get("action"), []).append((path, job))
        for action, batch in by_action.items():
            executor = self._executors.get(action)
            if executor is None:
                self._log.warning(
                    f"no executor for action '{action}' -> dead-lettering {len(batch)} job(s)"
                )
                for path, job in batch:
                    self._queue.dead_letter(path, job)
                continue
            executor(batch, now)
        return True

    def _run_purge_url(self, batch, now):
        cfg = self._cfg
        outcomes: dict = {path: {} for path, _ in batch}

        for name, provider in self._providers.items():
            targets = [(p, j) for p, j in batch if name in j.get("providers", [])]
            if not targets:
                continue
            url_to_paths: dict = {}
            for path, job in targets:
                url = f"{cfg.base_for(name, job.get('bucket', ''))}/{job['path']}"
                url_to_paths.setdefault(url, []).append(path)

            limit = max(1, min(provider.batch_limit, cfg.batch_size))
            for chunk in _chunks(list(url_to_paths), limit):
                self._buckets[name].take(len(chunk))
                result = provider.purge(chunk)
                for url in chunk:
                    for path in url_to_paths[url]:
                        outcomes[path][name] = result
                if result.ok:
                    self._log.info(f"purged {len(chunk)} url(s) via {name}")
                else:
                    level = self._log.warning if result.retryable else self._log.error
                    level(f"purge via {name} failed ({result.detail}) for {len(chunk)} url(s)")

        self._reconcile(batch, outcomes, now)

    def _reconcile(self, batch, outcomes, now):
        cfg = self._cfg
        enabled = set(self._providers)
        for path, job in batch:
            wanted = [pr for pr in job.get("providers", []) if pr in enabled]
            if not wanted:
                self._queue.ack(path)  # no actionable provider (creds removed) -> drop
                continue
            res = outcomes.get(path, {})
            remaining = [pr for pr in wanted if not (res.get(pr) and res[pr].ok)]
            if not remaining:
                self._queue.ack(path)
                continue
            retryable = any(res.get(pr) is None or res[pr].retryable for pr in remaining)
            if retryable and job.get("attempts", 0) + 1 < cfg.max_retries:
                job["attempts"] = job.get("attempts", 0) + 1
                job["providers"] = remaining  # only retry still-pending providers
                job["next_try_ts"] = now + self._backoff(job["attempts"])
                self._queue.retry(path, job)
            else:
                self._queue.dead_letter(path, job)
                self._log.error(
                    f"dead-lettered {job.get('path')} after {job.get('attempts', 0) + 1} "
                    f"attempt(s) (providers: {','.join(remaining)})"
                )

    def _backoff(self, attempts: int) -> float:
        base = min(
            self._cfg.retry_max_seconds,
            self._cfg.retry_base_seconds * (2 ** (attempts - 1)),
        )
        return base + random.uniform(0, base * 0.1)  # jitter
