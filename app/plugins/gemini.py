"""Google Gemini / Generative Language API plugin.

Auth model: an API key with the ``AIza`` prefix (39 chars total), sent via the
``x-goog-api-key`` header. Gemini exposes **no** rate-limit headers, so the rate
signal must be inferred entirely from response bodies:

- 200                            -> OK
- 429 (RESOURCE_EXHAUSTED)       -> RATE_LIMIT; body ``details[]`` carries a
  ``google.rpc.RetryInfo`` (``retryDelay`` like ``"34s"``) and a ``QuotaFailure``
  (``violations[].quotaValue`` / ``quotaId`` whose ``FreeTier`` suffix => free).
- 400 API_KEY_INVALID / 403 PERMISSION_DENIED -> AUTH (bad/revoked key)
- 400 FAILED_PRECONDITION        -> BILLING (billing/region required)
- 5xx (500/503/504)              -> SERVER / OVERLOADED

Grade strategy: two bursts (pro model, then image model) at the configured rps
for the configured duration; bucket the first-429 arrival time into a 1/2/3
level per model and emit a ``t{pro}-{image}`` tier label (e.g. ``"t3-1"``).
Rate limits on Gemini are per-project, not per-key, so the level reflects the
project behind the key.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.config import GeminiConfig
from app.http_util import base_outcome, timed_request
from app.models import (
    BurstResult,
    CheckMode,
    ErrorClass,
    KeyResult,
    KeyStatus,
    ProbeOutcome,
)
from app.plugins.base import CheckContext, CheckerPlugin
from app.ratetest import run_burst

# AIza prefix + 35 url-safe chars = 39 chars total.
_KEY_RE = re.compile(r"^AIza[0-9A-Za-z_\-]{35}$")

# Map of Gemini canonical status strings (in the JSON error body) to our taxonomy.
_AUTH_STATUSES = {"API_KEY_INVALID", "PERMISSION_DENIED", "UNAUTHENTICATED"}


def _parse_retry_delay(value: Any) -> float | None:
    """Parse a RetryInfo.retryDelay like ``"34s"`` / ``"1.5s"`` into seconds."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v.endswith("s"):
        v = v[:-1]
    try:
        return float(v)
    except ValueError:
        return None


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    """Best-effort JSON parse of a response body; never raises."""
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_quota(details: list[Any]) -> dict[str, Any]:
    """Pull the first QuotaFailure violation's quotaValue / quotaId out of an
    error ``details[]`` array, plus whether it looks like a free-tier quota."""
    out: dict[str, Any] = {}
    for item in details:
        if not isinstance(item, dict):
            continue
        if "QuotaFailure" not in str(item.get("@type", "")):
            continue
        violations = item.get("violations")
        if not isinstance(violations, list):
            continue
        for v in violations:
            if not isinstance(v, dict):
                continue
            quota_id = v.get("quotaId")
            quota_value = v.get("quotaValue")
            if quota_id is not None:
                out["quota_id"] = quota_id
            if quota_value is not None:
                out["quota_value"] = quota_value
            if quota_id is not None or quota_value is not None:
                out["free_tier"] = "FreeTier" in str(quota_id or "")
                return out
    return out


def _classify(resp: httpx.Response | None, base: ProbeOutcome) -> ProbeOutcome:
    """Refine a base outcome into the Gemini-specific ErrorClass, parsing the
    JSON error body for status string, retryDelay, and quota details."""
    if resp is None:
        # base_outcome already classified network failures as NETWORK.
        return base

    code = resp.status_code
    if 200 <= code < 300:
        base.error_class = ErrorClass.OK
        base.ok = True
        return base

    body = _safe_json(resp)
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    status_str = str(err.get("status") or "")
    message = str(err.get("message") or "")
    details = err.get("details") if isinstance(err.get("details"), list) else []
    base.body_snippet = (message or str(body))[:300] or None

    if code == 429 or status_str == "RESOURCE_EXHAUSTED":
        base.error_class = ErrorClass.RATE_LIMIT
        # Gemini has no Retry-After header — extract retryDelay from details[].
        for item in details:
            if isinstance(item, dict) and "RetryInfo" in str(item.get("@type", "")):
                delay = _parse_retry_delay(item.get("retryDelay"))
                if delay is not None:
                    base.retry_after_s = delay
                break
        quota = _extract_quota(details)
        if quota:
            base.detail = (
                f"quotaId={quota.get('quota_id')} "
                f"quotaValue={quota.get('quota_value')} "
                f"free_tier={quota.get('free_tier')}"
            )
        return base

    if code == 400:
        if "API_KEY_INVALID" in status_str or "API_KEY_INVALID" in message:
            base.error_class = ErrorClass.AUTH
        elif status_str == "FAILED_PRECONDITION":
            base.error_class = ErrorClass.BILLING
        else:
            base.error_class = ErrorClass.BAD_REQUEST
        return base

    if code == 403 or status_str in _AUTH_STATUSES:
        base.error_class = ErrorClass.AUTH
        return base

    if code == 404 or status_str == "NOT_FOUND":
        base.error_class = ErrorClass.NOT_FOUND
        return base

    if code == 503:
        base.error_class = ErrorClass.OVERLOADED
        return base
    if code in (500, 504) or 500 <= code < 600:
        base.error_class = ErrorClass.SERVER
        return base

    base.error_class = ErrorClass.UNKNOWN
    return base


class GeminiPlugin(CheckerPlugin):
    """Provider plugin for Google's Generative Language API (Gemini)."""

    name = "gemini"

    # ---- offline detection -------------------------------------------------

    def matches(self, key: str) -> bool:
        """True for an ``AIza``-prefixed 39-char Google API key."""
        return bool(_KEY_RE.match(key.strip()))

    # ---- shared request helpers -------------------------------------------

    def _cfg(self, ctx: CheckContext) -> GeminiConfig:
        return ctx.settings.gemini

    def _headers(self, key: str) -> dict[str, str]:
        return {"x-goog-api-key": key, "Content-Type": "application/json"}

    @staticmethod
    def _text_body() -> dict[str, Any]:
        return {
            "contents": [{"parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 1, "temperature": 0},
        }

    @staticmethod
    def _image_body() -> dict[str, Any]:
        return {
            "contents": [{"parts": [{"text": "a red dot"}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }

    async def _generate_probe(
        self, key: str, ctx: CheckContext, model: str, body: dict[str, Any]
    ) -> ProbeOutcome:
        """One ``generateContent`` request, normalized into a ProbeOutcome."""
        cfg = self._cfg(ctx)
        url = f"{cfg.base_url}/models/{model}:generateContent"
        resp, elapsed_ms, exc = await timed_request(
            ctx.client, "POST", url, headers=self._headers(key), json=body
        )
        outcome = base_outcome(resp, elapsed_ms, exc)
        return _classify(resp, outcome)

    # ---- health: cheapest possible liveness check -------------------------

    async def health_check(
        self, key: str, result: KeyResult, ctx: CheckContext
    ) -> None:
        """GET /models?pageSize=1 — a near-free call proving the key authenticates."""
        cfg = self._cfg(ctx)
        await ctx.progress(0.1, "gemini: probing liveness")
        url = f"{cfg.base_url}/models"
        resp, elapsed_ms, exc = await timed_request(
            ctx.client,
            "GET",
            url,
            headers=self._headers(key),
            params={"pageSize": 1},
        )
        outcome = _classify(resp, base_outcome(resp, elapsed_ms, exc))
        result.details["health_probe"] = {
            "status_code": outcome.status_code,
            "error_class": outcome.error_class.value,
            "elapsed_ms": outcome.elapsed_ms,
        }

        if outcome.ok:
            result.status = KeyStatus.ALIVE
            result.alive = True
            result.remarks.append(f"alive ({outcome.elapsed_ms} ms)")
        elif outcome.error_class == ErrorClass.AUTH:
            result.status = KeyStatus.DEAD
            result.alive = False
            result.remarks.append("dead: API key invalid / unauthorized")
        elif outcome.error_class == ErrorClass.NETWORK:
            # Not a verdict on the key — surface as an error for the job manager.
            result.status = KeyStatus.ERROR
            result.error = outcome.detail or "network error"
            result.remarks.append("network error during liveness probe")
        else:
            # Reachable & authenticated enough to get a structured error (rate
            # limit, billing, server). The key itself is live.
            result.status = KeyStatus.ALIVE
            result.alive = True
            result.remarks.append(
                f"alive (non-auth response: {outcome.error_class.value})"
            )
        await ctx.progress(1.0, "gemini: health done")

    # ---- grade: two bursts -> t{pro}-{image} ------------------------------

    def _bucket_level(self, first_429_at_s: float | None, cfg: GeminiConfig) -> int:
        """Bucket first-429 arrival into a 1/2/3 level (later/never == higher)."""
        if first_429_at_s is None:
            return 3
        if first_429_at_s < cfg.t1_cut_s:
            return 1
        if first_429_at_s < cfg.t2_cut_s:
            return 2
        return 3

    def _burst_params(self, ctx: CheckContext, cfg: GeminiConfig) -> tuple[float, float]:
        """(rps, duration) honoring full_load vs. gentle settings."""
        if ctx.full_load:
            return cfg.burst_rps, cfg.burst_seconds
        return cfg.gentle_rps, cfg.gentle_seconds

    async def grade_check(
        self, key: str, result: KeyResult, ctx: CheckContext
    ) -> None:
        """Burst the pro model then the image model; map first-429 timing to a tier."""
        cfg = self._cfg(ctx)
        rps, duration = self._burst_params(ctx, cfg)

        # --- liveness gate: a single pro probe before spending a full burst ---
        await ctx.progress(0.02, "gemini: auth probe")
        first = await self._generate_probe(key, ctx, cfg.pro_model, self._text_body())
        if first.error_class == ErrorClass.AUTH:
            result.status = KeyStatus.DEAD
            result.alive = False
            result.remarks.append("dead: API key invalid / unauthorized")
            result.details["auth_probe"] = {
                "status_code": first.status_code,
                "error_class": first.error_class.value,
            }
            await ctx.progress(1.0, "gemini: dead")
            return
        if first.error_class == ErrorClass.NETWORK:
            result.status = KeyStatus.ERROR
            result.error = first.detail or "network error"
            result.remarks.append("network error during grade probe")
            await ctx.progress(1.0, "gemini: error")
            return

        # The pre-burst probe is the only place we see a single, fully-parsed
        # outcome (run_burst aggregates and hides individual outcomes). If it was
        # already a 429, capture its quota detail — that's the richest signal for
        # free-tier vs paid available before the bursts smear the data together.
        probe_quota: str | None = first.detail if first.detail else None
        if first.error_class == ErrorClass.RATE_LIMIT:
            result.details["pro_probe_429"] = {
                "retry_after_s": first.retry_after_s,
                "quota": first.detail,
            }

        # --- PRO burst (progress 0.0 -> 0.45) ---
        async def pro_tick(frac: float) -> None:
            await ctx.progress(0.45 * frac, f"gemini: pro burst {int(frac * 100)}%")

        async def pro_probe() -> ProbeOutcome:
            return await self._generate_probe(
                key, ctx, cfg.pro_model, self._text_body()
            )

        pro_res: BurstResult = await run_burst(
            pro_probe,
            target_rps=rps,
            duration_s=duration,
            stop_on_first_429=False,
            on_tick=pro_tick,
        )
        pro_level = self._bucket_level(pro_res.first_429_at_s, cfg)

        # --- IMAGE burst (progress 0.45 -> 0.9) ---
        async def image_tick(frac: float) -> None:
            await ctx.progress(
                0.45 + 0.45 * frac, f"gemini: image burst {int(frac * 100)}%"
            )

        async def image_probe() -> ProbeOutcome:
            return await self._generate_probe(
                key, ctx, cfg.image_model, self._image_body()
            )

        image_res: BurstResult = await run_burst(
            image_probe,
            target_rps=rps,
            duration_s=duration,
            stop_on_first_429=False,
            on_tick=image_tick,
        )
        image_level = self._bucket_level(image_res.first_429_at_s, cfg)

        await ctx.progress(0.95, "gemini: scoring")

        # --- assemble verdict ---
        tier = f"t{pro_level}-{image_level}"
        result.tier = tier
        result.status = KeyStatus.GRADED
        result.alive = True

        quota_value = self._quota_value(pro_res) or self._quota_value(image_res)
        quota_value = quota_value or probe_quota

        result.remarks.append(
            f"pro: first-429 @ {self._fmt_t(pro_res.first_429_at_s)} "
            f"(level {pro_level}; {pro_res.success}/{pro_res.total_sent} ok, "
            f"{pro_res.rate_limited} x429)"
        )
        result.remarks.append(
            f"image: first-429 @ {self._fmt_t(image_res.first_429_at_s)} "
            f"(level {image_level}; {image_res.success}/{image_res.total_sent} ok, "
            f"{image_res.rate_limited} x429)"
        )
        if quota_value is not None:
            result.remarks.append(f"quotaValue: {quota_value}")
        if not ctx.full_load:
            result.remarks.append(
                f"gentle mode ({rps} rps x {duration}s) — timing buckets approximate"
            )

        result.details["grade"] = {
            "tier": tier,
            "pro_level": pro_level,
            "image_level": image_level,
            "full_load": ctx.full_load,
            "burst_rps": rps,
            "burst_seconds": duration,
            "pro": self._burst_detail(pro_res),
            "image": self._burst_detail(image_res),
        }
        await ctx.progress(1.0, f"gemini: {tier}")

    # ---- small formatting helpers -----------------------------------------

    @staticmethod
    def _fmt_t(t: float | None) -> str:
        return "never" if t is None else f"{t}s"

    @staticmethod
    def _quota_value(res: BurstResult) -> str | None:
        """Surface a quotaValue captured by a 429's detail string, if any."""
        for note in res.notes:
            if "quotaValue=" in note:
                return note
        return None

    @staticmethod
    def _burst_detail(res: BurstResult) -> dict[str, Any]:
        return {
            "total_sent": res.total_sent,
            "success": res.success,
            "rate_limited": res.rate_limited,
            "other_errors": res.other_errors,
            "first_429_at_s": res.first_429_at_s,
            "duration_s": res.duration_s,
            "error_breakdown": res.error_breakdown,
        }


# Registry auto-discovers this module-level instance.
PLUGIN = GeminiPlugin()
