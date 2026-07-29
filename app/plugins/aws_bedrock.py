"""AWS Bedrock plugin — IAM credentials (AKIA…:SECRET).

Detects AWS IAM access keys in ``AKIA...:SECRET_ACCESS_KEY`` format (with optional
``:REGION`` suffix). The parser auto-combines ``AWS_ACCESS_KEY_ID=`` /
``AWS_SECRET_ACCESS_KEY=`` env-var pairs into this format.

Uses **pure-Python AWS SigV4 signing** (hmac/hashlib — no boto3 dependency) so
the checker stays lightweight. All HTTP goes through the shared httpx client.

HEALTH: STS GetCallerIdentity verifies the credentials.

GRADE:
  1) STS GetCallerIdentity — account ID, ARN, user info
  2) ListFoundationModels across configured regions
  3) Converse API probe on priority models (Claude, Grok, Llama, Mistral …)
  4) Report: identity, per-provider model availability, working vs. denied
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import re
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote, urlparse

from app.config import AWSBedrockConfig
from app.http_util import timed_request
from app.models import KeyResult, KeyStatus
from app.plugins.base import CheckContext, CheckerPlugin, PluginMeta
from app.redact import redact

# ---------------------------------------------------------------------------
# Pure-Python AWS SigV4
# ---------------------------------------------------------------------------

def _hmac256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k = _hmac256(("AWS4" + secret).encode("utf-8"), datestamp)
    k = _hmac256(k, region)
    k = _hmac256(k, service)
    return _hmac256(k, "aws4_request")


def _sigv4_headers(
    method: str,
    url: str,
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return headers dict with SigV4 Authorization.

    The ``host`` header is used for signing but NOT included in the returned
    dict — httpx sets it from the URL automatically.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port and parsed.port not in (80, 443):
        host = f"{host}:{parsed.port}"

    now = datetime.datetime.utcnow()
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    # ---- headers used in the signature ----
    sign_h: dict[str, str] = {}
    if extra_headers:
        for k, v in extra_headers.items():
            sign_h[k.lower()] = v
    sign_h["host"] = host
    sign_h["x-amz-date"] = amzdate

    signed_keys = sorted(sign_h)
    canonical_headers = "".join(f"{k}:{sign_h[k].strip()}\n" for k in signed_keys)
    signed_headers_str = ";".join(signed_keys)

    # ---- canonical request ----
    # SigV4 requires URI-encoding each path segment; for non-S3 services
    # this means double-encoding (already-encoded %XX becomes %25XX).
    raw_path = parsed.path or "/"
    canonical_uri = "/".join(
        quote(seg, safe="-_.~") for seg in raw_path.split("/")
    )
    canonical_qs = ""
    if parsed.query:
        pairs = []
        for part in parsed.query.split("&"):
            kv = part.split("=", 1)
            pairs.append((kv[0], kv[1] if len(kv) > 1 else ""))
        pairs.sort()
        canonical_qs = "&".join(f"{k}={v}" for k, v in pairs)

    creq = "\n".join([
        method.upper(), canonical_uri, canonical_qs,
        canonical_headers, signed_headers_str, payload_hash,
    ])

    # ---- string to sign ----
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    sts = "\n".join([
        "AWS4-HMAC-SHA256", amzdate, scope,
        hashlib.sha256(creq.encode("utf-8")).hexdigest(),
    ])

    sig = hmac.new(
        _signing_key(secret_key, datestamp, region, service),
        sts.encode("utf-8"), hashlib.sha256,
    ).hexdigest()

    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers_str}, Signature={sig}"
    )

    # Return everything except host (httpx handles Host)
    out: dict[str, str] = {}
    for k, v in sign_h.items():
        if k != "host":
            out[k] = v
    out["authorization"] = auth
    return out


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AK_RE = re.compile(r"^AKIA[0-9A-Z]{16}$")

# Providers to prioritize when testing models
_PRIO_PROVIDERS = ("anthropic", "xai", "meta", "mistral")

# Claude models to test across all available regions
_CLAUDE_MODELS = [
    "anthropic.claude-fable-5",
    "anthropic.claude-opus-4-8",
    "anthropic.claude-opus-4-7",
    "anthropic.claude-opus-4-6-v1",
    "anthropic.claude-opus-4-5-20251101-v1:0",
    "anthropic.claude-sonnet-5",
    "anthropic.claude-sonnet-4-6",
    "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
]

# INFERENCE_PROFILE models need a cross-region inference profile ARN
_INFERENCE_PROFILE_REGIONS = {
    "us": "us",
    "eu": "eu",
    "ap": "ap",
}


def foundation_model_arn(model_id: str) -> str:
    """Convert model ID to foundation-model ARN path component."""
    return f"foundation-model/{model_id}"


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class AWSBedrockPlugin(CheckerPlugin):
    meta = PluginMeta(
        name="aws_bedrock",
        version="1.0.0",
        description="AWS Bedrock（IAM 凭证 SigV4）身份验证 + 跨区域模型可用性探测",
        key_format_hint="AWS IAM（AKIA…:<secret>[:region] 或 AWS_ACCESS_KEY_ID=/AWS_SECRET_ACCESS_KEY= 环境变量对）",
        capabilities=["health", "grade"],
        priority=20,
    )

    # ---- config ----
    def _cfg(self, ctx: CheckContext) -> AWSBedrockConfig:
        return ctx.settings.aws_bedrock

    # ---- detection ----
    def matches(self, key: str) -> bool:
        k = key.strip()
        if ":" not in k:
            return False
        ak = k.split(":")[0]
        return bool(_AK_RE.match(ak))

    @staticmethod
    def _parse(key: str) -> tuple[str, str, str | None]:
        parts = key.strip().split(":")
        ak = parts[0]
        sk = parts[1] if len(parts) > 1 else ""
        region = parts[2] if len(parts) > 2 and parts[2] else None
        return ak, sk, region

    # ---- parsing / masking hooks ----
    def mask(self, key: str) -> str | None:
        """Show the (non-secret) access key id, never the secret."""
        k = key.strip()
        if k.startswith("AKIA") and ":" in k:
            return f"AWS:{k.split(':')[0]}"
        return None

    def stitch(self, lines: list[str], i: int) -> tuple[str | None, set[int]] | None:
        """Fold ``AWS_ACCESS_KEY_ID=X`` + ``AWS_SECRET_ACCESS_KEY=Y`` env-var
        pairs into one ``X:Y`` credential; swallow orphan secret lines."""
        line = lines[i].strip()
        if line.upper().startswith("AWS_ACCESS_KEY_ID="):
            ak = line.split("=", 1)[1].strip()
            for j in range(i + 1, min(i + 6, len(lines))):
                nxt = lines[j].strip()
                if nxt.startswith("#") or not nxt:
                    continue
                if nxt.upper().startswith("AWS_SECRET_ACCESS_KEY="):
                    sk_val = nxt.split("=", 1)[1].strip()
                    return f"{ak}:{sk_val}", {j}
            return line, set()
        if line.upper().startswith("AWS_SECRET_ACCESS_KEY="):
            return None, set()  # orphan secret line — consumed silently
        return None

    def extract_candidates(self, text: str) -> list[str] | None:
        """Expand the ``aws_iam_pairs`` array of an all_combos aggregate export
        into ``AKIA…:SECRET`` credentials. ASIA temporary credentials require a
        session token, which the aggregate schema does not provide — skipped."""
        if '"aws_iam_pairs"' not in text:
            return None
        try:
            obj = json.loads(text)
        except (ValueError, TypeError, RecursionError):
            return None
        if not isinstance(obj, dict):
            return None
        # A standalone service-account key remains one credential even if it
        # carries an unrelated metadata field named like the aggregate array.
        if obj.get("type") == "service_account" or (
            obj.get("private_key") and obj.get("client_email")
        ):
            return None
        rows = obj.get("aws_iam_pairs")
        if not isinstance(rows, list):
            return None
        credentials: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            access_key = str(row.get("access_key_id") or "").strip()
            secret_key = str(row.get("secret_access_key") or "").strip()
            if not (_AK_RE.fullmatch(access_key) and secret_key):
                continue
            credentials.append(f"{access_key}:{secret_key}")
        return credentials

    # ---- signed HTTP helpers ----
    async def _aws(
        self, ctx: CheckContext, method: str, url: str, body: bytes,
        ak: str, sk: str, region: str, service: str,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> tuple[int | None, bytes | None, str | None]:
        headers = _sigv4_headers(method, url, body, ak, sk, region, service, extra_headers)
        kw: dict[str, Any] = {"headers": headers, "timeout": timeout}
        if method.upper() != "GET":
            kw["content"] = body
        resp, _, exc = await timed_request(ctx.client, method, url, **kw)
        if exc is not None or resp is None:
            return None, None, str(exc) if exc else "no response"
        return resp.status_code, resp.content, None

    # ---- STS identity ----
    async def _sts_identity(
        self, ctx: CheckContext, ak: str, sk: str,
    ) -> dict[str, str] | None:
        url = "https://sts.amazonaws.com/"
        body = b"Action=GetCallerIdentity&Version=2011-06-15"
        status, content, _ = await self._aws(
            ctx, "POST", url, body, ak, sk, "us-east-1", "sts",
            extra_headers={"content-type": "application/x-www-form-urlencoded"},
        )
        if status != 200 or not content:
            return None
        return self._parse_sts_xml(content)

    @staticmethod
    def _parse_sts_xml(content: bytes) -> dict[str, str] | None:
        text = content.decode("utf-8", errors="replace")
        out: dict[str, str] = {}
        for tag in ("Account", "Arn", "UserId"):
            m = re.search(rf"<{tag}[^>]*>([^<]+)</{tag}>", text)
            if m:
                out[tag] = m.group(1).strip()
        return out if out else None

    # ---- Bedrock: list foundation models ----
    async def _list_models(
        self, ctx: CheckContext, ak: str, sk: str, region: str,
    ) -> list[dict[str, Any]] | None:
        url = f"https://bedrock.{region}.amazonaws.com/foundation-models"
        status, content, _ = await self._aws(
            ctx, "GET", url, b"", ak, sk, region, "bedrock",
        )
        if status != 200 or not content:
            return None
        try:
            return json.loads(content).get("modelSummaries", [])
        except (ValueError, AttributeError):
            return None

    # ---- Bedrock: converse probe (tries direct + inference profile ARN) ----
    async def _test_converse(
        self, ctx: CheckContext, ak: str, sk: str, region: str, model_id: str,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        # First try direct model ID
        result = await self._converse_call(ctx, ak, sk, region, model_id)
        if result.get("ok"):
            return result

        # If "invalid model identifier" or "not allowed", try inference profile ARN
        err = result.get("error", "")
        if account_id and ("invalid" in err.lower() or "not allowed" in err.lower()
                           or "not authorized" in err.lower()):
            # Determine geo prefix from region
            geo = region.split("-")[0]  # us, eu, ap
            if geo in ("us", "eu", "ap"):
                profile_arn = f"arn:aws:bedrock:{geo}::{foundation_model_arn(model_id)}"
                result2 = await self._converse_call(ctx, ak, sk, region, profile_arn)
                if result2.get("ok") or "invalid" not in result2.get("error", "").lower():
                    result2["inference_profile"] = True
                    return result2

        return result

    async def _converse_call(
        self, ctx: CheckContext, ak: str, sk: str, region: str, model_id: str,
    ) -> dict[str, Any]:
        encoded = quote(model_id, safe="")
        url = f"https://bedrock-runtime.{region}.amazonaws.com/model/{encoded}/converse"
        payload = json.dumps({
            "messages": [{"role": "user", "content": [{"text": "Say OK"}]}],
            "inferenceConfig": {"maxTokens": 10},
        }).encode("utf-8")

        start = time.monotonic()
        status, content, err = await self._aws(
            ctx, "POST", url, payload, ak, sk, region, "bedrock",
            extra_headers={"content-type": "application/json", "accept": "application/json"},
            timeout=60.0,
        )
        ms = round((time.monotonic() - start) * 1000, 1)

        if err:
            return {"ok": False, "error": redact(err)[:120], "latency_ms": ms}

        result: dict[str, Any] = {"status": status, "latency_ms": ms}
        if status == 200 and content:
            try:
                data = json.loads(content)
                parts = (data.get("output") or {}).get("message", {}).get("content", [])
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                result["ok"] = True
                result["content"] = text[:80]
                usage = data.get("usage")
                if usage:
                    result["usage"] = usage
            except (ValueError, KeyError, TypeError):
                result["ok"] = True
        else:
            result["ok"] = False
            if content:
                try:
                    result["error"] = json.loads(content).get("message", str(status))[:120]
                except (ValueError, AttributeError):
                    result["error"] = content[:200].decode("utf-8", errors="replace")
        return result

    # ---- Bedrock: get model access / throughput info ----
    async def _get_model_throughput(
        self, ctx: CheckContext, ak: str, sk: str, region: str, model_id: str,
    ) -> dict[str, Any] | None:
        """GET /foundation-models/{modelId} for quota/throughput details."""
        encoded = quote(model_id, safe="")
        url = f"https://bedrock.{region}.amazonaws.com/foundation-models/{encoded}"
        status, content, _ = await self._aws(
            ctx, "GET", url, b"", ak, sk, region, "bedrock",
        )
        if status != 200 or not content:
            return None
        try:
            return json.loads(content).get("modelDetails")
        except (ValueError, AttributeError):
            return None

    # ---- Bedrock: attempt to enable model access (e.g. fable-5 data sharing) ----
    async def _request_model_access(
        self, ctx: CheckContext, ak: str, sk: str, region: str, model_id: str,
    ) -> dict[str, Any]:
        """PUT /foundation-model-entitlement to request access (accepts data sharing)."""
        url = f"https://bedrock.{region}.amazonaws.com/foundation-model-entitlement"
        payload = json.dumps({"modelId": model_id}).encode("utf-8")
        status, content, err = await self._aws(
            ctx, "PUT", url, payload, ak, sk, region, "bedrock",
            extra_headers={"content-type": "application/json"},
        )
        if err:
            return {"ok": False, "error": err}
        if status and status < 300:
            return {"ok": True, "status": status}
        msg = ""
        if content:
            try:
                msg = json.loads(content).get("message", "")[:120]
            except (ValueError, AttributeError):
                msg = content[:120].decode("utf-8", errors="replace")
        return {"ok": False, "status": status, "error": msg}

    # ------------------------------------------------------------------ #
    # health
    # ------------------------------------------------------------------ #
    async def health_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        ak, sk, region = self._parse(key)
        await ctx.progress(0.1, "STS 身份验证…")
        identity = await self._sts_identity(ctx, ak, sk)
        if identity:
            result.status = KeyStatus.ALIVE
            result.alive = True
            result.remarks.append(f"账户: {identity.get('Account')}")
            result.remarks.append(f"ARN: {identity.get('Arn')}")
            result.progress_label = "存活"
        else:
            result.status = KeyStatus.DEAD
            result.alive = False
            result.error = "STS 验证失败"
            result.progress_label = "凭证无效"
        await ctx.progress(1.0, result.progress_label)

    # ------------------------------------------------------------------ #
    # grade
    # ------------------------------------------------------------------ #
    async def grade_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        ak, sk, region_hint = self._parse(key)
        cfg = self._cfg(ctx)
        regions = list(cfg.regions)
        if region_hint and region_hint not in regions:
            regions.insert(0, region_hint)
        max_test = cfg.max_models_per_region
        test_conc = cfg.test_concurrency

        # ---- Step 1: identity ----
        await ctx.progress(0.02, "STS 身份验证…")
        identity = await self._sts_identity(ctx, ak, sk)
        if not identity:
            result.status = KeyStatus.DEAD
            result.alive = False
            result.error = "STS 验证失败 — 凭证无效或已过期"
            result.progress_label = "凭证无效"
            await ctx.progress(1.0, result.progress_label)
            return

        result.alive = True
        result.details["identity"] = identity
        result.remarks.append(f"账户: {identity.get('Account')}")
        result.remarks.append(f"ARN: {identity.get('Arn')}")

        # ---- Step 2: enumerate models across regions ----
        await ctx.progress(0.08, "扫描区域模型…")
        sem = asyncio.Semaphore(6)

        async def scan_region(r: str):
            async with sem:
                return r, await self._list_models(ctx, ak, sk, r)

        region_tasks = await asyncio.gather(
            *(scan_region(r) for r in regions), return_exceptions=True,
        )

        models_by_region: dict[str, list[dict]] = {}
        all_ids: set[str] = set()
        for item in region_tasks:
            if isinstance(item, Exception):
                continue
            r, summaries = item
            if not summaries:
                continue
            active = [
                m for m in summaries
                if isinstance(m, dict)
                and (m.get("modelLifecycle") or {}).get("status") == "ACTIVE"
                and "ON_DEMAND" in (m.get("inferenceTypesSupported") or [])
            ]
            if active:
                models_by_region[r] = active
                for m in active:
                    mid = m.get("modelId", "")
                    if mid:
                        all_ids.add(mid)

        by_provider: dict[str, list[str]] = {}
        for mid in sorted(all_ids):
            prov = mid.split(".")[0] if "." in mid else "unknown"
            by_provider.setdefault(prov, []).append(mid)

        result.details["models_by_region"] = {
            r: [m.get("modelId") for m in ms] for r, ms in models_by_region.items()
        }
        result.details["by_provider"] = by_provider
        result.remarks.append(
            f"模型: {len(all_ids)} 个 (跨 {len(models_by_region)} 个区域)"
        )
        for prov, mids in sorted(by_provider.items()):
            result.remarks.append(f"  {prov}: {len(mids)} 个")

        best_region = region_hint or "us-east-1"
        if models_by_region:
            best_region = max(models_by_region, key=lambda r: len(models_by_region[r]))

        # ---- Step 3: deep Claude probe — test every Claude model in every region ----
        claude_regions = [r for r in models_by_region if any(
            m.get("modelId", "").startswith("anthropic.") for m in models_by_region[r]
        )]
        if not claude_regions:
            claude_regions = [best_region]

        # Collect all anthropic model IDs actually listed + our priority list
        listed_claude = set()
        for r, ms in models_by_region.items():
            for m in ms:
                mid = m.get("modelId", "")
                if mid.startswith("anthropic."):
                    listed_claude.add(mid)
        claude_to_test = list(listed_claude)
        for cm in _CLAUDE_MODELS:
            if cm not in listed_claude:
                claude_to_test.append(cm)

        await ctx.progress(0.20, f"深度测试 Claude ({len(claude_to_test)}模型 x {len(claude_regions)}区域)…")
        claude_sem = asyncio.Semaphore(test_conc)
        claude_results: dict[str, dict[str, Any]] = {}  # "model@region" -> probe

        async def test_claude(model_id: str, region: str):
            async with claude_sem:
                key = f"{model_id}@{region}"
                probe = await self._test_converse(ctx, ak, sk, region, model_id,
                                                   account_id=identity.get("Account"))
                claude_results[key] = probe
                return key, probe

        claude_tasks = []
        for cm in claude_to_test:
            for r in claude_regions:
                claude_tasks.append(test_claude(cm, r))

        await asyncio.gather(*claude_tasks, return_exceptions=True)

        # Organize Claude results: model -> {region -> probe}
        claude_by_model: dict[str, dict[str, Any]] = {}
        for key, probe in claude_results.items():
            mid, region = key.rsplit("@", 1)
            short = mid.split(".")[-1] if "." in mid else mid
            claude_by_model.setdefault(mid, {})[region] = probe

        # Try to enable fable-5 access if it failed with "Operation not allowed"
        fable_models = [m for m in claude_to_test if "fable" in m.lower()]
        for fm in fable_models:
            any_ok = any(
                claude_by_model.get(fm, {}).get(r, {}).get("ok")
                for r in claude_regions
            )
            if not any_ok:
                await ctx.progress(0.55, f"尝试开启 fable-5 数据共享…")
                for r in claude_regions[:3]:
                    access = await self._request_model_access(ctx, ak, sk, r, fm)
                    if access.get("ok"):
                        result.remarks.append(f"  → {fm} 数据共享已请求 ({r})")
                        # Re-test after enabling
                        probe = await self._test_converse(ctx, ak, sk, r, fm)
                        claude_by_model.setdefault(fm, {})[r] = probe
                        claude_results[f"{fm}@{r}"] = probe
                        if probe.get("ok"):
                            result.remarks.append(f"  → fable-5 开启成功!")
                            break
                    else:
                        err_msg = access.get("error", "")[:60]
                        if err_msg and "already" not in err_msg.lower():
                            result.remarks.append(f"  → fable-5 开启失败 ({r}): {err_msg}")

        result.remarks.append(f"\n--- Claude 模型详情 ---")
        for mid in sorted(claude_by_model):
            short = mid.replace("anthropic.", "")
            region_probes = claude_by_model[mid]
            ok_regions = [r for r, p in region_probes.items() if p.get("ok")]
            fail_regions = [r for r, p in region_probes.items() if not p.get("ok")]
            if ok_regions:
                for r in sorted(ok_regions):
                    p = region_probes[r]
                    ms = p.get("latency_ms", "?")
                    result.remarks.append(f"  ✓ {short} [{r}] {ms}ms")
            else:
                errs = set()
                for r in fail_regions:
                    errs.add(region_probes[r].get("error", "?")[:50])
                result.remarks.append(f"  ✗ {short}: {'; '.join(errs)}")

        # ---- Step 4: quick-test other priority providers in best region ----
        await ctx.progress(0.65, "测试其他模型…")
        other_test: list[str] = []
        for prov in _PRIO_PROVIDERS:
            if prov == "anthropic":
                continue
            prov_models = by_provider.get(prov, [])
            other_test.extend(sorted(prov_models, reverse=True)[:5])
        for prov, prov_models in sorted(by_provider.items()):
            if prov not in _PRIO_PROVIDERS:
                other_test.extend(sorted(prov_models, reverse=True)[:2])

        seen_other: set[str] = set()
        deduped_other: list[str] = []
        for m in other_test:
            if m not in seen_other:
                seen_other.add(m)
                deduped_other.append(m)
        other_test = deduped_other[:max_test]

        other_sem = asyncio.Semaphore(test_conc)

        async def test_other(model_id: str, idx: int):
            async with other_sem:
                await ctx.progress(
                    0.65 + 0.25 * (idx / max(len(other_test), 1)),
                    f"{model_id.split('.')[-1][:25]}…",
                )
                return model_id, await self._test_converse(ctx, ak, sk, best_region, model_id)

        other_results = await asyncio.gather(
            *(test_other(m, i) for i, m in enumerate(other_test)),
            return_exceptions=True,
        )

        working: dict[str, list[str]] = {}
        failed: dict[str, list[str]] = {}
        probes: dict[str, Any] = {}

        # Include Claude results from best region
        for mid in claude_by_model:
            best_probe = claude_by_model[mid].get(best_region, {})
            if not best_probe:
                best_probe = next(iter(claude_by_model[mid].values()), {})
            probes[mid] = best_probe
            prov = "anthropic"
            if best_probe.get("ok"):
                working.setdefault(prov, []).append(mid)
            else:
                failed.setdefault(prov, []).append(mid)

        for item in other_results:
            if isinstance(item, Exception):
                continue
            mid, probe = item
            probes[mid] = probe
            prov = mid.split(".")[0] if "." in mid else "unknown"
            if probe.get("ok"):
                working.setdefault(prov, []).append(mid)
            else:
                failed.setdefault(prov, []).append(mid)

        result.details["probes"] = probes
        result.details["claude_per_region"] = {
            k: {r: p for r, p in v.items()} for k, v in claude_by_model.items()
        }
        total_ok = sum(len(ms) for ms in working.values())
        total_fail = sum(len(ms) for ms in failed.values())

        result.remarks.append(f"\n--- 其他模型 ({best_region}) ---")
        result.remarks.append(f"实测: ✓ {total_ok} / ✗ {total_fail}")

        for prov in sorted(set(list(working) + list(failed))):
            if prov == "anthropic":
                continue
            for m in working.get(prov, []):
                p = probes.get(m, {})
                result.remarks.append(
                    f"  ✓ {m} ({p.get('latency_ms', '?')}ms)"
                )
            fail_count = len(failed.get(prov, []))
            if fail_count:
                result.remarks.append(f"  ✗ {prov}: {fail_count} 个不可用")

        # ---- finalize ----
        claude_ok = sum(1 for mid in claude_by_model
                        if any(p.get("ok") for p in claude_by_model[mid].values()))
        result.status = KeyStatus.GRADED
        result.tier = f"Bedrock-{total_ok}模型(Claude:{claude_ok})"
        result.progress_label = result.tier

        report = {
            "identity": identity,
            "regions_scanned": list(models_by_region.keys()),
            "test_region": best_region,
            "total_unique_models": len(all_ids),
            "by_provider": by_provider,
            "claude_per_region": {
                mid: {r: p for r, p in rps.items()}
                for mid, rps in claude_by_model.items()
            },
            "probes": probes,
            "working": working,
            "failed": failed,
        }
        acct = identity.get("Account", "unknown")
        result.download_filename = f"bedrock-{acct}.json"
        result.download_text = json.dumps(report, ensure_ascii=False, indent=2)
        result.remarks.append("完整信息见下载报告 ↓")
        await ctx.progress(1.0, result.progress_label)


PLUGIN = AWSBedrockPlugin()
