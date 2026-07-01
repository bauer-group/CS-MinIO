"""Handler registry.

Discovers ``handlers/<name>.py`` modules (each declaring a ``SOURCE`` string and a
``handle(payload, ctx) -> list[job]`` function) and keys them by ``SOURCE``. The HTTP
layer routes ``POST /webhook/<source>`` to the matching handler (``/webhook`` defaults
to ``minio``). Adding a new event source = drop in one module — no core edits.
"""

import importlib
import pkgutil
from pathlib import Path


def discover(log) -> dict:
    registry = {}
    package_dir = Path(__file__).parent
    for mod in pkgutil.iter_modules([str(package_dir)]):
        if mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{mod.name}")
        source = getattr(module, "SOURCE", None)
        if source and hasattr(module, "handle"):
            registry[source] = module
            log.info(f"handler registered: source='{source}'")
    return registry
