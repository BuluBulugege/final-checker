"""Tests for the Azure OpenAI / AI Foundry plugin: URL|KEY detection, the
stitch()/extract_candidates() parsing hooks, and health/grade behavior against
fully mocked HTTP (respx — no real network)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.models import CheckMode, KeyResult, KeyStatus
from app.parsing import parse_credentials
from app.plugins.azure import PLUGIN
from app.plugins.base import CheckContext
from app.plugins.registry import all_plugins, dispatch
from app.config import settings

AZURE_KEY = "https://res.openai.azure.com|" + "k" * 32


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "key",
    [
        "https://res.openai.azure.com|" + "k" * 32,
        "https://res.services.ai.azure.com|" + "k" * 32,  # AI Foundry
        "https://res.cognitiveservices.azure.com|" + "k" * 32,
        "https://res.openai.azure.com:443|" + "k" * 32,  # default port explicit
        "HTTPS://RES.OPENAI.AZURE.COM|" + "k" * 32,  # case-insensitive
        "  https://res.openai.azure.com|" + "k" * 32 + "  ",  # surrounding ws
    ],
)
def test_matches_accepts_valid_url_key(key):
    assert PLUGIN.matches(key)


@pytest.mark.parametrize(
    "key",
    [
        "http://res.openai.azure.com|" + "k" * 32,  # plain http
        "https://res.openai.azure.com@attacker.example|" + "k" * 32,  # userinfo
        "https://attacker.example/|" + "k" * 32,  # non-azure host
        "https://res.openai.azure.com:8443|" + "k" * 32,  # wrong port
        "https://res.openai.azure.com|",  # empty key part
        "https://res.openai.azure.com",  # no separator
        "k" * 32,  # bare key
        "https://[bad|secret",  # unparseable URL
        "",
    ],
)
def test_matches_rejects_invalid_credentials(key):
    assert not PLUGIN.matches(key)


def test_azure_registered_and_dispatched():
    assert "azure" in {p.name for p in all_plugins()}
    assert dispatch(AZURE_KEY).name == "azure"


def test_mask_shows_resource_host():
    assert PLUGIN.mask(AZURE_KEY) == "Azure:res.openai.azure.com"
    assert PLUGIN.mask("sk-proj-abcdef") is None


# --------------------------------------------------------------------------- #
# stitch() hook — URL line + key line
# --------------------------------------------------------------------------- #
def test_stitch_joins_url_and_following_key_line():
    lines = ["https://res.openai.azure.com", "k" * 32]
    credential, consumed = PLUGIN.stitch(lines, 0)
    assert credential == AZURE_KEY
    assert consumed == {1}


def test_stitch_skips_blank_and_comment_lines_between_url_and_key():
    lines = ["https://res.openai.azure.com", "", "# a note", "k" * 32]
    credential, consumed = PLUGIN.stitch(lines, 0)
    assert credential == AZURE_KEY
    assert consumed == {3}


def test_stitch_bare_url_without_key_passes_through():
    # next line is too short to be a key -> the URL line is kept as-is
    lines = ["https://res.openai.azure.com", "short"]
    credential, consumed = PLUGIN.stitch(lines, 0)
    assert credential == "https://res.openai.azure.com"
    assert consumed == set()


def test_stitch_does_not_swallow_a_second_url():
    lines = ["https://a.openai.azure.com", "https://b.openai.azure.com"]
    credential, consumed = PLUGIN.stitch(lines, 0)
    assert credential == "https://a.openai.azure.com"
    assert consumed == set()


def test_stitch_ignores_unrelated_lines():
    assert PLUGIN.stitch(["sk-proj-abcdef"], 0) is None
    assert PLUGIN.stitch(["https://example.com"], 0) is None


def test_parse_credentials_folds_url_key_pair_end_to_end():
    raw = "https://res.openai.azure.com\n" + "k" * 32 + "\nsk-proj-other\n"
    creds = parse_credentials(raw)
    assert creds[0] == AZURE_KEY
    assert creds[1] == "sk-proj-other"
    assert dispatch(creds[0]).name == "azure"


# --------------------------------------------------------------------------- #
# extract_candidates() hook — azure_openai_pairs aggregate
# --------------------------------------------------------------------------- #
def test_extract_candidates_expands_and_canonicalizes_pairs():
    text = json.dumps(
        {
            "azure_openai_pairs": [
                {
                    "endpoint": "HTTPS://Res.OpenAI.Azure.Com/openai/v1/responses",
                    "api_key": "z" * 32,
                },
                {
                    "endpoint": "https://other.cognitiveservices.azure.com:443/some/path",
                    "api_key": "y" * 32,
                },
            ]
        }
    )
    assert PLUGIN.extract_candidates(text) == [
        "https://res.openai.azure.com|" + "z" * 32,
        "https://other.cognitiveservices.azure.com|" + "y" * 32,
    ]


def test_extract_candidates_skips_invalid_rows():
    text = json.dumps(
        {
            "azure_openai_pairs": [
                {"endpoint": "https://res.openai.azure.com@evil.example", "api_key": "a" * 32},
                {"endpoint": "http://res.openai.azure.com", "api_key": "b" * 32},
                {"endpoint": "https://evil.example", "api_key": "c" * 32},
                {"endpoint": "https://res.openai.azure.com:8443", "api_key": "d" * 32},
                {"endpoint": "https://res.openai.azure.com", "api_key": ""},
                {"endpoint": "https://[bad", "api_key": "e" * 32},
                "not-a-dict",
            ]
        }
    )
    # format recognized, every row invalid -> empty list (not None)
    assert PLUGIN.extract_candidates(text) == []


def test_extract_candidates_returns_none_for_non_aggregate():
    assert PLUGIN.extract_candidates("just some pasted text") is None
    assert PLUGIN.extract_candidates(json.dumps({"unrelated": 1})) is None
    assert PLUGIN.extract_candidates(json.dumps(["azure_openai_pairs"])) is None
    assert PLUGIN.extract_candidates("{not valid json azure_openai_pairs") is None


def test_extract_candidates_ignores_service_account_json():
    # a standalone GCP-style key carrying a lookalike metadata field stays one
    # credential — the aggregate hook must not claim it
    text = json.dumps(
        {
            "type": "service_account",
            "private_key": "pem",
            "client_email": "sa@proj.iam.gserviceaccount.com",
            "azure_openai_pairs": [
                {"endpoint": "https://res.openai.azure.com", "api_key": "z" * 32}
            ],
        }
    )
    assert PLUGIN.extract_candidates(text) is None


# --------------------------------------------------------------------------- #
# health / grade with mocked HTTP
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
async def test_health_alive_with_deployments():
    respx.get(url__regex=r"https://res\.openai\.azure\.com/openai/deployments.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-4o", "model": "gpt-4o", "status": "succeeded"},
                    {"id": "gpt-4o-mini", "model": "gpt-4o-mini", "status": "succeeded"},
                ]
            },
        )
    )
    client, ctx = await _ctx(mode=CheckMode.HEALTH)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.HEALTH)
    await PLUGIN.health_check(AZURE_KEY, r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.ALIVE
    assert r.alive is True
    assert any("2 个部署" in note for note in r.remarks)
    assert any("https://res.openai.azure.com" in note for note in r.remarks)


@pytest.mark.asyncio
@respx.mock
async def test_health_dead_on_401():
    respx.get(url__regex=r"https://res\.openai\.azure\.com/openai/deployments.*").mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "code": "401",
                    "message": "Access denied due to invalid subscription key.",
                }
            },
        )
    )
    client, ctx = await _ctx(mode=CheckMode.HEALTH)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.HEALTH)
    await PLUGIN.health_check(AZURE_KEY, r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.DEAD
    assert r.alive is False
    assert r.error


@pytest.mark.asyncio
@respx.mock
async def test_health_error_on_network_failure():
    respx.get(url__regex=r"https://res\.openai\.azure\.com/.*").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    client, ctx = await _ctx(mode=CheckMode.HEALTH)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.HEALTH)
    await PLUGIN.health_check(AZURE_KEY, r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.ERROR
    assert r.alive is not True
    assert r.error


@pytest.mark.asyncio
@respx.mock
async def test_grade_probes_deployments_and_reports_tpm_rpm():
    respx.get(url__regex=r"https://res\.openai\.azure\.com/openai/deployments.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "gpt-4o",
                        "model": "gpt-4o",
                        "status": "succeeded",
                        "scale_settings": {"scale_type": "standard", "capacity": 30},
                    },
                    {"id": "gpt-4o-mini", "model": "gpt-4o-mini", "status": "succeeded"},
                ]
            },
        )
    )
    respx.get(url__regex=r"https://res\.openai\.azure\.com/openai/models.*").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})
    )

    def probe_cb(request: httpx.Request) -> httpx.Response:
        if "/deployments/gpt-4o/" in str(request.url):
            return httpx.Response(
                200,
                json={"model": "gpt-4o"},
                headers={
                    "x-ratelimit-limit-tokens": "30000",
                    "x-ratelimit-limit-requests": "300",
                },
            )
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": "DeploymentNotFound",
                    "message": "The API deployment for this resource does not exist.",
                }
            },
        )

    respx.post(
        url__regex=r"https://res\.openai\.azure\.com/openai/deployments/.*/chat/completions.*"
    ).mock(side_effect=probe_cb)

    client, ctx = await _ctx(mode=CheckMode.GRADE)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.GRADE)
    await PLUGIN.grade_check(AZURE_KEY, r, ctx)
    await client.aclose()

    assert r.status == KeyStatus.GRADED
    assert r.alive is True
    assert r.tier == "Azure-1部署"
    assert r.details["endpoint"] == "https://res.openai.azure.com"
    # deployment inventory parsed, including scale capacity
    deps = {d["id"]: d for d in r.details["deployments"]}
    assert set(deps) == {"gpt-4o", "gpt-4o-mini"}
    assert deps["gpt-4o"]["capacity_ktpm"] == 30
    # probe captured the rate-limit headers
    probe = r.details["probes"]["gpt-4o"]
    assert probe["alive"] is True
    assert probe["x-ratelimit-limit-tokens"] == "30000"
    assert probe["x-ratelimit-limit-requests"] == "300"
    assert r.details["probes"]["gpt-4o-mini"]["alive"] is False
    # remarks summarize the working deployment with TPM/RPM
    assert any("✓ gpt-4o" in note and "TPM=30000" in note for note in r.remarks)
    # downloadable report attached
    assert r.download_filename == "azure-report.json"
    report = json.loads(r.download_text)
    assert report["endpoint"] == "https://res.openai.azure.com"
    assert "gpt-4o" in report["probes"]


@pytest.mark.asyncio
@respx.mock
async def test_grade_dead_on_401():
    respx.get(url__regex=r"https://res\.openai\.azure\.com/openai/deployments.*").mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "code": "401",
                    "message": "Access denied due to invalid subscription key.",
                }
            },
        )
    )
    client, ctx = await _ctx(mode=CheckMode.GRADE)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.GRADE)
    await PLUGIN.grade_check(AZURE_KEY, r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.DEAD
    assert r.alive is False
    assert r.error
