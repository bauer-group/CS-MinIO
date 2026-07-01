"""Bunny CDN provider — POST https://api.bunny.net/purge?url=..., AccessKey header.

The account-level purge-by-URL endpoint takes a single URL per call (``batch_limit=1``);
the worker loops per URL. A future optimization is the pull-zone batch endpoint
(``POST /pullzone/{id}/purgeCache``) — left out until its request shape is verified
against Bunny's official docs.
"""

from providers.base import PurgeResult

_API = "https://api.bunny.net/purge"


class BunnyProvider:
    name = "bunny"
    batch_limit = 1           # account-level purge-by-URL: one URL per request
    rate_capacity = 10
    rate_refill = 2           # Bunny publishes no hard limit -> stay conservative

    def __init__(self, cfg, log, http):
        self._log = log
        self._http = http
        self._key = cfg.bunny_api_key

    def enabled(self) -> bool:
        return bool(self._key)

    def purge(self, urls) -> PurgeResult:
        for url in urls:
            try:
                resp = self._http.post(
                    _API,
                    params={"url": url, "async": "false"},
                    headers={"AccessKey": self._key, "Accept": "application/json"},
                )
            except Exception as e:  # network / timeout
                return PurgeResult(False, True, f"network error: {e.__class__.__name__}")

            if 200 <= resp.status_code < 300:
                continue
            if resp.status_code in (401, 403):
                return PurgeResult(False, False, f"auth failed: http {resp.status_code}")
            retryable = resp.status_code == 429 or resp.status_code >= 500
            return PurgeResult(False, retryable, f"http {resp.status_code}")
        return PurgeResult(True, False, "ok")


def build(cfg, log, http):
    return BunnyProvider(cfg, log, http)
