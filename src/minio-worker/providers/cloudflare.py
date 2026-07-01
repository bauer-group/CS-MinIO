"""Cloudflare provider — POST /zones/{zone}/purge_cache, Bearer auth, <=30 files/request."""

from providers.base import PurgeResult

_API = "https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache"


class CloudflareProvider:
    name = "cloudflare"
    batch_limit = 30          # non-Enterprise plans: max 30 files per request
    rate_capacity = 30
    rate_refill = 5           # conservative; CF free tier allows ~1000 URLs/min

    def __init__(self, cfg, log, http):
        self._log = log
        self._http = http
        self._token = cfg.cf_api_token
        self._zone = cfg.cf_zone_id

    def enabled(self) -> bool:
        return bool(self._token and self._zone)

    def purge(self, urls) -> PurgeResult:
        try:
            resp = self._http.post(
                _API.format(zone=self._zone),
                headers={"Authorization": f"Bearer {self._token}"},
                json={"files": list(urls)},
            )
        except Exception as e:  # network / timeout
            return PurgeResult(False, True, f"network error: {e.__class__.__name__}")

        if resp.status_code == 200:
            body = {}
            try:
                body = resp.json()
            except ValueError:
                pass
            if body.get("success"):
                return PurgeResult(True, False, "ok")
            return PurgeResult(False, False, f"api rejected: {body.get('errors')}")

        retryable = resp.status_code == 429 or resp.status_code >= 500
        return PurgeResult(False, retryable, f"http {resp.status_code}")


def build(cfg, log, http):
    return CloudflareProvider(cfg, log, http)
