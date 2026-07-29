"""OpenAI provider plugin.

Implements the CheckerPlugin contract for OpenAI API keys (sk-…, but not the
Anthropic sk-ant-… family).

HEALTH mode: a single cheap ``GET /v1/models`` proves the key can talk to the
API; the ``openai-organization`` response header is captured as a bonus.

GRADE mode: a three-step probe —
  1) organization identity (``GET /v1/me``) + key-type from the prefix,
  2) tier from the ``x-ratelimit-limit-tokens`` (TPM) header returned by a tiny
     ``POST /v1/chat/completions`` call (mapped against ``cfg.tpm_tiers``),
  3) image requests-per-minute ceiling via a capped burst against
     ``POST /v1/images/generations``, bucketed 0..5 per ``cfg.rpm_buckets``.

The final tier label has the form ``"T<n>-r<rpm>"`` e.g. ``"T5-r100"``.

Every response is classified into the shared ErrorClass taxonomy by reading the
JSON ``error.code`` field, not just the HTTP status: a 429 with
``rate_limit_exceeded`` is a TRUE rate limit (RATE_LIMIT), while a 429 with
``insufficient_quota`` is a billing problem (BILLING); a 401 ``invalid_api_key``
is AUTH.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import OpenAIConfig
from app.http_util import base_outcome, timed_request
from app.models import (
    CheckMode,
    ErrorClass,
    KeyResult,
    KeyStatus,
    ProbeOutcome,
)
from app.plugins.base import CheckContext, CheckerPlugin, PluginMeta
from app.ratetest import run_burst


class OpenAIPlugin(CheckerPlugin):
    """Checker for OpenAI API keys."""

    # NOTE: priority 90 keeps this plugin LAST in dispatch order. Its matcher
    # accepts any ``sk-…`` key, so running earlier would swallow Anthropic's
    # ``sk-ant-…`` keys before the anthropic plugin (priority 10) sees them.
    meta = PluginMeta(
        name="openai",
        version="1.0.0",
        description="OpenAI API 测活 + TPM/RPM 等级（chat 模型限流头 + 图像突发）",
        key_format_hint="OpenAI key（sk-…）",
        capabilities=["health", "grade"],
        priority=90,
    )

    # ------------------------------------------------------------------ #
    # detection
    # ------------------------------------------------------------------ #
    def matches(self, key: str) -> bool:
        """Offline format test: an OpenAI key starts with ``sk-`` but must not be
        an Anthropic key (those use the ``sk-ant-`` prefix)."""
        k = key.strip()
        return k.startswith("sk-") and not k.startswith("sk-ant-")

    # ------------------------------------------------------------------ #
    # small helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _auth_headers(key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key.strip()}"}

    @staticmethod
    def _key_type(key: str) -> str:
        """Human-readable key-type derived from the prefix."""
        k = key.strip()
        if k.startswith("sk-proj-"):
            return "项目密钥 (sk-proj)"
        if k.startswith("sk-svcacct-"):
            return "服务账户密钥 (sk-svcacct)"
        if k.startswith("sk-admin-"):
            return "管理员密钥 (sk-admin)"
        return "用户密钥 (sk-)"

    @staticmethod
    def _json(resp: httpx.Response | None) -> dict[str, Any]:
        """Best-effort JSON body decode; never raises."""
        if resp is None:
            return {}
        try:
            data = resp.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _error_code(body: dict[str, Any]) -> str | None:
        """Pull ``error.code`` (falling back to ``error.type``) from an error body."""
        err = body.get("error")
        if isinstance(err, dict):
            code = err.get("code") or err.get("type")
            return str(code) if code is not None else None
        return None

    @staticmethod
    def _error_message(body: dict[str, Any]) -> str | None:
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            return str(msg) if msg is not None else None
        return None

    def _classify(
        self, resp: httpx.Response | None, body: dict[str, Any]
    ) -> ErrorClass:
        """Map a response to the shared ErrorClass taxonomy using error.code,
        not just the HTTP status."""
        if resp is None:
            return ErrorClass.NETWORK
        if resp.is_success:
            return ErrorClass.OK

        status = resp.status_code
        code = (self._error_code(body) or "").lower()

        if status == 429:
            # A true rate limit vs. an exhausted-quota/billing problem look the
            # same at the HTTP layer; only error.code distinguishes them.
            if code == "insufficient_quota":
                return ErrorClass.BILLING
            if code == "rate_limit_exceeded":
                return ErrorClass.RATE_LIMIT
            # Unknown 429 code: fall back to a message-keyword heuristic so a
            # renamed billing error isn't silently misread as a rate limit.
            msg = (self._error_message(body) or "").lower()
            if any(w in msg for w in ("quota", "billing", "insufficient", "credit")):
                return ErrorClass.BILLING
            return ErrorClass.RATE_LIMIT
        if status == 401:
            return ErrorClass.AUTH
        if status == 403:
            return ErrorClass.PERMISSION
        if status == 404:
            return ErrorClass.NOT_FOUND
        if status == 400:
            return ErrorClass.BAD_REQUEST
        if 500 <= status < 600:
            return ErrorClass.SERVER
        return ErrorClass.UNKNOWN

    def rpm_bucket(self, rpm: float, cfg: OpenAIConfig) -> int:
        """Bucket an observed image-rpm into 0..5 per ``cfg.rpm_buckets``.

        Buckets are ``(upper_inclusive, index)`` pairs, ascending:
        0 -> 0; (0,25] -> 1; (25,100] -> 2; (100,200] -> 3; (200,300] -> 4; >300 -> 5.
        """
        buckets = cfg.rpm_buckets
        if rpm <= 0:
            return 0
        for upper, idx in buckets:
            if rpm <= upper:
                return idx
        # Above the highest configured threshold -> next bucket index up.
        return len(buckets)

    def _tier_label(self, observed_tpm: float | None, cfg: OpenAIConfig) -> str | None:
        """Pick the HIGHEST tier whose threshold is <= the observed TPM."""
        if observed_tpm is None:
            return None
        chosen: str | None = None
        # Iterate ascending by threshold so the last match wins (highest tier).
        for name, threshold in sorted(cfg.tpm_tiers.items(), key=lambda kv: kv[1]):
            if observed_tpm >= threshold:
                chosen = name
        return chosen

    # ------------------------------------------------------------------ #
    # health
    # ------------------------------------------------------------------ #
    async def health_check(
        self, key: str, result: KeyResult, ctx: CheckContext
    ) -> None:
        """测活: a single ``GET /v1/models`` is the cheapest proof of validity."""
        cfg = ctx.settings.openai
        await ctx.progress(0.1, "校验密钥…")

        url = f"{cfg.base_url}/models"
        resp, _elapsed, exc = await timed_request(
            ctx.client, "GET", url, headers=self._auth_headers(key)
        )
        body = self._json(resp)
        klass = ErrorClass.NETWORK if exc is not None else self._classify(resp, body)

        await ctx.progress(0.9, "处理结果…")

        if klass == ErrorClass.OK and resp is not None:
            org = resp.headers.get("openai-organization")
            result.status = KeyStatus.ALIVE
            result.alive = True
            result.remarks.append(self._key_type(key))
            if org:
                result.details["organization_id"] = org
                result.remarks.append(f"组织ID: {org}")
            result.details["http_status"] = resp.status_code
            result.progress_label = "存活"
        else:
            self._apply_failure(result, klass, resp, body)

        await ctx.progress(1.0, result.progress_label)

    # ------------------------------------------------------------------ #
    # grade
    # ------------------------------------------------------------------ #
    async def grade_check(
        self, key: str, result: KeyResult, ctx: CheckContext
    ) -> None:
        """测等级: org identity + TPM tier + image-rpm bucket. Implies liveness."""
        cfg = ctx.settings.openai
        headers = self._auth_headers(key)

        # --- Step 1: organization identity ----------------------------------
        await ctx.progress(0.05, "查询组织信息…")
        result.remarks.append(self._key_type(key))

        me_url = f"{cfg.base_url}/me"
        resp, _elapsed, exc = await timed_request(
            ctx.client, "GET", me_url, headers=headers
        )
        body = self._json(resp)
        klass = ErrorClass.NETWORK if exc is not None else self._classify(resp, body)

        # A bad key here is a verdict; bail out as DEAD.
        if klass == ErrorClass.AUTH or (
            exc is None and resp is not None and resp.status_code == 401
        ):
            self._apply_failure(result, klass, resp, body)
            await ctx.progress(1.0, result.progress_label)
            return
        if klass == ErrorClass.NETWORK:
            self._apply_failure(result, klass, resp, body)
            await ctx.progress(1.0, result.progress_label)
            return

        if resp is not None and resp.headers.get("openai-organization"):
            result.details["organization_id"] = resp.headers["openai-organization"]
        self._record_org(result, body)

        # --- Step 2: TPM tier from chat-completions headers ------------------
        await ctx.progress(0.25, "探测速率上限 (TPM)…")
        chat_outcome = await self._chat_probe(key, ctx, cfg)

        # A clear auth failure during the chat probe is still a DEAD verdict.
        if chat_outcome.error_class == ErrorClass.AUTH:
            self._apply_failure_from_outcome(result, chat_outcome)
            await ctx.progress(1.0, result.progress_label)
            return

        observed_tpm: float | None = None
        observed_rpm_hdr: float | None = None
        hdrs = chat_outcome.headers
        if hdrs:
            observed_tpm = self._to_float(hdrs.get("x-ratelimit-limit-tokens"))
            observed_rpm_hdr = self._to_float(hdrs.get("x-ratelimit-limit-requests"))

        tier_label = self._tier_label(observed_tpm, cfg)
        if observed_tpm is not None:
            result.details["tpm_limit"] = observed_tpm
            result.remarks.append(f"TPM 上限: {int(observed_tpm):,}")
        if observed_rpm_hdr is not None:
            result.details["rpm_limit_header"] = observed_rpm_hdr
            result.remarks.append(f"RPM 上限(头): {int(observed_rpm_hdr):,}")
        if tier_label is not None:
            result.details["tier_label"] = tier_label
            result.remarks.append(f"账户等级: {tier_label}")
        else:
            result.remarks.append("账户等级: 未知 (无 TPM 头)")

        # The chat probe also proves liveness — mark alive regardless of tier.
        result.alive = True

        # --- Step 3: image rpm ceiling via capped burst ---------------------
        await ctx.progress(0.45, "压测图像生成速率…")
        rpm, image_warn = await self._image_burst(key, ctx, cfg)
        bucket = self.rpm_bucket(rpm, cfg)
        result.details["image_rpm"] = rpm
        result.details["image_rpm_bucket"] = bucket
        result.remarks.append(f"图像 rpm≈{round(rpm)} -> 桶 {bucket}")
        if image_warn:
            result.remarks.append(f"⚠ {image_warn}")

        # --- Compose final tier label ---------------------------------------
        label_prefix = tier_label if tier_label is not None else "T?"
        result.tier = f"{label_prefix}-r{round(rpm)}"
        result.status = KeyStatus.GRADED
        result.progress_label = result.tier
        await ctx.progress(1.0, result.tier)

    # ------------------------------------------------------------------ #
    # grade sub-steps
    # ------------------------------------------------------------------ #
    async def _chat_probe(
        self, key: str, ctx: CheckContext, cfg: OpenAIConfig
    ) -> ProbeOutcome:
        """POST a minimal chat completion to surface ``x-ratelimit-*`` headers.

        Retries with ``max_completion_tokens`` if the model rejects the legacy
        ``max_tokens`` parameter (400 / unsupported_parameter)."""
        url = f"{cfg.base_url}/chat/completions"
        headers = self._auth_headers(key)
        base_payload: dict[str, Any] = {
            "model": cfg.probe_model,
            "messages": [{"role": "user", "content": "hi"}],
        }

        # First attempt: legacy max_tokens.
        outcome = await self._post_probe(
            ctx, url, headers, {**base_payload, "max_tokens": 1}
        )
        # Retry path: newer models only accept max_completion_tokens.
        if outcome.status_code == 400 and (outcome.detail or "").lower().find(
            "unsupported_parameter"
        ) != -1:
            outcome = await self._post_probe(
                ctx, url, headers, {**base_payload, "max_completion_tokens": 1}
            )
        return outcome

    async def _post_probe(
        self,
        ctx: CheckContext,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> ProbeOutcome:
        """One POST returning a classified ProbeOutcome with headers preserved."""
        resp, elapsed, exc = await timed_request(
            ctx.client, "POST", url, headers=headers, json=payload
        )
        outcome = base_outcome(resp, elapsed, exc)
        body = self._json(resp)
        outcome.error_class = (
            ErrorClass.NETWORK if exc is not None else self._classify(resp, body)
        )
        # Surface error.code so the retry heuristic can detect unsupported_parameter.
        code = self._error_code(body)
        if code:
            outcome.detail = code
        msg = self._error_message(body)
        if msg:
            outcome.body_snippet = msg[:200]
        return outcome

    async def _image_burst(
        self, key: str, ctx: CheckContext, cfg: OpenAIConfig
    ) -> tuple[float, str | None]:
        """Capped burst against image generation; returns (measured_rpm, warning).

        Honors ctx.full_load: when off, use the gentle_* settings to save cost.
        New/unverified orgs 429 instantly on images without verification — that is
        captured as a warning rather than a tier penalty."""
        url = f"{cfg.base_url}/images/generations"
        headers = self._auth_headers(key)
        payload: dict[str, Any] = {
            "model": cfg.image_model,
            "prompt": "a",
            "n": 1,
            "size": "1024x1024",
        }

        if ctx.full_load:
            target_rps = cfg.image_burst_rps
            duration_s = cfg.image_burst_seconds
            cap = cfg.image_burst_cap
        else:
            target_rps = cfg.gentle_image_rps
            cap = cfg.gentle_image_cap
            # Keep the gentle burst short; cover the cap at the gentle rate.
            duration_s = max(cap / target_rps if target_rps > 0 else 1.0, 1.0)

        # Track whether any 429 we saw was actually a billing/verification block.
        billing_seen: dict[str, bool] = {"hit": False, "msg": False}

        async def probe() -> ProbeOutcome:
            resp, elapsed, exc = await timed_request(
                ctx.client, "POST", url, headers=headers, json=payload
            )
            outcome = base_outcome(resp, elapsed, exc)
            body = self._json(resp)
            outcome.error_class = (
                ErrorClass.NETWORK if exc is not None else self._classify(resp, body)
            )
            if outcome.error_class == ErrorClass.BILLING:
                billing_seen["hit"] = True
            msg = (self._error_message(body) or "").lower()
            if "verif" in msg:
                billing_seen["msg"] = True
            return outcome

        async def on_tick(frac: float) -> None:
            # Map burst progress into the 0.45..0.95 band of the overall check.
            await ctx.progress(0.45 + 0.5 * frac, "压测图像生成速率…")

        burst = await run_burst(
            probe,
            target_rps=target_rps,
            duration_s=duration_s,
            cap=cap,
            stop_on_first_429=True,
        )
        # Drive progress to the end of the burst band.
        await ctx.progress(0.95, "汇总结果…")

        rpm = burst.measured_rpm if burst.measured_rpm is not None else 0.0

        warning: str | None = None
        if billing_seen["hit"]:
            warning = "图像生成被拒: insufficient_quota/账单问题"
        elif billing_seen["msg"]:
            warning = "图像生成需要组织验证 (verification required)"
        return float(rpm), warning

    # ------------------------------------------------------------------ #
    # result shaping helpers
    # ------------------------------------------------------------------ #
    def _record_org(self, result: KeyResult, body: dict[str, Any]) -> None:
        """Extract org title/id from a ``/v1/me`` body and add a remark."""
        orgs = body.get("orgs")
        data = orgs.get("data") if isinstance(orgs, dict) else orgs
        title: str | None = None
        org_id: str | None = None
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                title = first.get("title") or first.get("name")
                org_id = first.get("id")
        if title:
            suffix = f" ({org_id})" if org_id else ""
            result.details["organization_name"] = title
            if org_id:
                result.details["organization_id"] = org_id
            result.remarks.append(f"组织: {title}{suffix}")
        else:
            result.remarks.append("个人账户")

    def _apply_failure(
        self,
        result: KeyResult,
        klass: ErrorClass,
        resp: httpx.Response | None,
        body: dict[str, Any],
    ) -> None:
        """Translate a non-OK classification into a KeyResult verdict."""
        msg = self._error_message(body)
        code = self._error_code(body)
        if resp is not None:
            result.details["http_status"] = resp.status_code
        if code:
            result.details["error_code"] = code

        if klass in (ErrorClass.AUTH, ErrorClass.BILLING, ErrorClass.PERMISSION):
            # A definitive verdict that the key cannot be used.
            result.status = KeyStatus.DEAD
            result.alive = False
            label = {
                ErrorClass.AUTH: "无效密钥",
                ErrorClass.BILLING: "余额/配额不足",
                ErrorClass.PERMISSION: "权限不足",
            }[klass]
            result.progress_label = label
            result.remarks.append(f"{label}: {msg or code or klass.value}")
        elif klass == ErrorClass.NETWORK:
            result.status = KeyStatus.ERROR
            result.error = msg or "网络错误"
            result.progress_label = "网络错误"
            result.remarks.append("网络错误，无法判定")
        else:
            # 5xx / unknown — transient, not a verdict on the key.
            result.status = KeyStatus.ERROR
            result.error = msg or klass.value
            result.progress_label = "检测失败"
            result.remarks.append(f"检测失败 ({klass.value}): {msg or code or ''}")

    def _apply_failure_from_outcome(
        self, result: KeyResult, outcome: ProbeOutcome
    ) -> None:
        """Same as _apply_failure but sourced from a ProbeOutcome (burst path)."""
        if outcome.status_code is not None:
            result.details["http_status"] = outcome.status_code
        klass = outcome.error_class
        msg = outcome.body_snippet or outcome.detail
        if klass in (ErrorClass.AUTH, ErrorClass.BILLING, ErrorClass.PERMISSION):
            result.status = KeyStatus.DEAD
            result.alive = False
            label = {
                ErrorClass.AUTH: "无效密钥",
                ErrorClass.BILLING: "余额/配额不足",
                ErrorClass.PERMISSION: "权限不足",
            }[klass]
            result.progress_label = label
            result.remarks.append(f"{label}: {msg or klass.value}")
        else:
            result.status = KeyStatus.ERROR
            result.error = msg or klass.value
            result.progress_label = "检测失败"
            result.remarks.append(f"检测失败 ({klass.value})")

    @staticmethod
    def _to_float(value: str | None) -> float | None:
        """Parse a numeric header value, tolerating ``k``/``m`` suffixes."""
        if value is None:
            return None
        s = value.strip().lower()
        if not s:
            return None
        try:
            mult = 1.0
            if s.endswith("k"):
                mult, s = 1_000.0, s[:-1]
            elif s.endswith("m"):
                mult, s = 1_000_000.0, s[:-1]
            return float(s) * mult
        except ValueError:
            return None


# Module-level instance — the registry auto-discovers this attribute.
PLUGIN = OpenAIPlugin()
