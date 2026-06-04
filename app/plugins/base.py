"""The plugin contract. Every provider is a self-contained plugin implementing
this interface. To add a 4th provider: drop a new module in app/plugins/ whose
PLUGIN attribute is an instance of a CheckerPlugin subclass — it auto-registers.

Design goals:
- matches() is a pure, fast, offline pattern test on the raw key string.
- health_check() and grade_check() are async, take a shared httpx.AsyncClient,
  and report incremental progress through a callback so the UI can stream it.
- Plugins never raise for "the key is bad" — that's a KeyResult verdict. They
  may raise only for genuine internal bugs, which the job manager turns into ERROR.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable

import httpx

from app.config import Settings
from app.models import CheckMode, KeyResult
from app.redact import redact  # re-exported for plugins: `from app.plugins.base import redact`

__all__ = ["CheckContext", "CheckerPlugin", "ProgressCb", "mask_key", "redact"]

# progress_cb(fraction_0_to_1, label) — both args optional-ish; label may be None
ProgressCb = Callable[[float, str | None], Awaitable[None]]


class CheckContext:
    """Everything a plugin needs to run one key's check, passed by the job manager."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        mode: CheckMode,
        full_load: bool,
        progress: ProgressCb,
    ) -> None:
        self.client = client
        self.settings = settings
        self.mode = mode
        self.full_load = full_load
        self.progress = progress


class CheckerPlugin(abc.ABC):
    """Base class for a provider plugin."""

    #: short stable identifier, e.g. "gemini", "openai", "anthropic"
    name: str = "base"

    @abc.abstractmethod
    def matches(self, key: str) -> bool:
        """Return True if this plugin recognizes `key` as one of its own.

        Must be a cheap, offline pattern check (prefix/format). Stripped of
        surrounding whitespace by the caller.
        """

    @abc.abstractmethod
    async def health_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        """测活: determine only whether the key can call a model. Mutate `result`
        in place (set alive, status, remarks, details)."""

    @abc.abstractmethod
    async def grade_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        """测等级: full tier/rate-limit probe. Implies liveness. Mutate `result`
        in place (set tier, alive, remarks, details, status)."""


def mask_key(key: str) -> str:
    """Mask a key for display: keep a short prefix and last 4 chars."""
    key = key.strip()
    if len(key) <= 12:
        return (key[:4] + "…") if key else "(empty)"
    return f"{key[:8]}…{key[-4:]}"

