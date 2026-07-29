"""Plugin registry with auto-discovery. Any module in this package that exposes a
module-level `PLUGIN` (a CheckerPlugin instance) is registered automatically on
import. Dispatch picks the first plugin whose matches() accepts a key, iterating
in explicit priority order (PluginMeta.priority, lower first).

Priorities reproduce the historical effective order exactly:
anthropic 10, aws_bedrock 20, azure 30, gcp 40, gemini 50, openai 90.
openai MUST stay last: its permissive ``sk-`` matcher would claim Anthropic's
``sk-ant-…`` keys if it ran any earlier.
"""

from __future__ import annotations

import importlib
import pkgutil

from app.plugins.base import CheckerPlugin

_REGISTRY: list[CheckerPlugin] = []
_LOADED = False


def register(plugin: CheckerPlugin) -> None:
    """Register a plugin. Duplicate names are a configuration bug, not something
    to silently ignore — raise loudly, naming both source modules."""
    for existing in _REGISTRY:
        if existing.name == plugin.name:
            raise ValueError(
                f"duplicate plugin name {plugin.name!r} from module "
                f"{type(plugin).__module__!r} — already registered by "
                f"{type(existing).__module__!r}"
            )
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
    """Enabled plugins in dispatch order: explicit meta.priority (lower first),
    with the name as a deterministic tie-breaker."""
    _discover()
    return sorted(
        (p for p in _REGISTRY if p.meta.enabled),
        key=lambda p: (p.meta.priority, p.name),
    )


def dispatch(key: str) -> CheckerPlugin | None:
    """Return the plugin that claims this key, or None if unsupported."""
    _discover()
    k = key.strip()
    for plugin in all_plugins():
        try:
            if plugin.matches(k):
                return plugin
        except Exception:
            continue
    return None
