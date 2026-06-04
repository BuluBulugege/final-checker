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
        locations = list(getattr(cfg, "locations", None) or DEFAULT_LOCATIONS)

        # Try to discover the live location list (best effort).
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

        # Cap the number of regions actually probed. The live list can be ~48
        # regions (mostly empty for one project); publisher models are uniform
        # across regions, so a handful covers the catalog. Keep the configured
        # core regions first when capping. 0 = no cap.
        max_loc = int(getattr(cfg, "max_locations", 0) or 0)
        if max_loc and len(locations) > max_loc:
            core = [loc for loc in DEFAULT_LOCATIONS if loc in locations]
            ordered = core + [loc for loc in locations if loc not in core]
            locations = ordered[:max_loc]

        per_location: dict[str, Any] = {}
        all_models: set[str] = set()
        sem = asyncio.Semaphore(int(getattr(cfg, "region_concurrency", 20) or 20))

        async def scan_one(loc: str) -> tuple[str, dict[str, Any] | None]:
            async with sem:
                base = f"https://{loc}-aiplatform.googleapis.com/v1"
                pub = await self._list_paginated(
                    ctx,
                    token,
                    f"{base}/projects/{project}/locations/{loc}/publishers/google/models",
                    "models",
                    page_param="pageSize",
                )
                tuned = await self._list_paginated(
                    ctx,
                    token,
                    f"{base}/projects/{project}/locations/{loc}/models",
                    "models",
                    page_param="pageSize",
                )
            pub_names = [m.get("name", "").split("/")[-1] for m in pub if isinstance(m, dict)]
            tuned_names = [
                m.get("displayName") or m.get("name") for m in tuned if isinstance(m, dict)
            ]
            if pub_names or tuned_names:
                return loc, {"publisher_models": pub_names, "tuned_models": tuned_names}
            return loc, None

        # Scan all regions concurrently (was serial -> very slow with ~12 regions).
        results = await asyncio.gather(
            *(scan_one(loc) for loc in locations), return_exceptions=True
        )
        reachable = 0
        for item in results:
            if isinstance(item, Exception) or item is None:
                continue
            loc, payload = item
            if payload:
                reachable += 1
                per_location[loc] = payload
                all_models.update(payload["publisher_models"])

        # The publisher-models LIST endpoint frequently 404s even when the models
        # are fully usable (as the user correctly suspected: "真的如此吗?"). So
        # when listing found nothing, ACTUALLY CALL a set of known Gemini models
        # with a 1-token generateContent to discover what really works. This is
        # the user's spec: "如果没法测，就真正实际调用这些模型".
        usable_models: list[str] = []
        probe_region = getattr(cfg, "probe_region", "us-central1")
        probe_models = list(getattr(cfg, "probe_models", None) or [])
        if not all_models and probe_models:
            psem = asyncio.Semaphore(int(getattr(cfg, "region_concurrency", 10) or 10))

            async def probe_model(model: str) -> tuple[str, bool, str | None]:
                async with psem:
                    url = (
                        f"https://{probe_region}-aiplatform.googleapis.com/v1/projects/"
                        f"{project}/locations/{probe_region}/publishers/google/models/"
                        f"{model}:generateContent"
                    )
                    body = {
                        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                        "generationConfig": {"maxOutputTokens": 1},
                    }
                    st, data, reason = await self._get_json(
                        ctx, url, token, method="POST", json=body
                    )
                    if st == 200:
                        return model, True, None
                    # 404 here means "model not available to this project"; 403 a
                    # permission gap; 429 means it works but is rate-limited.
                    if st == 429:
                        return model, True, "rate-limited (可用但限流)"
                    return model, False, reason or f"status {st}"

            probe_results = await asyncio.gather(
                *(probe_model(m) for m in probe_models), return_exceptions=True
            )
            probe_detail: dict[str, Any] = {}
            for item in probe_results:
                if isinstance(item, Exception):
                    continue
                model, ok, note = item
                probe_detail[model] = {"usable": ok, "note": note}
                if ok:
                    usable_models.append(model)
            report["vertex_model_probe"] = {
                "region": probe_region,
                "probed": probe_models,
                "usable": usable_models,
                "detail": probe_detail,
            }

        # RPM/TPM measurement: Vertex exposes NO rate-limit headers, so the only
        # way to learn the real per-minute ceilings is to actually push traffic
        # and watch for the first 429 (the user's spec). Real calls cost money,
        # so this only runs in full_load mode.
        model_rates: dict[str, Any] = {}
        models_to_measure = (usable_models or sorted(all_models))[
            : int(getattr(cfg, "rpm_probe_max_models", 3) or 3)
        ]
        if ctx.full_load and models_to_measure:
            # Measure each model concurrently (was serial -> 3x slower, and the
            # serial windows could interfere via the shared connection pool).
            rate_results = await asyncio.gather(
                *(
                    self._measure_model_rate(ctx, token, project, probe_region, m, cfg)
                    for m in models_to_measure
                ),
                return_exceptions=True,
            )
            for model, rate in zip(models_to_measure, rate_results):
                if isinstance(rate, Exception):
                    continue
                model_rates[model] = rate
                result.remarks.append(
                    f"{model}: 实测 RPM {rate['rpm_label']} · TPM {rate['tpm_label']}"
                )
            report["vertex_rate_measurements"] = model_rates
        elif models_to_measure:
            result.remarks.append("RPM/TPM: 未测 (开启「全速压测」才会实际调用测量)")

        # Quota / RPM-TPM (best effort via Cloud Quotas API, needs project number).
        # The project scan runs concurrently, so fetch the number ourselves if it
        # isn't in the report yet.
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

        report["vertex"] = {
            "locations_scanned": len(locations),
            "locations_with_models": reachable,
            "distinct_models": sorted(all_models),
            "by_location": per_location,
            "usable_models_probed": usable_models,
            "quota_metrics": quota_info,
        }
        # Honest remark: separate "listed" from "actually-callable" and never
        # claim a flat 0 when the truth is "list unavailable".
        if all_models:
            result.remarks.append(
                f"Vertex: {len(all_models)} 个模型 (列表) / {reachable} 个区域"
            )
        elif usable_models:
            result.remarks.append(
                f"Vertex: 列表不可用，实测 {len(usable_models)} 个模型可调用 "
                f"({', '.join(usable_models)})"
            )
        else:
            result.remarks.append(
                "Vertex: 列表端点 404 且已知模型均不可调用 "
                "(可能 aiplatform API 未启用或无 predict 权限)"
            )
        if quota_info:
            result.remarks.append(f"配额指标: {len(quota_info)} 项 (见报告)")
        else:
            result.remarks.append("配额: 无法读取 (需 cloudquotas 权限)")

    # ------------------------------------------------------------------ #
    async def _measure_model_rate(
        self, ctx, token, project, region, model, cfg
    ) -> dict[str, Any]:
        """Empirically measure a Vertex model's RPM/TPM by pushing real traffic
        until the first 429 (Vertex exposes no rate-limit headers). Accumulates
        usageMetadata.totalTokenCount from successes to derive TPM. Returns a
        dict with raw counts, the derived per-minute figures, and labels that
        say '≥X' when the limit was never hit within the window."""
        import time

        rps = float(getattr(cfg, "rpm_probe_rps", 20.0))
        seconds = float(getattr(cfg, "rpm_probe_seconds", 10.0))
        cap = int(getattr(cfg, "rpm_probe_cap", 200))
        url = (
            f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
            f"/locations/{region}/publishers/google/models/{model}:generateContent"
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 1},
        }
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        success = 0
        rate_limited = 0
        other = 0
        tokens = 0
        first_429_at: float | None = None
        stop = asyncio.Event()
        start = time.monotonic()
        interval = 1.0 / rps if rps > 0 else 0.0
        tasks: list[asyncio.Task] = []

        async def one() -> None:
            nonlocal success, rate_limited, other, tokens, first_429_at
            resp, _elapsed, exc = await timed_request(
                ctx.client, "POST", url, headers=headers, json=body, timeout=20.0
            )
            now = time.monotonic()
            if exc is not None or resp is None:
                other += 1
                return
            if resp.status_code == 200:
                success += 1
                try:
                    tokens += (resp.json().get("usageMetadata") or {}).get("totalTokenCount", 0)
                except ValueError:
                    pass
            elif resp.status_code == 429:
                rate_limited += 1
                if first_429_at is None or now - start < first_429_at:
                    first_429_at = now - start
                stop.set()
            else:
                other += 1

        n = 0
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= seconds or n >= cap or stop.is_set():
                break
            tasks.append(asyncio.create_task(one()))
            n += 1
            if interval > 0:
                nxt = start + n * interval
                wait = nxt - time.monotonic()
                if wait > 0:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        window = time.monotonic() - start

        # Derive per-minute figures. If we hit a 429, the successes before it are
        # the empirical ceiling; otherwise we only know the rate is AT LEAST what
        # we achieved in the window.
        hit_limit = rate_limited > 0
        if window > 0:
            rpm = round(success / window * 60.0)
            tpm = round(tokens / window * 60.0)
        else:
            rpm = success
            tpm = tokens
        return {
            "success": success,
            "rate_limited": rate_limited,
            "errors": other,
            "tokens_total": tokens,
            "window_s": round(window, 2),
            "first_429_at_s": round(first_429_at, 2) if first_429_at is not None else None,
            "rpm": rpm,
            "tpm": tpm,
            "hit_limit": hit_limit,
            "rpm_label": f"{rpm}" if hit_limit else f"≥{rpm}",
            "tpm_label": f"{tpm}" if hit_limit else f"≥{tpm}",
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
