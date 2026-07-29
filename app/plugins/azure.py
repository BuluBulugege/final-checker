"""Azure OpenAI / Azure AI Services plugin.

Detects credentials in ``URL|API_KEY`` format where the URL contains
``.openai.azure.com`` or ``.services.ai.azure.com``. The parser auto-combines
an Azure URL line followed by a key line into this format.

HEALTH: GET /openai/deployments proves the key can talk to Azure.

GRADE:
  1) List all deployments — model name, deployment ID, scale settings
  2) Probe each deployment with a minimal chat completion
  3) Read ``x-ratelimit-limit-tokens`` / ``x-ratelimit-limit-requests`` for TPM/RPM
  4) Report per-deployment: model mapping, TPM, RPM, status
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlparse

from app.http_util import timed_request
from app.models import ErrorClass, KeyResult, KeyStatus
from app.plugins.base import CheckContext, CheckerPlugin
from app.redact import redact

_AZURE_DOMAINS = (
    ".openai.azure.com",
    ".services.ai.azure.com",
    ".cognitiveservices.azure.com",
)

_FOUNDRY_DOMAINS = (".services.ai.azure.com",)

_API_VERSIONS = ["2024-10-21", "2024-06-01", "2023-12-01-preview"]

_PRIORITY_MODELS = [
    "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra",
    "gpt-5.5", "gpt-5.4", "gpt-5.4-pro", "gpt-5.4-mini", "gpt-5.4-nano",
    "gpt-5.3-chat", "gpt-5.3-codex",
    "gpt-5.2", "gpt-5.2-chat", "gpt-5.2-codex",
    "gpt-5.1", "gpt-5.1-chat", "gpt-5.1-codex", "gpt-5.1-codex-max",
    "gpt-5", "gpt-5-chat", "gpt-5-pro", "gpt-5-mini", "gpt-5-nano",
    "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
    "gpt-4o", "gpt-4o-mini",
    "o4-mini", "o3", "o3-mini", "o1", "o1-mini",
    "gpt-4-turbo", "gpt-4", "gpt-35-turbo",
    "gpt-image-2", "gpt-image-1.5", "gpt-image-1",
    "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-4-5",
    "grok-4.5", "grok-4.3", "grok-4-fast-reasoning", "grok-3",
    "DeepSeek-R1", "DeepSeek-V3.2", "DeepSeek-V4-Pro", "DeepSeek-V4-Flash",
    "model-router",
]


class AzurePlugin(CheckerPlugin):
    name = "azure"

    def matches(self, key: str) -> bool:
        k = key.strip()
        if "|" not in k:
            return False
        url_part = k.split("|", 1)[0].strip()
        try:
            parsed = urlparse(url_part)
            host = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme.lower() == "https"
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
            and any(host.endswith(domain) for domain in _AZURE_DOMAINS)
            and bool(k.split("|", 1)[1].strip())
        )

    @staticmethod
    def _parse(key: str) -> tuple[str, str]:
        parts = key.strip().split("|", 1)
        raw_url = parts[0].strip()
        api_key = parts[1].strip() if len(parts) > 1 else ""
        parsed = urlparse(raw_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        return base, api_key

    @staticmethod
    def _is_foundry(base_url: str) -> bool:
        host = urlparse(base_url).hostname or ""
        return any(host.endswith(d) for d in _FOUNDRY_DOMAINS)

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"api-key": api_key, "Content-Type": "application/json"}

    async def _get_json(
        self, ctx: CheckContext, url: str, api_key: str
    ) -> tuple[int | None, Any | None, str | None]:
        resp, _, exc = await timed_request(
            ctx.client, "GET", url, headers=self._headers(api_key)
        )
        if exc is not None or resp is None:
            return None, None, redact(repr(exc)) if exc else "no response"
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code == 200:
            return 200, data, None
        err_msg = None
        if isinstance(data, dict):
            err = data.get("error", {})
            err_msg = err.get("message") if isinstance(err, dict) else str(err) if err else None
        return resp.status_code, data, err_msg or f"HTTP {resp.status_code}"

    async def _list_deployments(
        self, ctx: CheckContext, base_url: str, api_key: str
    ) -> tuple[list[dict], str | None]:
        for api_ver in _API_VERSIONS:
            url = f"{base_url}/openai/deployments?api-version={api_ver}"
            status, data, err = await self._get_json(ctx, url, api_key)
            if status == 200 and isinstance(data, dict):
                items = data.get("data", [])
                if isinstance(items, list):
                    return items, None
        return [], err

    async def _list_models(
        self, ctx: CheckContext, base_url: str, api_key: str
    ) -> list[dict]:
        for api_ver in _API_VERSIONS[:2]:
            url = f"{base_url}/openai/models?api-version={api_ver}"
            status, data, _ = await self._get_json(ctx, url, api_key)
            if status == 200 and isinstance(data, dict):
                items = data.get("data", [])
                if isinstance(items, list):
                    return items
        return []

    async def _probe_deployment(
        self, ctx: CheckContext, base_url: str, api_key: str, deployment_id: str
    ) -> dict[str, Any]:
        """Try classic Azure OpenAI deployment path first, then foundry path."""
        # --- classic: /openai/deployments/{id}/chat/completions ---
        for api_ver in _API_VERSIONS[:2]:
            url = (
                f"{base_url}/openai/deployments/{deployment_id}"
                f"/chat/completions?api-version={api_ver}"
            )
            body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
            resp, elapsed, exc = await timed_request(
                ctx.client, "POST", url, headers=self._headers(api_key), json=body,
            )
            if exc is not None or resp is None:
                continue

            result = self._read_probe_response(resp, elapsed)
            if result.get("alive"):
                return result
            if resp.status_code == 401:
                return result

        # --- foundry: /openai/v1/chat/completions with model in body ---
        if self._is_foundry(base_url):
            return await self._probe_foundry(ctx, base_url, api_key, deployment_id)

        return {"alive": False, "error": "所有 API 版本均失败"}

    async def _probe_foundry(
        self, ctx: CheckContext, base_url: str, api_key: str, model: str
    ) -> dict[str, Any]:
        """Probe Azure AI Foundry endpoint — try Responses API first, then chat/completions."""
        # Responses API (the only path that works for some Foundry deployments)
        result = await self._probe_responses(ctx, base_url, api_key, model)
        if result.get("alive"):
            return result

        # Fallback: OpenAI-compatible chat/completions
        url = f"{base_url}/openai/v1/chat/completions"
        body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
        resp, elapsed, exc = await timed_request(
            ctx.client, "POST", url, headers=self._headers(api_key), json=body,
        )
        if exc is not None or resp is None:
            return result  # return the responses API result (has the error)
        return self._read_probe_response(resp, elapsed)

    async def _probe_responses(
        self, ctx: CheckContext, base_url: str, api_key: str, model: str
    ) -> dict[str, Any]:
        """Probe via /openai/v1/responses (OpenAI Responses API)."""
        url = f"{base_url}/openai/v1/responses"
        body = {"model": model, "input": "Say OK"}
        resp, elapsed, exc = await timed_request(
            ctx.client, "POST", url, headers=self._headers(api_key), json=body,
            timeout=60.0,
        )
        if exc is not None or resp is None:
            return {"alive": False, "error": redact(repr(exc)) if exc else "no response"}

        result: dict[str, Any] = {
            "status": resp.status_code,
            "latency_ms": round(elapsed, 1),
            "api": "responses",
        }
        h = {k.lower(): v for k, v in resp.headers.items()}
        for hdr in (
            "x-ratelimit-limit-tokens", "x-ratelimit-limit-requests",
            "x-ratelimit-remaining-tokens", "x-ratelimit-remaining-requests",
        ):
            val = h.get(hdr)
            if val:
                result[hdr] = val

        if resp.status_code == 200:
            result["alive"] = True
            try:
                data = resp.json()
                result["model"] = data.get("model")
                output = data.get("output", [])
                text = ""
                for item in output:
                    if isinstance(item, dict) and item.get("type") == "message":
                        for c in item.get("content", []):
                            text += c.get("text", "")
                if text:
                    result["content"] = text[:40]
            except ValueError:
                pass
            return result

        try:
            err_body = resp.json()
            err = err_body.get("error", {})
            if isinstance(err, dict):
                result["error"] = err.get("message", err.get("code", ""))
            elif isinstance(err, str):
                result["error"] = err
        except ValueError:
            pass
        result["alive"] = False
        return result

    @staticmethod
    def _read_probe_response(resp, elapsed) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": resp.status_code,
            "latency_ms": round(elapsed, 1),
        }
        h = {k.lower(): v for k, v in resp.headers.items()}
        for hdr in (
            "x-ratelimit-limit-tokens", "x-ratelimit-limit-requests",
            "x-ratelimit-remaining-tokens", "x-ratelimit-remaining-requests",
        ):
            val = h.get(hdr)
            if val:
                result[hdr] = val

        if resp.status_code == 200:
            result["alive"] = True
            try:
                result["model"] = resp.json().get("model")
            except ValueError:
                pass
            return result

        try:
            err_body = resp.json()
            err = err_body.get("error", {})
            if isinstance(err, dict):
                result["error"] = err.get("message", err.get("code", ""))
            elif isinstance(err, str):
                result["error"] = err
        except ValueError:
            pass
        result["alive"] = False
        return result

    # ------------------------------------------------------------------ #
    # health
    # ------------------------------------------------------------------ #
    async def health_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        base_url, api_key = self._parse(key)
        await ctx.progress(0.1, "连接 Azure…")

        deployments, err = await self._list_deployments(ctx, base_url, api_key)
        if deployments:
            result.status = KeyStatus.ALIVE
            result.alive = True
            result.remarks.append(f"Azure: {len(deployments)} 个部署")
            result.remarks.append(f"端点: {base_url}")
            result.progress_label = "存活"
        elif err and any(w in str(err).lower() for w in ("401", "403", "invalid", "denied")):
            result.status = KeyStatus.DEAD
            result.alive = False
            result.error = err
            result.progress_label = "密钥无效"
        else:
            models = await self._list_models(ctx, base_url, api_key)
            if models:
                result.status = KeyStatus.ALIVE
                result.alive = True
                result.remarks.append(f"Azure: {len(models)} 个模型")
                result.progress_label = "存活"
            else:
                result.status = KeyStatus.ERROR
                result.error = err or "无法验证"
                result.progress_label = "未知"
        await ctx.progress(1.0, result.progress_label)

    # ------------------------------------------------------------------ #
    # grade
    # ------------------------------------------------------------------ #
    async def grade_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        base_url, api_key = self._parse(key)
        result.details["endpoint"] = base_url

        # 1 — list deployments
        await ctx.progress(0.05, "列出部署…")
        deployments, dep_err = await self._list_deployments(ctx, base_url, api_key)

        if not deployments and dep_err:
            if any(w in str(dep_err).lower() for w in ("401", "403", "invalid", "denied")):
                result.status = KeyStatus.DEAD
                result.alive = False
                result.error = dep_err
                result.progress_label = "密钥无效"
                await ctx.progress(1.0, result.progress_label)
                return

        # also try models list
        await ctx.progress(0.10, "列出模型…")
        models = await self._list_models(ctx, base_url, api_key)

        result.remarks.append(f"端点: {base_url}")

        # parse deployment info
        dep_infos: list[dict[str, Any]] = []
        for dep in deployments:
            if not isinstance(dep, dict):
                continue
            dep_id = dep.get("id") or dep.get("deployment_id") or ""
            model = dep.get("model") or ""
            status = dep.get("status") or dep.get("state") or "unknown"
            scale = dep.get("scale_settings") or dep.get("sku") or {}
            cap = scale.get("capacity") if isinstance(scale, dict) else None
            scale_type = scale.get("scale_type") or scale.get("name") if isinstance(scale, dict) else None
            dep_infos.append({
                "id": dep_id, "model": model, "status": status,
                "scale_type": scale_type, "capacity_ktpm": cap,
            })

        if dep_infos:
            result.details["deployments"] = dep_infos
            result.remarks.append(f"部署: {len(dep_infos)} 个")
            for info in dep_infos:
                cap = info.get("capacity_ktpm")
                cap_str = f", {cap}K TPM" if cap else ""
                result.remarks.append(
                    f"  📦 {info['id']} → {info['model']} ({info['status']}{cap_str})"
                )
        elif models:
            result.remarks.append(f"模型 (无部署列表): {len(models)} 个")
        else:
            result.remarks.append(f"部署列表不可用: {dep_err or '?'}")

        # 2 — probe each active deployment
        active = [d for d in dep_infos if d.get("status") in ("succeeded", "running") and d.get("id")]
        if not active:
            names: list[str] = []
            if self._is_foundry(base_url):
                # Foundry deployments use short names (no date suffix).
                # Build candidates: priority list + stripped catalog names.
                model_ids = [m.get("id") for m in models if isinstance(m, dict) and m.get("id")]
                seen: set[str] = set()
                for pm in _PRIORITY_MODELS:
                    if pm not in seen:
                        names.append(pm)
                        seen.add(pm)
                # Strip date suffixes from catalog (e.g. "gpt-5.6-sol-2026-07-09" → "gpt-5.6-sol")
                import re
                _DATE_RE = re.compile(r"-\d{4}-\d{2}-\d{2}(-\w+)?$")
                for mid in model_ids:
                    short = _DATE_RE.sub("", mid)
                    if short not in seen and short != mid:
                        names.append(short)
                        seen.add(short)
                    if len(names) >= 50:
                        break
            else:
                for m in models:
                    mid = m.get("id") if isinstance(m, dict) else None
                    if mid:
                        names.append(mid)
            if not names:
                names = list(_PRIORITY_MODELS)
            active = [{"id": n, "model": n} for n in names]
            if not dep_infos:
                result.remarks.append(f"探测 {len(active)} 个模型…")

        any_alive = False
        probes: dict[str, Any] = {}
        total = len(active)
        sem = asyncio.Semaphore(5)

        async def probe_one(dep: dict, idx: int):
            async with sem:
                dep_id = dep["id"]
                await ctx.progress(0.15 + 0.75 * (idx / max(total, 1)), f"测试 {dep_id}…")
                return dep_id, await self._probe_deployment(ctx, base_url, api_key, dep_id)

        results = await asyncio.gather(
            *(probe_one(d, i) for i, d in enumerate(active)),
            return_exceptions=True,
        )

        for item in results:
            if isinstance(item, Exception):
                continue
            dep_id, probe = item
            probes[dep_id] = probe
            if probe.get("alive"):
                any_alive = True
                tpm = probe.get("x-ratelimit-limit-tokens", "?")
                rpm = probe.get("x-ratelimit-limit-requests", "?")
                resp_model = probe.get("model", "?")
                api_tag = f" [{probe.get('api')}]" if probe.get("api") else ""
                result.remarks.append(f"  ✓ {dep_id} → {resp_model} | TPM={tpm} RPM={rpm}{api_tag}")

        failed_count = sum(1 for p in probes.values() if not p.get("alive"))
        if failed_count:
            result.remarks.append(f"  (另有 {failed_count} 个模型未部署)")

        result.details["probes"] = probes

        # 3 — finalize
        working = sum(1 for p in probes.values() if p.get("alive"))
        if any_alive:
            result.status = KeyStatus.GRADED
            result.alive = True
            result.tier = f"Azure-{working}部署"
        elif dep_infos or models:
            result.status = KeyStatus.GRADED
            result.alive = True
            result.tier = "Azure-无活跃部署"
        else:
            result.status = KeyStatus.ERROR
            result.error = "无法确定部署状态"

        result.progress_label = result.tier or "Azure"
        report = {"endpoint": base_url, "deployments": dep_infos, "models": [m for m in models if isinstance(m, dict)], "probes": probes}
        result.download_filename = "azure-report.json"
        result.download_text = json.dumps(report, ensure_ascii=False, indent=2)
        result.remarks.append("完整信息见下载报告 ↓")
        await ctx.progress(1.0, result.progress_label)


PLUGIN = AzurePlugin()
