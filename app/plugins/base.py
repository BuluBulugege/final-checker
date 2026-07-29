"""The plugin contract. Every provider is a self-contained plugin implementing
this interface. To add a 4th provider: drop a new module in app/plugins/ whose
PLUGIN attribute is an instance of a CheckerPlugin subclass — it auto-registers.

Design goals:
- matches() is a pure, fast, offline pattern test on the raw key string.
- health_check() and grade_check() are async, take a shared httpx.AsyncClient,
  and report incremental progress through a callback so the UI can stream it.
- Plugins never raise for "the key is bad" — that's a KeyResult verdict. They
  may raise only for genuine internal bugs, which the job manager turns into ERROR.
- Core code (parsing, masking, dispatch, error messages) learns about providers
  exclusively through the metadata (PluginMeta) and the optional hooks below —
  adding a plugin never requires touching core.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.models import CheckMode, KeyResult
from app.redact import redact  # re-exported for plugins: `from app.plugins.base import redact`

__all__ = [
    "CheckContext",
    "CheckerPlugin",
    "PluginMeta",
    "ProgressCb",
    "mask_key",
    "redact",
]

# progress_cb(fraction_0_to_1, label) — both args optional-ish; label may be None
ProgressCb = Callable[[float, str | None], Awaitable[None]]


class PluginMeta(BaseModel):
    """Structured metadata every plugin declares.

    - ``key_format_hint``: one-line human description of the credential format,
      used to build the "unsupported key" error message.
    - ``capabilities``: which check modes the plugin implements.
    - ``priority``: dispatch order, lower runs first. It MUST reproduce the
      historical effective order (anthropic 10, aws_bedrock 20, azure 30,
      gcp 40, gemini 50, openai 90). openai is deliberately last: its permissive
      ``sk-`` matcher would otherwise swallow Anthropic's ``sk-ant-…`` keys.
    - ``enabled``: disabled plugins stay registered but never dispatch.
    """

    name: str
    version: str = "1.0.0"
    description: str = ""
    key_format_hint: str = ""
    capabilities: list[str] = Field(default_factory=lambda: ["health", "grade"])
    priority: int = 100
    enabled: bool = True


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

    #: structured metadata; every concrete plugin MUST override this
    meta: PluginMeta = PluginMeta(name="base", description="abstract base plugin")

    @property
    def name(self) -> str:
        """Short stable identifier, e.g. "gemini" — sourced from meta."""
        return self.meta.name

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

    # ------------------------------------------------------------------ #
    # optional hooks — defaults keep core fully generic
    # ------------------------------------------------------------------ #
    def mask(self, key: str) -> str | None:
        """Optional: provider-specific masked label for display (e.g. an
        identifiable account/host instead of a brace fragment). Return None to
        fall back to the generic prefix/suffix mask. Called with the stripped
        key; should be cheap, offline, and tolerant of malformed input."""
        return None

    def extract_candidates(self, text: str) -> list[str] | None:
        """Optional: recognize a provider-specific WHOLE-INPUT format (e.g. the
        provider's array inside an all_combos aggregate export) and expand it
        to credential strings.

        Return None when the input is not in this plugin's format. Return a
        (possibly empty) list when the format IS recognized — the core then
        uses the merged result of all claiming plugins instead of its generic
        line/JSON splitting. Skipping invalid rows is the plugin's choice."""
        return None

    def stitch(self, lines: list[str], i: int) -> tuple[str | None, set[int]] | None:
        """Optional: multi-line credential stitching during line preprocessing.

        ``lines`` is the raw input split on ``"\\n"`` and ``i`` indexes a
        non-empty, non-comment line. Return None when line ``i`` does not start
        one of this plugin's multi-line formats. Otherwise return
        ``(credential, consumed)``: the joined single-line credential (or None
        to swallow the line silently, e.g. an orphan continuation line) plus
        the extra line indices folded into it."""
        return None


def _generic_mask(key: str) -> str:
    """Default mask: keep a short prefix and the last 4 chars."""
    if len(key) <= 12:
        return (key[:4] + "…") if key else "(empty)"
    return f"{key[:8]}…{key[-4:]}"


def mask_key(key: str) -> str:
    """Mask a credential for display.

    The plugin that CLAIMS the key gets first say via its mask() hook (so a
    GCP service-account JSON shows its client_email, an Azure URL|KEY its
    host, etc.); unclaimed keys get a second pass over the plugins' weak
    format hooks before falling back to the generic mask.
    """
    from app.plugins.registry import all_plugins  # lazy: registry imports this module

    key = key.strip()
    plugins = all_plugins()
    # 1) the claiming plugin masks its own credential
    for plugin in plugins:
        try:
            if plugin.matches(key):
                masked = plugin.mask(key)
                if masked is not None:
                    return masked
        except Exception:
            continue
    # 2) weak hooks for malformed keys no plugin would claim
    for plugin in plugins:
        try:
            masked = plugin.mask(key)
        except Exception:
            continue
        if masked is not None:
            return masked
    return _generic_mask(key)
