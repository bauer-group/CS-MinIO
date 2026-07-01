"""Provider abstraction: where a purge job is sent.

Each concrete provider (``cloudflare``, ``bunny``, future: Fastly/Akamai/…) lives in
its own module and exposes a ``build(cfg, log, http) -> Provider | None`` factory that
the registry auto-discovers. A provider self-reports whether it is ``enabled()`` based
on the presence of its credentials — that is the "auto-enable per credentials" rule.
"""

from typing import NamedTuple, Protocol, runtime_checkable


class PurgeResult(NamedTuple):
    ok: bool          # True  -> job done for this provider (remove from queue)
    retryable: bool   # on failure: True -> backoff + retry, False -> dead-letter now
    detail: str       # short, SAFE-to-log reason (never contains a token)


@runtime_checkable
class Provider(Protocol):
    name: str
    batch_limit: int        # max URLs per purge() call
    rate_capacity: float    # token-bucket size
    rate_refill: float      # tokens/second

    def enabled(self) -> bool:
        """True when this provider's credentials are configured."""
        ...

    def purge(self, urls: list[str]) -> PurgeResult:
        """Purge the given absolute URLs from this provider's edge cache."""
        ...
