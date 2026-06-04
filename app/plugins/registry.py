"""Plugin registry with auto-discovery. Any module in this package that exposes a
module-level `PLUGIN` (a CheckerPlugin instance) is registered automatically on
import. Dispatch picks the first plugin whose matches() accepts a key.
"""

from __future__ import annotations

import importlib
import pkgutil

from app.plugins.base import CheckerPlugin

_REGISTRY: list[CheckerPlugin] = []
_LOADED = False


def register(plugin: CheckerPlugin) -> None:
    if any(p.name == plugin.name for p in _REGISTRY):
        return
    _REGISTRY.append(plugin)


def _discover() -> None:
    global _LOADED
    if _LOADED:
        return
    import app.plugins as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name in {"base", "registry"}:
            continue
        module = importlib.import_module(f"app.plugins.{mod.name}")
        plugin = getattr(module, "PLUGIN", None)
        if isinstance(plugin, CheckerPlugin):
            register(plugin)
    _LOADED = True


def all_plugins() -> list[CheckerPlugin]:
    _discover()
    return list(_REGISTRY)


def dispatch(key: str) -> CheckerPlugin | None:
    """Return the plugin that claims this key, or None if unsupported."""
    _discover()
    k = key.strip()
    for plugin in _REGISTRY:
        try:
            if plugin.matches(k):
                return plugin
        except Exception:
            continue
    return None
