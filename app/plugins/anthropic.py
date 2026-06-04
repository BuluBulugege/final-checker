"""Anthropic (Claude API) plugin — direct first-party API at api.anthropic.com.

Detection: keys start with ``sk-ant-``.
Auth: header ``x-api-key: <key>`` plus ``anthropic-version`` and JSON content-type.

HEALTH (测活): a single minimum ``POST /v1/messages`` with the cheapest model and
``max_tokens=1`` proves the key can call a model.

GRADE (测等级): enumerate models via ``GET /v1/models``, then probe each with a
``max_tokens=1`` message. Anthropic returns its rate-limit headers on BOTH 200 and
429, so a single cheap call per model reveals the account's RPM/ITPM/OTPM ceilings.
The maximum observed RPM maps to a tier (T1=50, T2=1000, T3=2000, T4=4000); any
nonstandard RPM, priority-token headers, or token limits above the T4 ceiling mark
the account as enterprise (企业级).

Note: Anthropic has NO dedicated image model and NO "tokens-per-day" header — the
daily figure exposed elsewhere is a dollar spend cap, not a token rate limit.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import AnthropicConfig
from app.http_util import base_outcome, parse_retry_after, timed_request
from app.models import CheckMode, ErrorClass, KeyResult, KeyStatus, ProbeOutcome
from app.plugins.base import CheckContext, CheckerPlugin

# Map of HTTP status / Anthropic error.type -> normalized ErrorClass. Anthropic's
# JSON error body is {"type":"error","error":{"type":<t>,"message":...}}.
_ERROR_TYPE_MAP: dict[str, ErrorClass] = {
    "authentication_error": ErrorClass.AUTH,
    "rate_limit_error": ErrorClass.RATE_LIMIT,
    "billing_error": ErrorClass.BILLING,
    "permission_error": ErrorClass.PERMISSION,
    "not_found_error": ErrorClass.NOT_FOUND,
    "invalid_request_error": ErrorClass.BAD_REQUEST,
    "overloaded_error": ErrorClass.OVERLOADED,
}

# Rate-limit header names (lowercased by base_outcome's header normalization).
_HDR_RPM = "anthropic-ratelimit-requests-limit"
_HDR_ITPM = "anthropic-ratelimit-input-tokens-limit"
_HDR_OTPM = "anthropic-ratelimit-output-tokens-limit"
_HDR_TPM = "anthropic-ratelimit-tokens-limit"
# Any "anthropic-priority-*-tokens-limit" header signals priority/enterprise access.
_PRIORITY_PREFIX = "anthropic-priority-"

# The four standard RPM tiers; anything outside this set is nonstandard => enterprise.
_STANDARD_RPMS = {50, 1000, 2000, 4000}


def _classify(outcome: ProbeOutcome, resp: httpx.Response | None) -> ProbeOutcome:
    """Refine an outcome's error_class from the Anthropic JSON error body / status.

    base_outcome() already set OK for 2xx and NETWORK for transport errors; this
    fills in the auth/rate-limit/billing/etc. taxonomy for non-2xx responses.
    """
    if resp is None:
        return outcome  # transport error already classified as NETWORK
    if resp.is_success:
        outcome.error_class = ErrorClass.OK
        return outcome

    status = resp.status_code
    err_type: str | None = None
    message: str | None = None
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            err_type = err.get("type")
            message = err.get("message")
    except (ValueError, httpx.DecodingError):
        pass

    # Prefer the structured error.type; fall back to status-code heuristics.
    if err_type and err_type in _ERROR_TYPE_MAP:
        outcome.error_class = _ERROR_TYPE_MAP[err_type]
    elif status == 429:
        outcome.error_class = ErrorClass.RATE_LIMIT
    elif status == 401:
        outcome.error_class = ErrorClass.AUTH
    elif status == 402:
        outcome.error_class = ErrorClass.BILLING
    elif status == 403:
        outcome.error_class = ErrorClass.PERMISSION
    elif status == 404:
        outcome.error_class = ErrorClass.NOT_FOUND
    elif status == 400:
        outcome.error_class = ErrorClass.BAD_REQUEST
    elif status == 529:
        outcome.error_class = ErrorClass.OVERLOADED
    elif 500 <= status < 600:
        outcome.error_class = ErrorClass.SERVER
    else:
        outcome.error_class = ErrorClass.UNKNOWN

    # retry-after is only meaningful for a real rate limit.
    if outcome.error_class == ErrorClass.RATE_LIMIT and outcome.retry_after_s is None:
        outcome.retry_after_s = parse_retry_after(
            {k.lower(): v for k, v in resp.headers.items()}.get("retry-after")
        )
    outcome.detail = err_type or (message[:160] if message else None)
    return outcome


def _to_int(value: str | None) -> int | None:
    """Parse a header limit value to int, tolerating stray formatting; None if absent."""
    if value is None:
        return None
    try:
        return int(float(value.strip()))
    except (ValueError, AttributeError):
        return None


def _has_priority_header(headers: dict[str, str]) -> bool:
    """True if any nonzero anthropic-priority-*-tokens-limit header is present."""
    for name, val in headers.items():
        if name.startswith(_PRIORITY_PREFIX) and "tokens-limit" in name:
            n = _to_int(val)
            if n is not None and n > 0:
                return True
    return False


class AnthropicPlugin(CheckerPlugin):
    """测活/测等级 for first-party Anthropic Claude API keys."""

    name = "anthropic"

    # ---- offline detection -------------------------------------------------
    def matches(self, key: str) -> bool:
        return key.startswith("sk-ant-")

    # ---- request helpers ---------------------------------------------------
    def _cfg(self, ctx: CheckContext) -> AnthropicConfig:
        return ctx.settings.anthropic

    def _headers(self, key: str, ctx: CheckContext) -> dict[str, str]:
        return {
            "x-api-key": key,
            "anthropic-version": self._cfg(ctx).anthropic_version,
            "content-type": "application/json",
        }

    async def _probe_message(
        self, key: str, model: str, ctx: CheckContext
    ) -> ProbeOutcome:
        """POST a 1-token message to /messages and return a classified outcome.

        Used both for the health liveness check and per-model grade probes. The
        rate-limit headers Anthropic returns are preserved on the outcome for
        both 200 and 429 responses.
        """
        cfg = self._cfg(ctx)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        resp, elapsed_ms, exc = await timed_request(
            ctx.client,
            "POST",
            f"{cfg.base_url}/messages",
            headers=self._headers(key, ctx),
            json=payload,
        )
        outcome = base_outcome(resp, elapsed_ms, exc)
        if resp is not None and resp.text:
            outcome.body_snippet = resp.text[:300]
        return _classify(outcome, resp)

    # ---- HEALTH (测活) -----------------------------------------------------
    async def health_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        cfg = self._cfg(ctx)
        await ctx.progress(0.1, "probing claude /messages")
        outcome = await self._probe_message(key, cfg.probe_model, ctx)
        await ctx.progress(1.0, "done")

        result.details["health_probe"] = {
            "model": cfg.probe_model,
            "status_code": outcome.status_code,
            "error_class": outcome.error_class.value,
        }

        if outcome.error_class == ErrorClass.OK:
            result.status = KeyStatus.ALIVE
            result.alive = True
            result.progress_label = "alive"
            result.remarks.append(f"alive via {cfg.probe_model}")
            self._record_limits(result, outcome.headers)
            return

        # A real auth failure means the key is dead; everything else is a non-verdict
        # condition (network/server/overloaded) reported as ERROR so it can be retried.
        self._apply_failure(result, outcome)

    # ---- GRADE (测等级) ----------------------------------------------------
    async def grade_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        cfg = self._cfg(ctx)

        # 1) Enumerate models. Auth failure here => dead, stop early.
        await ctx.progress(0.02, "listing models")
        resp, elapsed_ms, exc = await timed_request(
            ctx.client,
            "GET",
            f"{cfg.base_url}/models?limit=1000",
            headers=self._headers(key, ctx),
        )
        list_outcome = _classify(base_outcome(resp, elapsed_ms, exc), resp)

        if list_outcome.error_class == ErrorClass.AUTH:
            self._apply_failure(result, list_outcome)
            return

        model_ids = self._extract_model_ids(resp)
        if not model_ids:
            # Listing unavailable (perm/network/etc.) — fall back to the probe model
            # so we can still grade liveness + a single set of limit headers.
            model_ids = [cfg.probe_model]
            result.remarks.append(
                "model listing unavailable; graded via fallback model"
            )

        discovered = len(model_ids)
        model_ids = model_ids[: cfg.max_models_to_probe]
        total = len(model_ids)
        if discovered > total:
            # Be transparent that "probe all models" was capped for cost/time.
            result.remarks.append(
                f"模型较多，仅检测前 {total}/{discovered} 个 "
                f"(可调 FC_ANTHROPIC__MAX_MODELS_TO_PROBE)"
            )

        # 2) Probe each model, pacing ~1/sec to avoid self-induced 429s. Headers are
        #    read from both 200 and 429; only AUTH is a hard dead-key verdict.
        per_model: dict[str, dict[str, int | None]] = {}
        max_rpm = 0
        max_itpm = 0
        max_otpm = 0
        max_tpm = 0
        priority_seen = False
        any_alive = False

        for i, model_id in enumerate(model_ids):
            await ctx.progress(
                0.05 + 0.9 * (i / total) if total else 0.05,
                f"probing {model_id} ({i + 1}/{total})",
            )
            outcome = await self._probe_message(key, model_id, ctx)

            if outcome.error_class == ErrorClass.AUTH:
                # Key revoked mid-run — definitive dead verdict.
                self._apply_failure(result, outcome)
                return
            if outcome.error_class == ErrorClass.NOT_FOUND:
                continue  # model not accessible to this account; skip

            # OK and RATE_LIMIT (and even billing/overloaded) carry limit headers.
            if outcome.error_class in (ErrorClass.OK, ErrorClass.RATE_LIMIT):
                any_alive = True

            h = outcome.headers
            rpm = _to_int(h.get(_HDR_RPM))
            itpm = _to_int(h.get(_HDR_ITPM))
            otpm = _to_int(h.get(_HDR_OTPM))
            tpm = _to_int(h.get(_HDR_TPM))
            if _has_priority_header(h):
                priority_seen = True

            per_model[model_id] = {"rpm": rpm, "itpm": itpm, "otpm": otpm, "tpm": tpm}
            max_rpm = max(max_rpm, rpm or 0)
            max_itpm = max(max_itpm, itpm or 0)
            max_otpm = max(max_otpm, otpm or 0)
            max_tpm = max(max_tpm, tpm or 0)

            # Be polite: pace roughly one probe per second.
            if i + 1 < total:
                await asyncio.sleep(1.0)

        result.details["models"] = per_model

        # If nothing succeeded and we never saw limit headers, the key cannot grade.
        if not any_alive and max_rpm == 0:
            result.status = KeyStatus.DEAD
            result.alive = False
            result.progress_label = "dead"
            result.error = "no model responded with rate-limit headers"
            result.remarks.append("no usable model / no rate-limit headers observed")
            await ctx.progress(1.0, "dead")
            return

        # 3) + 4) Determine tier / enterprise status.
        t4_ceiling = self._t4_rpm(cfg)
        nonstandard_rpm = max_rpm not in _STANDARD_RPMS and max_rpm > 0
        above_t4 = max_rpm > t4_ceiling

        enterprise_reasons: list[str] = []
        if nonstandard_rpm:
            enterprise_reasons.append(f"nonstandard RPM={max_rpm}")
        if above_t4:
            enterprise_reasons.append(f"RPM {max_rpm} > T4 ceiling {t4_ceiling}")
        if priority_seen:
            enterprise_reasons.append("priority-tokens limit header present")

        is_enterprise = bool(enterprise_reasons)

        if is_enterprise:
            tier = "企业级"
        else:
            tier = self._rpm_to_tier(cfg, max_rpm) or "T1"

        # 5) Finalize.
        result.status = KeyStatus.GRADED
        result.alive = True
        result.tier = tier
        result.progress_label = tier
        result.details["max_rpm"] = max_rpm
        result.details["max_itpm"] = max_itpm
        result.details["max_otpm"] = max_otpm
        result.details["max_tpm"] = max_tpm
        result.details["enterprise"] = is_enterprise

        # Compact per-model remarks: "model: RPM=.., ITPM=.., OTPM=..".
        for model_id, lim in per_model.items():
            result.remarks.append(
                f"{model_id}: RPM={lim['rpm']}, ITPM={lim['itpm']}, OTPM={lim['otpm']}"
            )
        if is_enterprise:
            result.remarks.append("企业级: " + "; ".join(enterprise_reasons))
        else:
            result.remarks.append(f"tier {tier} from max RPM={max_rpm}")
        result.remarks.append(
            "note: Anthropic has no image model and no TPD header "
            "(daily figure is a $ spend cap, not a token rate)"
        )
        await ctx.progress(1.0, tier)

    # ---- shared helpers ----------------------------------------------------
    @staticmethod
    def _extract_model_ids(resp: httpx.Response | None) -> list[str]:
        """Pull model ids from a GET /models response ({"data":[{"id":...},...]})."""
        if resp is None or not resp.is_success:
            return []
        try:
            body = resp.json()
        except (ValueError, httpx.DecodingError):
            return []
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return []
        ids: list[str] = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.append(item["id"])
        return ids

    @staticmethod
    def _rpm_to_tier(cfg: AnthropicConfig, rpm: int) -> str | None:
        """Map an observed RPM to a tier label using the highest tier it meets."""
        best: str | None = None
        best_threshold = -1
        for threshold, label in cfg.rpm_to_tier.items():
            if rpm >= threshold and threshold > best_threshold:
                best_threshold = threshold
                best = label
        return best

    @staticmethod
    def _t4_rpm(cfg: AnthropicConfig) -> int:
        """The highest standard RPM ceiling configured (T4 = 4000 by default)."""
        return max(cfg.rpm_to_tier.keys()) if cfg.rpm_to_tier else 4000

    def _record_limits(self, result: KeyResult, headers: dict[str, str]) -> None:
        """Stash any rate-limit headers seen during a health probe (informational)."""
        limits = {
            "rpm": _to_int(headers.get(_HDR_RPM)),
            "itpm": _to_int(headers.get(_HDR_ITPM)),
            "otpm": _to_int(headers.get(_HDR_OTPM)),
            "tpm": _to_int(headers.get(_HDR_TPM)),
        }
        if any(v is not None for v in limits.values()):
            result.details["limits"] = limits
            result.remarks.append(
                f"RPM={limits['rpm']}, ITPM={limits['itpm']}, OTPM={limits['otpm']}"
            )

    @staticmethod
    def _apply_failure(result: KeyResult, outcome: ProbeOutcome) -> None:
        """Translate a non-OK probe outcome into a KeyResult verdict.

        AUTH (and BILLING/PERMISSION, which mean the key is unusable) => DEAD.
        Transient conditions (network/server/overloaded/rate-limit) => ERROR so the
        job manager can surface a retryable failure rather than a false dead verdict.
        """
        ec = outcome.error_class
        detail = outcome.detail or (
            f"HTTP {outcome.status_code}" if outcome.status_code else ec.value
        )
        if ec in (ErrorClass.AUTH, ErrorClass.BILLING, ErrorClass.PERMISSION):
            result.status = KeyStatus.DEAD
            result.alive = False
            result.progress_label = "dead"
            result.remarks.append(f"dead: {ec.value} ({detail})")
        else:
            result.status = KeyStatus.ERROR
            result.alive = None
            result.progress_label = "error"
            result.error = f"{ec.value}: {detail}"
            result.remarks.append(f"error: {ec.value} ({detail})")


# Registry auto-discovers this module-level instance.
PLUGIN = AnthropicPlugin()
