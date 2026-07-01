"""Configuration for minio-worker.

All settings come from environment variables (12-factor). ``load_config()`` parses
them once at startup into a frozen ``Config``; ``Context`` bundles the config with
the set of providers that were auto-enabled, and is what handlers receive.
"""

import json
import os
from dataclasses import dataclass


def _get(name: str, default: str | None = None) -> str | None:
    """Return an env var, treating empty strings as unset (compose passes ``:-`` as '')."""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Config:
    webhook_auth_token: str
    listen_host: str
    listen_port: int
    queue_dir: str
    public_base_url: str
    cf_public_base_url: str
    bunny_public_base_url: str
    host_map: dict
    max_retries: int
    retry_base_seconds: float
    retry_max_seconds: float
    batch_size: int
    batch_wait_ms: int
    log_level: str
    cf_api_token: str
    cf_zone_id: str
    bunny_api_key: str

    def base_for(self, provider: str, bucket: str) -> str:
        """Resolve the public base URL for a purge, per bucket then per provider.

        Precedence: per-bucket ``HOST_MAP_JSON`` > per-provider override > ``PUBLIC_BASE_URL``.
        Lets Cloudflare and Bunny front different hostnames.
        """
        if bucket and bucket in self.host_map:
            return self.host_map[bucket]
        if provider == "cloudflare" and self.cf_public_base_url:
            return self.cf_public_base_url
        if provider == "bunny" and self.bunny_public_base_url:
            return self.bunny_public_base_url
        return self.public_base_url

    def secret_values(self) -> list[str]:
        """Secret strings to redact from logs."""
        return [v for v in (self.webhook_auth_token, self.cf_api_token, self.bunny_api_key) if v]


@dataclass(frozen=True)
class Context:
    """Runtime context passed to handlers (config + which providers are live)."""

    cfg: Config
    enabled_providers: tuple


def load_config() -> Config:
    listen = _get("LISTEN_ADDR", "0.0.0.0:8080")
    host, _, port = listen.rpartition(":")

    host_map_raw = _get("HOST_MAP_JSON", "")
    try:
        host_map = json.loads(host_map_raw) if host_map_raw else {}
    except json.JSONDecodeError as e:
        raise ValueError(f"HOST_MAP_JSON is not valid JSON: {e}") from e
    host_map = {k: str(v).rstrip("/") for k, v in host_map.items()}

    def _url(name: str) -> str:
        return (_get(name, "") or "").rstrip("/")

    return Config(
        webhook_auth_token=_get("WEBHOOK_AUTH_TOKEN", "") or "",
        listen_host=host or "0.0.0.0",
        listen_port=int(port or "8080"),
        queue_dir=_get("QUEUE_DIR", "/data/queue"),
        public_base_url=_url("PUBLIC_BASE_URL"),
        cf_public_base_url=_url("CF_PUBLIC_BASE_URL"),
        bunny_public_base_url=_url("BUNNY_PUBLIC_BASE_URL"),
        host_map=host_map,
        max_retries=int(_get("MAX_RETRIES", "10")),
        retry_base_seconds=float(_get("RETRY_BASE_SECONDS", "2")),
        retry_max_seconds=float(_get("RETRY_MAX_SECONDS", "300")),
        batch_size=int(_get("BATCH_SIZE", "30")),
        batch_wait_ms=int(_get("BATCH_WAIT_MS", "500")),
        log_level=(_get("LOG_LEVEL", "INFO") or "INFO").upper(),
        cf_api_token=_get("CF_API_TOKEN", "") or "",
        cf_zone_id=_get("CF_ZONE_ID", "") or "",
        bunny_api_key=_get("BUNNY_API_KEY", "") or "",
    )
