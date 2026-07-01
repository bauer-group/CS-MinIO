"""Provider registry.

Discovers ``providers/<name>.py`` modules (each exposing a ``build(cfg, log, http)``
factory) and keeps those whose credentials are present — the auto-enable rule. Mirrors
the init container's task discovery so the codebase stays internally consistent.
"""

import importlib
import pkgutil
from pathlib import Path


def discover(cfg, log, http) -> list:
    providers = []
    package_dir = Path(__file__).parent
    for mod in pkgutil.iter_modules([str(package_dir)]):
        if mod.name == "base" or mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{mod.name}")
        build = getattr(module, "build", None)
        if build is None:
            continue
        provider = build(cfg, log, http)
        if provider is None:
            continue
        if provider.enabled():
            providers.append(provider)
            log.info(f"provider enabled: {provider.name} (batch<={provider.batch_limit})")
        else:
            log.debug(f"provider skipped (no credentials): {provider.name}")
    return providers
