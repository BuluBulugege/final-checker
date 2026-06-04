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
        """Strip BOM/zero-width junk and normalize smart quotes so a pasted key
        with editor artifacts still parses as JSON."""
        k = key.strip().lstrip("﻿").replace("​", "")
        # smart double quotes -> straight (single quotes aren't valid JSON anyway)
        k = k.replace("“", '"').replace("”", '"')
        return k

    def matches(self, key: str) -> bool:
        """A GCP key is a JSON object with type=service_account and the tell-tale
        service-account fields. Cheap structural check, no network."""
        k = self._normalize(key)
        if not (k.startswith("{") and k.endswith("}")):
            return False
        try:
            obj = json.loads(k)
        except (ValueError, TypeError):
            return False
        if not isinstance(obj, dict):
            return False
        if obj.get("type") != "service_account":
            return False
        return bool(obj.get("private_key") and obj.get("client_email"))

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_info(key: str) -> dict[str, Any]:
        return json.loads(GCPPlugin._normalize(key))

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

        # enabled APIs (needs project number)
        if project_number:
            apis = await self._list_paginated(
                ctx,
                token,
                f"https://serviceusage.googleapis.com/v1/projects/{project_number}"
                "/services?filter=state:ENABLED",
                "services",
            )
            names = [
                (s.get("config") or {}).get("name")
                for s in apis
                if isinstance(s, dict)
            ]
            names = [n for n in names if n]
            report["enabled_apis"] = names
            result.remarks.append(f"已启用 API: {len(names)} 个")

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

        # Cloud SQL
        sql = await self._list_paginated(
            ctx, token, f"https://sqladmin.googleapis.com/v1/projects/{project}/instances", "items"
        )
        dbs["cloudsql"] = {
            "count": len(sql),
            "instances": [
                {"name": i.get("name"), "version": i.get("databaseVersion"),
                 "state": i.get("state"), "region": i.get("region")}
                for i in sql if isinstance(i, dict)
            ],
        }

        # AlloyDB (wildcard location)
        alloy = await self._list_paginated(
            ctx, token,
            f"https://alloydb.googleapis.com/v1/projects/{project}/locations/-/clusters",
            "clusters",
        )
        dbs["alloydb"] = {"count": len(alloy)}

        # Spanner
        spanner = await self._list_paginated(
            ctx, token, f"https://spanner.googleapis.com/v1/projects/{project}/instances",
            "instances",
        )
        dbs["spanner"] = {"count": len(spanner)}

        # Firestore (no pagination)
        status, data, reason = await self._get_json(
            ctx, f"https://firestore.googleapis.com/v1/projects/{project}/databases", token
        )
        fs = (data.get("databases") if status == 200 and data else None) or []
        dbs["firestore"] = {"count": len(fs)}

        report["databases"] = dbs
        total_db = (
            dbs["cloudsql"]["count"]
            + dbs["alloydb"]["count"]
            + dbs["spanner"]["count"]
            + dbs["firestore"]["count"]
        )
        result.remarks.append(
            f"数据库: {total_db} (SQL {dbs['cloudsql']['count']}, "
            f"AlloyDB {dbs['alloydb']['count']}, Spanner {dbs['spanner']['count']}, "
            f"Firestore {dbs['firestore']['count']})"
        )

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
            "quota_metrics": quota_info,
        }
        result.remarks.append(
            f"Vertex: {len(all_models)} 个模型 / {reachable} 个可用区域"
        )
        if quota_info:
            result.remarks.append(f"配额指标: {len(quota_info)} 项 (见报告)")
        else:
            result.remarks.append("配额: 无法读取 (报告含模型清单)")

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
