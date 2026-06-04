"""Tests for the GCP service-account plugin and credential parsing."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import settings
from app.models import CheckMode, ErrorClass, KeyResult, KeyStatus
from app.parsing import parse_credentials
from app.plugins.base import CheckContext
from app.plugins.gcp import PLUGIN
from app.plugins.registry import all_plugins, dispatch


def _sa_key(project="test-proj"):
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": project,
            "private_key_id": "abc",
            "private_key": pem,
            "client_email": f"sa@{project}.iam.gserviceaccount.com",
            "client_id": "1",
            "token_uri": "https://oauth2.googleapis.com/token",
            "universe_domain": "googleapis.com",
        }
    )


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_parse_mixed_credentials_order_and_count():
    raw = (
        "sk-proj-AAAA\n"
        + _sa_key("p1")
        + "\nAIzaSy"
        + "x" * 33
        + "\nsk-ant-api03-zzz"
    )
    creds = parse_credentials(raw)
    assert len(creds) == 4
    assert creds[0] == "sk-proj-AAAA"
    assert creds[1].startswith("{") and json.loads(creds[1])["project_id"] == "p1"
    assert creds[2].startswith("AIzaSy")
    assert creds[3] == "sk-ant-api03-zzz"


def test_parse_json_with_braces_in_private_key():
    raw = _sa_key("p2")
    creds = parse_credentials(raw)
    assert len(creds) == 1
    obj = json.loads(creds[0])
    assert obj["type"] == "service_account"


def test_parse_empty():
    assert parse_credentials("   \n\n  ") == []


@pytest.mark.parametrize(
    "wrap",
    [
        lambda s: "﻿" + s,  # UTF-8 BOM
        lambda s: "​" + s,  # zero-width space
        lambda s: "\n\n   " + s + "  \n",  # blank lines + whitespace
        lambda s: s.replace('"type"', "“type”"),  # smart quotes
    ],
)
def test_parse_and_detect_survives_paste_artifacts(wrap):
    raw = wrap(_sa_key("artifact-proj"))
    creds = parse_credentials(raw)
    assert len(creds) == 1, f"expected 1 credential, got {len(creds)}"
    assert PLUGIN.matches(creds[0]), "GCP plugin failed to match after artifact"
    assert PLUGIN._load_info(creds[0])["project_id"] == "artifact-proj"


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def test_gcp_registered_and_dispatched():
    assert "gcp" in {p.name for p in all_plugins()}
    assert dispatch(_sa_key()).name == "gcp"


def test_gcp_matches_only_service_account_json():
    assert PLUGIN.matches(_sa_key())
    assert not PLUGIN.matches("sk-proj-abc")
    assert not PLUGIN.matches("AIza" + "x" * 35)
    assert not PLUGIN.matches('{"type":"authorized_user","x":1}')
    assert not PLUGIN.matches('{"foo":"bar"}')
    assert not PLUGIN.matches("not json at all")


def test_other_plugins_dont_claim_gcp_json():
    sa = _sa_key()
    for p in all_plugins():
        if p.name != "gcp":
            assert not p.matches(sa), f"{p.name} wrongly claims a GCP key"


# --------------------------------------------------------------------------- #
# token exchange (mocked)
# --------------------------------------------------------------------------- #
async def _ctx(mode=CheckMode.GRADE):
    client = httpx.AsyncClient()

    async def progress(frac, label):
        pass

    return client, CheckContext(
        client=client, settings=settings, mode=mode, full_load=False, progress=progress
    )


@pytest.mark.asyncio
@respx.mock
async def test_gcp_health_invalid_grant_is_dead():
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "revoked"}
        )
    )
    client, ctx = await _ctx(mode=CheckMode.HEALTH)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.HEALTH)
    await PLUGIN.health_check(_sa_key(), r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.DEAD
    assert r.alive is False


@pytest.mark.asyncio
@respx.mock
async def test_gcp_health_alive_on_token():
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "ya29.fake", "expires_in": 3600, "token_type": "Bearer"}
        )
    )
    client, ctx = await _ctx(mode=CheckMode.HEALTH)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.HEALTH)
    await PLUGIN.health_check(_sa_key("driveredsafety-syeh"), r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.ALIVE
    assert r.alive is True
    assert any("driveredsafety-syeh" in note for note in r.remarks)


@pytest.mark.asyncio
@respx.mock
async def test_gcp_grade_full_scan_builds_report_and_download():
    project = "test-proj"
    # token
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "ya29.fake"})
    )
    # project metadata
    respx.get(
        f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"name": "Test Project", "projectNumber": "123", "lifecycleState": "ACTIVE"},
        )
    )
    # billing
    respx.get(
        f"https://cloudbilling.googleapis.com/v1/projects/{project}/billingInfo"
    ).mock(return_value=httpx.Response(200, json={"billingEnabled": True}))
    # enabled apis
    respx.get(url__startswith="https://serviceusage.googleapis.com").mock(
        return_value=httpx.Response(
            200, json={"services": [{"config": {"name": "compute.googleapis.com"}}]}
        )
    )
    # testIamPermissions
    respx.post(
        f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:testIamPermissions"
    ).mock(return_value=httpx.Response(200, json={"permissions": ["compute.instances.list"]}))
    # compute aggregated
    respx.get(url__startswith="https://compute.googleapis.com").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": {
                    "zones/us-central1-a": {
                        "instances": [
                            {"status": "RUNNING"},
                            {"status": "TERMINATED"},
                        ]
                    }
                }
            },
        )
    )
    # databases
    respx.get(url__startswith="https://sqladmin.googleapis.com").mock(
        return_value=httpx.Response(200, json={"items": [{"name": "db1", "state": "RUNNABLE"}]})
    )
    respx.get(url__startswith="https://alloydb.googleapis.com").mock(
        return_value=httpx.Response(200, json={"clusters": []})
    )
    respx.get(url__startswith="https://spanner.googleapis.com").mock(
        return_value=httpx.Response(200, json={"instances": []})
    )
    respx.get(url__startswith="https://firestore.googleapis.com").mock(
        return_value=httpx.Response(200, json={"databases": [{"name": "(default)"}]})
    )
    # vertex locations + model enumeration (v1beta1 publishers/{pub}/models)
    respx.get(
        f"https://aiplatform.googleapis.com/v1/projects/{project}/locations"
    ).mock(return_value=httpx.Response(200, json={"locations": [{"locationId": "us-central1"}]}))
    respx.get(url__regex=r"https://us-central1-aiplatform\.googleapis\.com/v1beta1/publishers/google/models").mock(
        return_value=httpx.Response(
            200,
            json={"publisherModels": [
                {"name": "publishers/google/models/gemini-2.5-pro"},
                {"name": "publishers/google/models/gemini-2.5-flash"},
            ]},
        )
    )
    respx.get(url__regex=r"https://us-central1-aiplatform\.googleapis\.com/v1beta1/publishers/(anthropic|meta|mistralai)/models").mock(
        return_value=httpx.Response(200, json={"publisherModels": []})
    )
    respx.get(url__startswith="https://cloudquotas.googleapis.com").mock(
        return_value=httpx.Response(
            200,
            json={
                "quotaInfos": [
                    {"metric": "aiplatform.googleapis.com/online_prediction_requests_per_minute",
                     "displayName": "RPM"}
                ]
            },
        )
    )

    client, ctx = await _ctx(mode=CheckMode.GRADE)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.GRADE)
    await PLUGIN.grade_check(_sa_key(project), r, ctx)
    await client.aclose()

    assert r.status == KeyStatus.GRADED
    assert r.tier == "GCP"
    assert r.alive is True
    # download report attached
    assert r.download_filename and r.download_filename.endswith(".json")
    assert r.download_text
    report = json.loads(r.download_text)
    assert report["compute"]["running"] == 1
    assert report["compute"]["total"] == 2
    assert report["databases"]["cloudsql"]["count"] == 1
    assert report["databases"]["firestore"]["count"] == 1
    assert "gemini-2.5-pro" in report["vertex"]["distinct_models"]
    # remarks summarize each area
    joined = " ".join(r.remarks)
    assert "服务器" in joined and "数据库" in joined and "Vertex" in joined


def test_download_text_excluded_from_serialization():
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.GRADE)
    r.download_filename = "gcp.json"
    r.download_text = "SECRET-BIG-PAYLOAD"
    dumped = r.model_dump(mode="json")
    assert "download_text" not in dumped
    assert dumped["download_filename"] == "gcp.json"
