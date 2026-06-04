"""GCP service-account checker plugin.

Detects a Google Cloud service-account JSON key (``{"type":"service_account",
...}``), exchanges it for an OAuth2 access token (RS256 JWT-bearer grant, signed
offline with google-auth, exchanged via httpx), then scans the project:

  * Vertex AI models across all locations (+ best-effort RPM/TPM from Cloud Quotas)
  * Compute Engine instances (aggregated across zones)
  * Databases: Cloud SQL, AlloyDB, Spanner, Firestore
  * Project overview: metadata, enabled APIs, billing, IAM permissions

GCP has no single "tier"; everything goes into remarks, and the FULL machine
report is attached as a downloadable JSON (``result.download_text``) so the UI
can offer a 下载 button when the remarks can't hold it all.

Grade mode runs the full scan. Health mode just proves the key can mint a token
and reach one API.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.http_util import timed_request
from app.models import ErrorClass, KeyResult, KeyStatus
from app.plugins.base import CheckContext, CheckerPlugin
from app.redact import redact

TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def fmt(count: int | None) -> str:
    """Render a resource count, showing '无权限' instead of a misleading 0 when
    the listing was denied (count is None)."""
    return "无权限" if count is None else str(count)


# A compact, well-supported set of Vertex regions to sweep. Configurable via
# settings.gcp.locations; we also try to discover the live list at runtime.
DEFAULT_LOCATIONS = [
    "us-central1",
    "us-east1",
    "us-east4",
    "us-east5",
    "us-west1",
    "us-west4",
    "europe-west1",
    "europe-west4",
    "europe-west2",
    "asia-southeast1",
    "asia-northeast1",
    "asia-east1",
]


class GCPPlugin(CheckerPlugin):
    name = "gcp"

    # ------------------------------------------------------------------ #
    # detection
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize(key: str) -> str:
        """Aggressively clean a pasted key of editor/clipboard artifacts so it
        parses as JSON: strip BOM and zero-width chars, map every smart-quote
        variant to straight quotes, and turn non-breaking spaces into spaces."""
        k = key.strip()
        # BOM + zero-width + word-joiner + nbsp variants
        for junk in ("﻿", "​", "‌", "‍", "⁠"):
            k = k.replace(junk, "")
        k = k.replace(" ", " ").replace(" ", " ")
        # smart / typographic double quotes -> straight ASCII quote
        for q in ("“", "”", "„", "‟", "″", "〃", "＂"):
            k = k.replace(q, '"')
        return k.strip()

    def matches(self, key: str) -> bool:
        """Claim anything that structurally looks like a GCP service-account key.

        Detection is intentionally LENIENT: we route a service-account-shaped
        JSON block to this plugin even if it has a minor JSON flaw, so the user
        gets a precise error from the parse/token step instead of a useless
        'no plugin matched'. The strict parse happens later in _load_info.
        """
        k = self._normalize(key)
        if not (k.startswith("{") and k.endswith("}")):
            return False
        # Fast path: valid JSON with the right type.
        try:
            obj = json.loads(k)
            if isinstance(obj, dict):
                if obj.get("type") == "service_account":
                    return True
                # tolerate keys that omit "type" but clearly are SA keys
                if obj.get("private_key") and obj.get("client_email"):
                    return True
                return False
        except (ValueError, TypeError):
            pass
        # Fallback: structural sniff for a service-account block that didn't
        # cleanly parse (stray char, trailing comma, etc.). All three markers
        # present => it's a GCP key with a defect, claim it and report the flaw.
        low = k
        return (
            '"service_account"' in low
            and '"private_key"' in low
            and '"client_email"' in low
        )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_info(key: str) -> dict[str, Any]:
        """Tolerant parse: normalize artifacts, then try strict JSON; on failure
        strip a trailing comma before the closing brace (a common paste defect)
        and retry once."""
        k = GCPPlugin._normalize(key)
        try:
            return json.loads(k)
        except ValueError:
            import re

            repaired = re.sub(r",(\s*[}\]])", r"\1", k)  # drop trailing commas
            return json.loads(repaired)

    async def _mint_token(
        self, info: dict[str, Any], ctx: CheckContext
    ) -> tuple[str | None, ErrorClass, str | None]:
        """Sign a JWT assertion offline with google-auth and exchange it for an
        access token via httpx. Returns (token, error_class, detail)."""
        try:
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_info(
                info, scopes=[SCOPE]
            )
            # Build the RS256-signed JWT-bearer assertion offline (no network).
            assertion = creds._make_authorization_grant_assertion()
            if isinstance(assertion, bytes):
                assertion = assertion.decode("ascii")
        except Exception as exc:  # malformed key / bad private_key
            return None, ErrorClass.AUTH, redact(f"key parse/sign failed: {exc}")

        resp, _elapsed, exc = await timed_request(
            ctx.client,
            "POST",
            info.get("token_uri", TOKEN_URI),
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if exc is not None or resp is None:
            return None, ErrorClass.NETWORK, redact(repr(exc))
        if resp.status_code == 200:
            try:
                token = resp.json().get("access_token")
            except ValueError:
                token = None
            if token:
                return token, ErrorClass.OK, None
            return None, ErrorClass.UNKNOWN, "no access_token in response"
        # 400 invalid_grant => revoked / invalid key
        body = resp.text[:300]
        if resp.status_code == 400 and "invalid_grant" in body:
            return None, ErrorClass.AUTH, "invalid_grant (key revoked or invalid)"
        return None, ErrorClass.UNKNOWN, redact(f"token {resp.status_code}: {body}")

    async def _get_json(
        self, ctx: CheckContext, url: str, token: str, method: str = "GET", **kw
    ) -> tuple[int | None, dict[str, Any] | None, str | None]:
        """GET/POST a GCP REST endpoint with the bearer token. Returns
        (status, json_or_None, error_reason). 403 reasons are surfaced."""
        headers = {"Authorization": f"Bearer {token}"}
        if "json" in kw:
            headers["Content-Type"] = "application/json"
        # Cap per-request time so unreachable regions/disabled APIs fail fast
        # instead of dragging the whole scan to a multi-minute crawl.
        timeout = getattr(getattr(ctx.settings, "gcp", None), "probe_timeout_s", 8.0)
        kw.setdefault("timeout", timeout)
        resp, _elapsed, exc = await timed_request(ctx.client, method, url, headers=headers, **kw)
        if exc is not None or resp is None:
            return None, None, "network"
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code == 200:
            return 200, data if isinstance(data, dict) else {}, None
        reason = None
        if isinstance(data, dict):
            err = data.get("error", {})
            if isinstance(err, dict):
                details = err.get("details", [])
                for d in details if isinstance(details, list) else []:
                    if isinstance(d, dict) and d.get("reason"):
                        reason = d["reason"]
                        break
                reason = reason or err.get("status") or err.get("message")
        return resp.status_code, data if isinstance(data, dict) else None, reason

    # ------------------------------------------------------------------ #
    # health
    # ------------------------------------------------------------------ #
    async def health_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        info = self._load_info(key)
        result.details["project_id"] = info.get("project_id")
        result.details["client_email"] = info.get("client_email")
        await ctx.progress(0.2, "签发 token…")
        token, klass, detail = await self._mint_token(info, ctx)
        if klass == ErrorClass.OK and token:
            result.status = KeyStatus.ALIVE
            result.alive = True
            result.remarks.append(f"项目: {info.get('project_id')}")
            result.remarks.append(f"SA: {info.get('client_email')}")
            result.progress_label = "存活"
        elif klass == ErrorClass.AUTH:
            result.status = KeyStatus.DEAD
            result.alive = False
            result.error = detail
            result.progress_label = "密钥失效"
        else:
            result.status = KeyStatus.ERROR
            result.error = detail
            result.progress_label = "无法验证"
        await ctx.progress(1.0, result.progress_label)

    # ------------------------------------------------------------------ #
    # grade — full scan
    # ------------------------------------------------------------------ #
    async def grade_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        info = self._load_info(key)
        project = info.get("project_id")
        result.details["project_id"] = project
        result.details["client_email"] = info.get("client_email")

        await ctx.progress(0.05, "签发 token…")
        token, klass, detail = await self._mint_token(info, ctx)
        if not token:
            if klass == ErrorClass.AUTH:
                result.status = KeyStatus.DEAD
                result.alive = False
            else:
                result.status = KeyStatus.ERROR
            result.error = detail
            await ctx.progress(1.0, "失败")
            return

        result.alive = True
        report: dict[str, Any] = {
            "project_id": project,
            "client_email": info.get("client_email"),
        }

        # Run the four scan areas concurrently — they are independent (vertex
        # fetches its own project number for the quota call). This turns a
        # ~16s serial crawl into roughly the slowest single area.
        await ctx.progress(0.15, "并行扫描中…")
        await asyncio.gather(
            self._scan_project(ctx, token, project, result, report),
            self._scan_compute(ctx, token, project, result, report),
            self._scan_databases(ctx, token, project, result, report),
            self._scan_vertex(ctx, token, project, result, report, info),
            return_exceptions=True,
        )

        # finalize: GCP has no single tier; summarize + attach full report
        result.status = KeyStatus.GRADED
        result.tier = "GCP"
        result.details["report"] = report
        result.download_filename = f"gcp-{project or 'project'}.json"
        result.download_text = json.dumps(report, ensure_ascii=False, indent=2)
        result.remarks.append("完整信息见下载报告 ↓")
        await ctx.progress(1.0, "GCP")

    # ------------------------------------------------------------------ #
    # scan sub-steps
    # ------------------------------------------------------------------ #
    async def _scan_project(self, ctx, token, project, result: KeyResult, report) -> None:
        # metadata
        status, data, reason = await self._get_json(
            ctx, f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}", token
        )
        project_number = None
        if status == 200 and data:
            project_number = data.get("projectNumber")
            report["project"] = {
                "name": data.get("name"),
                "projectNumber": project_number,
                "lifecycleState": data.get("lifecycleState"),
            }
            result.remarks.append(
                f"项目: {data.get('name')} (#{project_number}, {data.get('lifecycleState')})"
            )
        elif reason:
            result.remarks.append(f"项目元数据: {reason}")

        # billing
        status, data, reason = await self._get_json(
            ctx,
            f"https://cloudbilling.googleapis.com/v1/projects/{project}/billingInfo",
            token,
        )
        if status == 200 and data:
            enabled = data.get("billingEnabled")
            report["billing"] = {
                "billingEnabled": enabled,
                "billingAccountName": data.get("billingAccountName"),
            }
            result.remarks.append(f"计费: {'已开启' if enabled else '未开启'}")

        # enabled APIs (needs project number). Distinguish "0 enabled" from
        # "no permission to list" — reporting 0 when it's actually 403 is a lie.
        if project_number:
            estatus, edata, ereason = await self._get_json(
                ctx,
                f"https://serviceusage.googleapis.com/v1/projects/{project_number}"
                "/services?filter=state:ENABLED&pageSize=200",
                token,
            )
            if estatus == 200 and edata is not None:
                services = edata.get("services") or []
                names = [
                    (s.get("config") or {}).get("name")
                    for s in services
                    if isinstance(s, dict)
                ]
                names = [n for n in names if n]
                report["enabled_apis"] = names
                result.remarks.append(f"已启用 API: {len(names)} 个")
            else:
                report["enabled_apis"] = {"error": ereason or f"status {estatus}"}
                result.remarks.append(f"已启用 API: 无法读取 ({ereason or estatus})")

        # what can this SA actually do
        perms_to_test = [
            "compute.instances.list",
            "cloudsql.instances.list",
            "aiplatform.endpoints.predict",
            "resourcemanager.projects.get",
            "serviceusage.services.list",
        ]
        status, data, reason = await self._get_json(
            ctx,
            f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:testIamPermissions",
            token,
            method="POST",
            json={"permissions": perms_to_test},
        )
        if status == 200 and data:
            granted = data.get("permissions", [])
            report["granted_permissions"] = granted
            result.remarks.append(f"SA 权限: {len(granted)}/{len(perms_to_test)} 项可用")

    async def _scan_compute(self, ctx, token, project, result: KeyResult, report) -> None:
        url = (
            f"https://compute.googleapis.com/compute/v1/projects/{project}"
            "/aggregated/instances"
        )
        status, data, reason = await self._get_json(ctx, url, token)
        if status != 200 or not data:
            report["compute"] = {"error": reason or f"status {status}"}
            result.remarks.append(f"服务器: 不可用 ({reason or status})")
            return
        total = 0
        running = 0
        by_zone: dict[str, int] = {}
        for zone_key, bucket in (data.get("items") or {}).items():
            insts = bucket.get("instances") if isinstance(bucket, dict) else None
            if not insts:
                continue
            zone = zone_key.split("/")[-1]
            by_zone[zone] = len(insts)
            for inst in insts:
                total += 1
                if inst.get("status") == "RUNNING":
                    running += 1
        report["compute"] = {"total": total, "running": running, "by_zone": by_zone}
        result.remarks.append(f"服务器: {running} 运行 / {total} 总计 ({len(by_zone)} 区)")

    async def _scan_databases(self, ctx, token, project, result: KeyResult, report) -> None:
        dbs: dict[str, Any] = {}
        denied: list[str] = []

        async def count(label: str, url: str, items_key: str):
            """Return a dict with either a count or an access-denied marker, so a
            403 is never silently reported as '0'."""
            st, data, reason = await self._get_json(ctx, url, token)
            if st == 200 and data is not None:
                items = data.get(items_key) or []
                return {"count": len(items), "items": items[:20]}
            denied.append(label)
            return {"count": None, "error": reason or f"status {st}"}

        sql, alloy, spanner, fs = await asyncio.gather(
            count("Cloud SQL", f"https://sqladmin.googleapis.com/v1/projects/{project}/instances", "items"),
            count("AlloyDB", f"https://alloydb.googleapis.com/v1/projects/{project}/locations/-/clusters", "clusters"),
            count("Spanner", f"https://spanner.googleapis.com/v1/projects/{project}/instances", "instances"),
            count("Firestore", f"https://firestore.googleapis.com/v1/projects/{project}/databases", "databases"),
        )
        dbs = {"cloudsql": sql, "alloydb": alloy, "spanner": spanner, "firestore": fs}
        report["databases"] = dbs

        def n(d):
            return d.get("count")

        counts = [n(sql), n(alloy), n(spanner), n(fs)]
        known = [c for c in counts if c is not None]
        total_db = sum(known)
        parts = (
            f"SQL {fmt(n(sql))}, AlloyDB {fmt(n(alloy))}, "
            f"Spanner {fmt(n(spanner))}, Firestore {fmt(n(fs))}"
        )
        if denied:
            result.remarks.append(
                f"数据库: 已知 {total_db} ({parts}) — {len(denied)} 类无权限: {', '.join(denied)}"
            )
        else:
            result.remarks.append(f"数据库: {total_db} ({parts})")

    async def _scan_vertex(self, ctx, token, project, result: KeyResult, report, info) -> None:
        cfg = getattr(ctx.settings, "gcp", None)
        publishers = list(getattr(cfg, "publishers", None) or ["google"])
        max_loc = int(getattr(cfg, "max_locations", 0) or 0)

        # 1) Determine the regions to enumerate. Prefer the live locations list.
        locations = list(getattr(cfg, "locations", None) or DEFAULT_LOCATIONS)
        status, data, _ = await self._get_json(
            ctx,
            f"https://aiplatform.googleapis.com/v1/projects/{project}/locations",
            token,
        )
        if status == 200 and data and data.get("locations"):
            live = [
                loc.get("locationId")
                for loc in data["locations"]
                if isinstance(loc, dict) and loc.get("locationId")
            ]
            if live:
                locations = live
        if max_loc and len(locations) > max_loc:
            core = [loc for loc in DEFAULT_LOCATIONS if loc in locations]
            locations = (core + [x for x in locations if x not in core])[:max_loc]

        # 2) Enumerate the REAL model catalogue per region via the working
        #    v1beta1 publishers/{publisher}/models endpoint. No hardcoded names.
        sem = asyncio.Semaphore(int(getattr(cfg, "region_concurrency", 12) or 12))

        async def list_models(loc: str, publisher: str) -> tuple[str, str, list[str]]:
            async with sem:
                names: list[str] = []
                page = None
                base = (
                    f"https://{loc}-aiplatform.googleapis.com/v1beta1/"
                    f"publishers/{publisher}/models?pageSize=200"
                )
                for _ in range(6):
                    url = base + (f"&pageToken={page}" if page else "")
                    st, d, _r = await self._get_json(ctx, url, token)
                    if st != 200 or not d:
                        break
                    for m in d.get("publisherModels", []) or []:
                        nm = (m.get("name") or "").split("/")[-1]
                        if nm:
                            names.append(nm)
                    page = d.get("nextPageToken")
                    if not page:
                        break
                return loc, publisher, names

        tasks = [
            list_models(loc, pub) for loc in locations for pub in publishers
        ]
        listings = await asyncio.gather(*tasks, return_exceptions=True)

        by_location: dict[str, dict[str, list[str]]] = {}
        all_models: set[str] = set()
        for item in listings:
            if isinstance(item, Exception):
                continue
            loc, pub, names = item
            if not names:
                continue
            by_location.setdefault(loc, {})[pub] = names
            all_models.update(names)

        report["vertex"] = {
            "regions_enumerated": len(locations),
            "publishers": publishers,
            "distinct_model_count": len(all_models),
            "distinct_models": sorted(all_models),
            "by_location": by_location,
        }
        if all_models:
            result.remarks.append(
                f"Vertex: {len(all_models)} 个模型 / {len(by_location)} 个区域 "
                f"({'+'.join(publishers)})"
            )
        else:
            result.remarks.append(
                "Vertex: 未枚举到模型 (aiplatform API 可能未启用或无访问权限)"
            )

        # 3) Pick models to measure RPM/TPM. User specified priority models that
        #    need global location or special formats (Claude, image, 3.x preview).
        #    Strategy: try user priority list first, fall back to callable gemini.
        priority_models = [
            # Claude (anthropic publisher, :rawPredict, global only for this project)
            "claude-opus-4-1", "claude-sonnet-4-5", "claude-haiku-4-5",
            "claude-opus-4-5", "claude-opus-4-6", "claude-sonnet-4-6",
            "claude-opus-4-7", "claude-opus-4-8",
            # Gemini 3.x (google publisher, some global-only, some image)
            "gemini-3.1-pro-preview", "gemini-3.1-flash-image-preview",
            "gemini-3-pro-preview", "gemini-3.5-flash",
        ]
        # Filter to models actually in the enumerated list.
        priority_in_list = [m for m in priority_models if m in all_models]
        # Add generic gemini text models as fallback.
        gen_fallback = sorted(
            m for m in all_models
            if m.startswith("gemini") and "embedding" not in m and "image" not in m and m not in priority_in_list
        )

        if ctx.full_load and (priority_in_list or gen_fallback):
            # Check priority models first
            priority_callable = []
            if priority_in_list:
                priority_callable = await self._filter_callable_multi_loc(
                    ctx, token, project, priority_in_list
                )
                callable_names = {m for m, loc in priority_callable}
                unavailable = [m for m in priority_in_list if m not in callable_names]
                if unavailable:
                    result.remarks.append(
                        f"优先模型不可用 ({len(unavailable)}): {', '.join(unavailable[:5])}"
                        + (f" ..." if len(unavailable) > 5 else "")
                    )

            # Then check fallback models (up to max_models - priority count)
            max_models = int(getattr(cfg, "rate_probe_max_models", 12) or 12)
            remaining_slots = max(0, max_models - len(priority_callable))
            fallback_callable = []
            if remaining_slots > 0 and gen_fallback:
                fallback_check = gen_fallback[:remaining_slots * 2]  # check 2x to ensure enough
                fallback_callable = await self._filter_callable_multi_loc(
                    ctx, token, project, fallback_check
                )
                fallback_callable = fallback_callable[:remaining_slots]

            # Combine: priority first, then fallback
            measure = priority_callable + fallback_callable
            rate_report: dict[str, Any] = {}
            for model, location in measure:
                rpm = await self._measure_rpm(ctx, token, project, location, model, cfg)
                # TPM: skip image models (RPM-only), measure text/Claude models.
                if "image" in model:
                    rate_report[model] = {"location": location, "rpm": rpm, "tpm": "N/A (图像模型)"}
                    result.remarks.append(f"{model}: RPM {rpm['label']}")
                else:
                    tpm = await self._measure_tpm(ctx, token, project, location, model, cfg)
                    rate_report[model] = {"location": location, "rpm": rpm, "tpm": tpm}
                    result.remarks.append(
                        f"{model}: RPM {rpm['label']} · TPM {tpm['label']}"
                    )
            report["vertex_rate_measurements"] = {
                "note": "配额跨地区共享，实测一个 location 即代表项目上限",
                "models": rate_report,
            }
        elif priority_in_list or gen_fallback:
            result.remarks.append(
                "RPM/TPM: 未测 (开启「全速压测」才会实际调用测量)"
            )

        # 4) Cloud Quotas (best effort) for documented limits.
        project_number = (report.get("project") or {}).get("projectNumber")
        if not project_number:
            pstatus, pdata, _ = await self._get_json(
                ctx,
                f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}",
                token,
            )
            if pstatus == 200 and pdata:
                project_number = pdata.get("projectNumber")
        quota_info: list[Any] = []
        if project_number:
            quotas = await self._list_paginated(
                ctx,
                token,
                f"https://cloudquotas.googleapis.com/v1/projects/{project_number}"
                "/locations/global/services/aiplatform.googleapis.com/quotaInfos",
                "quotaInfos",
            )
            for q in quotas:
                if not isinstance(q, dict):
                    continue
                metric = q.get("metric", "")
                if "request" in metric.lower() or "token" in metric.lower():
                    dims = q.get("dimensionsInfos") or q.get("quotaLimit") or {}
                    quota_info.append(
                        {"metric": metric, "displayName": q.get("displayName"), "limit": dims}
                    )
        if quota_info:
            report["vertex"]["quota_metrics"] = quota_info
            result.remarks.append(f"配额指标: {len(quota_info)} 项 (见报告)")

    def _model_params(self, project: str, location: str, model: str, text: str, max_tokens: int = 1) -> tuple[str, dict, callable]:
        """Return (url, body, token_extractor) for a given model. Adapts between
        Gemini (:generateContent + contents), Claude (:rawPredict + messages),
        and image models (responseModalities). location can be a region or 'global'."""
        is_claude = model.startswith("claude")
        is_image = "image" in model
        base = (
            f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}"
            if location == "global"
            else f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}"
        )
        if is_claude:
            url = f"{base}/publishers/anthropic/models/{model}:rawPredict"
            body = {
                "anthropic_version": "vertex-2023-10-16",
                "messages": [{"role": "user", "content": text}],
                "max_tokens": max_tokens,
            }
            # Claude on Vertex: usage = {input_tokens, output_tokens}
            def extract(resp_json):
                u = resp_json.get("usage") or {}
                return u.get("input_tokens", 0) + u.get("output_tokens", 0)
        else:
            url = f"{base}/publishers/google/models/{model}:generateContent"
            body = {
                "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": {"maxOutputTokens": max_tokens},
            }
            if is_image:
                body["generationConfig"]["responseModalities"] = ["IMAGE"]
            # Gemini: usageMetadata.totalTokenCount
            def extract(resp_json):
                return (resp_json.get("usageMetadata") or {}).get("totalTokenCount", 0)
        return url, body, extract

    async def _filter_callable_multi_loc(
        self, ctx, token, project, models
    ) -> list[tuple[str, str]]:
        """Return (model, location) pairs for models callable in ANY location.
        Tries regional probe_region first, then 'global' for models that 404 in
        the region (Claude, gemini-3.x often live only in global). Returns pairs
        that answer 200 or 429."""
        probe_region = getattr(ctx.settings.gcp, "probe_region", "us-central1") if hasattr(ctx.settings, "gcp") else "us-central1"
        sem = asyncio.Semaphore(12)

        async def chk(model: str, loc: str) -> tuple[str, str, bool]:
            async with sem:
                url, body, _ = self._model_params(project, loc, model, "hi", 1)
                st, _d, _r = await self._get_json(ctx, url, token, method="POST", json=body)
                return model, loc, st in (200, 429)

        # Try probe_region first for all; then try global for any that failed.
        regional = await asyncio.gather(*(chk(m, probe_region) for m in models), return_exceptions=True)
        ok_regional = {m: loc for item in regional if not isinstance(item, Exception) for m, loc, success in [item] if success}
        need_global = [m for m in models if m not in ok_regional]
        global_res = await asyncio.gather(*(chk(m, "global") for m in need_global), return_exceptions=True)
        ok_global = {m: loc for item in global_res if not isinstance(item, Exception) for m, loc, success in [item] if success}
        # Merge, preserving order from the input models list.
        combined = {**ok_regional, **ok_global}
        return [(m, combined[m]) for m in models if m in combined]

    async def _measure_rpm(self, ctx, token, project, location, model, cfg) -> dict[str, Any]:
        """Empirical RPM: fire small calls fast until first 429. Uses _model_params
        to adapt between Gemini/Claude/image. '≥X' when no 429 hit."""
        import time

        rps = float(getattr(cfg, "rpm_probe_rps", 50.0))
        seconds = float(getattr(cfg, "rpm_probe_seconds", 8.0))
        cap = int(getattr(cfg, "rpm_probe_cap", 400))
        url, body, _ = self._model_params(project, location, model, "hi", 1)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        success = rate_limited = other = 0
        first_429: float | None = None
        stop = asyncio.Event()
        start = time.monotonic()
        interval = 1.0 / rps if rps > 0 else 0.0
        tasks: list[asyncio.Task] = []

        async def one() -> None:
            nonlocal success, rate_limited, other, first_429
            resp, _e, exc = await timed_request(
                ctx.client, "POST", url, headers=headers, json=body, timeout=20.0
            )
            now = time.monotonic()
            if exc is not None or resp is None:
                other += 1
            elif resp.status_code == 200:
                success += 1
            elif resp.status_code == 429:
                rate_limited += 1
                if first_429 is None:
                    first_429 = now - start
                stop.set()
            else:
                other += 1

        n = 0
        while True:
            if time.monotonic() - start >= seconds or n >= cap or stop.is_set():
                break
            tasks.append(asyncio.create_task(one()))
            n += 1
            if interval > 0:
                wait = (start + n * interval) - time.monotonic()
                if wait > 0:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        window = max(time.monotonic() - start, 1e-6)
        hit = rate_limited > 0
        rpm = round(success / window * 60.0)
        return {
            "success": success,
            "rate_limited": rate_limited,
            "errors": other,
            "window_s": round(window, 2),
            "first_429_at_s": round(first_429, 2) if first_429 is not None else None,
            "rpm": rpm,
            "hit_limit": hit,
            "label": f"{rpm}" if hit else f"≥{rpm}",
        }

    async def _measure_tpm(self, ctx, token, project, location, model, cfg) -> dict[str, Any]:
        """Empirical TPM: send LARGE inputs and accumulate tokens until 429. Uses
        _model_params adapter + its token extractor for both Gemini (usageMetadata)
        and Claude (usage.input_tokens/output_tokens). '≥X' when no 429."""
        import time

        per_req = int(getattr(cfg, "tpm_tokens_per_request", 200_000))
        # Claude models have a 200k token context limit; use 100k per request.
        if model.startswith("claude"):
            per_req = min(per_req, 100_000)
        max_req = int(getattr(cfg, "tpm_probe_max_requests", 40))
        conc = int(getattr(cfg, "tpm_probe_concurrency", 8))
        big_text = "word " * per_req
        url, body, extract_tokens = self._model_params(project, location, model, big_text, 1)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        tokens = 0
        success = rate_limited = other = 0
        first_429: float | None = None
        stop = asyncio.Event()
        sem = asyncio.Semaphore(max(1, conc))
        start = time.monotonic()

        async def one() -> None:
            nonlocal tokens, success, rate_limited, other, first_429
            if stop.is_set():
                return
            async with sem:
                if stop.is_set():
                    return
                resp, _e, exc = await timed_request(
                    ctx.client, "POST", url, headers=headers, json=body, timeout=90.0
                )
                now = time.monotonic()
                if exc is not None or resp is None:
                    other += 1
                elif resp.status_code == 200:
                    success += 1
                    try:
                        tokens += extract_tokens(resp.json())
                    except (ValueError, KeyError, TypeError):
                        pass
                elif resp.status_code == 429:
                    rate_limited += 1
                    if first_429 is None:
                        first_429 = now - start
                    stop.set()
                else:
                    other += 1

        await asyncio.gather(*(one() for _ in range(max_req)), return_exceptions=True)
        window = max(time.monotonic() - start, 1e-6)
        hit = rate_limited > 0
        tpm = round(tokens / window * 60.0)
        return {
            "requests_ok": success,
            "rate_limited": rate_limited,
            "errors": other,
            "tokens_total": tokens,
            "tokens_per_request": per_req,
            "window_s": round(window, 2),
            "first_429_at_s": round(first_429, 2) if first_429 is not None else None,
            "tpm": tpm,
            "hit_limit": hit,
            "label": f"{tpm}" if hit else f"≥{tpm}",
        }

    # ------------------------------------------------------------------ #
    async def _list_paginated(
        self, ctx, token, url, items_key, page_param: str = "pageToken", max_pages: int = 10
    ) -> list[Any]:
        """Follow nextPageToken up to max_pages, collecting items_key arrays.
        Silently returns whatever it gathered (403/disabled API => [])."""
        out: list[Any] = []
        page_token = None
        sep = "&" if "?" in url else "?"
        for _ in range(max_pages):
            page_url = url
            if page_token:
                page_url = f"{url}{sep}pageToken={page_token}"
            status, data, _reason = await self._get_json(ctx, page_url, token)
            if status != 200 or not data:
                break
            items = data.get(items_key)
            if isinstance(items, list):
                out.extend(items)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return out


PLUGIN = GCPPlugin()
