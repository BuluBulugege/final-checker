"""Tests for the AWS Bedrock plugin: AKIA credential detection, the pure-Python
SigV4 signer (verified against the official AWS documentation test vectors),
the stitch()/extract_candidates() parsing hooks, and health/grade behavior
against fully mocked HTTP (respx — no real network)."""

from __future__ import annotations

import datetime
import json
import types

import httpx
import pytest
import respx

from app.config import settings
from app.models import CheckMode, KeyResult, KeyStatus
from app.parsing import parse_credentials
from app.plugins import aws_bedrock
from app.plugins.aws_bedrock import PLUGIN, _sigv4_headers, _signing_key
from app.plugins.base import CheckContext
from app.plugins.registry import all_plugins, dispatch

AK = "AKIA" + "A" * 16
SK = "s" * 40
CRED = f"{AK}:{SK}"

STS_XML = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <GetCallerIdentityResult>
    <Arn>arn:aws:iam::123456789012:user/test-user</Arn>
    <UserId>AIDAEXAMPLEUSERID</UserId>
    <Account>123456789012</Account>
  </GetCallerIdentityResult>
  <ResponseMetadata><RequestId>req-1</RequestId></ResponseMetadata>
</GetCallerIdentityResponse>"""

STS_ERROR_XML = """<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <Error>
    <Type>Sender</Type>
    <Code>InvalidClientTokenId</Code>
    <Message>The security token included in the request is invalid.</Message>
  </Error>
  <RequestId>req-2</RequestId>
</ErrorResponse>"""


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "key",
    [
        CRED,
        f"{CRED}:us-west-2",  # optional region suffix
        "  " + CRED + "  ",  # surrounding whitespace
    ],
)
def test_matches_accepts_akia_credentials(key):
    assert PLUGIN.matches(key)


@pytest.mark.parametrize(
    "key",
    [
        f"ASIA{'C' * 16}:{SK}",  # temporary creds need a session token — skipped
        AK,  # bare access key, no secret
        f"AKIA{'A' * 15}:{SK}",  # access key too short
        f"akia{'a' * 16}:{SK}",  # wrong case
        f"BKIA{'A' * 16}:{SK}",  # wrong prefix
        "garbage",
        "",
    ],
)
def test_matches_rejects_non_akia_or_malformed(key):
    assert not PLUGIN.matches(key)


def test_bedrock_registered_and_dispatched():
    assert "aws_bedrock" in {p.name for p in all_plugins()}
    assert dispatch(CRED).name == "aws_bedrock"
    assert dispatch(f"ASIA{'C' * 16}:{SK}") is None  # ASIA is unsupported


def test_parse_splits_access_secret_region():
    assert PLUGIN._parse(CRED) == (AK, SK, None)
    assert PLUGIN._parse(f"{CRED}:us-west-2") == (AK, SK, "us-west-2")
    assert PLUGIN._parse(f"{CRED}:") == (AK, SK, None)


def test_mask_shows_access_key_id_only():
    masked = PLUGIN.mask(CRED)
    assert masked == f"AWS:{AK}"
    assert SK not in masked
    assert PLUGIN.mask("sk-proj-abcdef") is None


# --------------------------------------------------------------------------- #
# SigV4 signing — official AWS documentation test vectors
# --------------------------------------------------------------------------- #
class _FrozenDatetime(datetime.datetime):
    @classmethod
    def utcnow(cls):
        return cls(2015, 8, 30, 12, 36, 0)


def _freeze_time(monkeypatch):
    monkeypatch.setattr(
        aws_bedrock, "datetime", types.SimpleNamespace(datetime=_FrozenDatetime)
    )


def test_signing_key_matches_aws_doc_vector():
    # AWS docs "Examples of how to derive a signing key":
    # secret wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY, 20120215, us-east-1, iam
    key = _signing_key(
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20120215", "us-east-1", "iam"
    )
    assert key.hex() == (
        "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"
    )


def test_sigv4_authorization_matches_aws_doc_vector(monkeypatch):
    # AWS SigV4 docs example: GET iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08
    # at 20150830T123600Z with content-type;host;x-amz-date signed.
    _freeze_time(monkeypatch)
    headers = _sigv4_headers(
        "GET",
        "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        b"",
        "AKIDEXAMPLE",
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "us-east-1",
        "iam",
        {"content-type": "application/x-www-form-urlencoded; charset=utf-8"},
    )
    assert headers["x-amz-date"] == "20150830T123600Z"
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )


def test_sigv4_deterministic_and_host_header_excluded(monkeypatch):
    _freeze_time(monkeypatch)
    args = ("POST", "https://sts.amazonaws.com/", b"Action=GetCallerIdentity&Version=2011-06-15")
    kw = {
        "access_key": AK,
        "secret_key": SK,
        "region": "us-east-1",
        "service": "sts",
        "extra_headers": {"content-type": "application/x-www-form-urlencoded"},
    }
    h1 = _sigv4_headers(*args, **kw)
    h2 = _sigv4_headers(*args, **kw)
    assert h1 == h2  # same inputs + same clock -> identical signature
    # host is used for signing but left for httpx to set from the URL
    assert "host" not in h1
    assert "SignedHeaders=content-type;host;x-amz-date" in h1["authorization"]
    assert h1["content-type"] == "application/x-www-form-urlencoded"


def test_parse_sts_xml_extracts_identity_fields():
    identity = PLUGIN._parse_sts_xml(STS_XML.encode())
    assert identity == {
        "Arn": "arn:aws:iam::123456789012:user/test-user",
        "UserId": "AIDAEXAMPLEUSERID",
        "Account": "123456789012",
    }
    assert PLUGIN._parse_sts_xml(b"<unrelated/>") is None


# --------------------------------------------------------------------------- #
# stitch() hook — AWS_ACCESS_KEY_ID= / AWS_SECRET_ACCESS_KEY= env pairs
# --------------------------------------------------------------------------- #
def test_stitch_joins_env_var_pair():
    lines = [f"AWS_ACCESS_KEY_ID={AK}", f"AWS_SECRET_ACCESS_KEY={SK}"]
    credential, consumed = PLUGIN.stitch(lines, 0)
    assert credential == CRED
    assert consumed == {1}


def test_stitch_env_var_pair_is_case_insensitive():
    lines = [f"aws_access_key_id={AK}", f"aws_secret_access_key={SK}"]
    credential, consumed = PLUGIN.stitch(lines, 0)
    assert credential == CRED
    assert consumed == {1}


def test_stitch_swallows_orphan_secret_line():
    lines = [f"AWS_SECRET_ACCESS_KEY={SK}"]
    credential, consumed = PLUGIN.stitch(lines, 0)
    assert credential is None  # consumed silently, not surfaced as a credential
    assert consumed == set()


def test_stitch_access_key_without_secret_passes_through():
    lines = [f"AWS_ACCESS_KEY_ID={AK}", "unrelated-line"]
    credential, consumed = PLUGIN.stitch(lines, 0)
    assert credential == f"AWS_ACCESS_KEY_ID={AK}"
    assert consumed == set()


def test_stitch_ignores_unrelated_lines():
    assert PLUGIN.stitch(["sk-proj-abcdef"], 0) is None
    assert PLUGIN.stitch([CRED], 0) is None  # already-joined creds need no stitch


def test_parse_credentials_folds_env_pair_and_swallows_orphan():
    raw = (
        f"AWS_ACCESS_KEY_ID={AK}\n"
        f"AWS_SECRET_ACCESS_KEY={SK}\n"
        f"AWS_SECRET_ACCESS_KEY={'t' * 40}\n"  # orphan — silently dropped
        "sk-proj-other\n"
    )
    creds = parse_credentials(raw)
    assert creds == [CRED, "sk-proj-other"]
    assert dispatch(creds[0]).name == "aws_bedrock"


# --------------------------------------------------------------------------- #
# extract_candidates() hook — aws_iam_pairs aggregate
# --------------------------------------------------------------------------- #
def test_extract_candidates_expands_akia_rows_and_skips_asia():
    text = json.dumps(
        {
            "aws_iam_pairs": [
                {"access_key_id": AK, "secret_access_key": SK},
                {
                    "access_key_id": "ASIA" + "C" * 16,
                    "secret_access_key": "t" * 40,
                    "session_token": "tok",
                },
                {"access_key_id": "AKIA" + "B" * 16},  # missing secret
                "not-a-dict",
            ]
        }
    )
    assert PLUGIN.extract_candidates(text) == [CRED]


def test_extract_candidates_returns_none_for_non_aggregate():
    assert PLUGIN.extract_candidates("just some pasted text") is None
    assert PLUGIN.extract_candidates(json.dumps({"unrelated": 1})) is None
    assert PLUGIN.extract_candidates("{not valid json aws_iam_pairs") is None


def test_extract_candidates_ignores_service_account_json():
    text = json.dumps(
        {
            "type": "service_account",
            "private_key": "pem",
            "client_email": "sa@proj.iam.gserviceaccount.com",
            "aws_iam_pairs": [{"access_key_id": AK, "secret_access_key": SK}],
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
async def test_health_alive_with_sts_identity():
    respx.post("https://sts.amazonaws.com/").mock(
        return_value=httpx.Response(200, text=STS_XML)
    )
    client, ctx = await _ctx(mode=CheckMode.HEALTH)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.HEALTH)
    await PLUGIN.health_check(CRED, r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.ALIVE
    assert r.alive is True
    assert any("123456789012" in note for note in r.remarks)
    assert any("arn:aws:iam::123456789012:user/test-user" in note for note in r.remarks)


@pytest.mark.asyncio
@respx.mock
async def test_health_dead_on_bad_credentials():
    respx.post("https://sts.amazonaws.com/").mock(
        return_value=httpx.Response(403, text=STS_ERROR_XML)
    )
    client, ctx = await _ctx(mode=CheckMode.HEALTH)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.HEALTH)
    await PLUGIN.health_check(CRED, r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.DEAD
    assert r.alive is False
    assert r.error


def _mock_bedrock_region_scan():
    """us-east-1 lists one ACTIVE ON_DEMAND Claude model; other regions empty."""

    def models_cb(request: httpx.Request) -> httpx.Response:
        region = request.url.host.split(".")[1]
        if region == "us-east-1":
            return httpx.Response(
                200,
                json={
                    "modelSummaries": [
                        {
                            "modelId": "anthropic.claude-sonnet-4-6",
                            "modelLifecycle": {"status": "ACTIVE"},
                            "inferenceTypesSupported": ["ON_DEMAND"],
                        },
                        {
                            "modelId": "anthropic.claude-legacy",
                            "modelLifecycle": {"status": "LEGACY"},
                            "inferenceTypesSupported": ["ON_DEMAND"],
                        },
                    ]
                },
            )
        return httpx.Response(200, json={"modelSummaries": []})

    respx.get(
        url__regex=r"https://bedrock\.[a-z0-9-]+\.amazonaws\.com/foundation-models$"
    ).mock(side_effect=models_cb)

    def converse_cb(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "claude-sonnet-4-6" in path and "arn" not in path:
            return httpx.Response(
                200,
                json={
                    "output": {"message": {"content": [{"text": "OK"}]}},
                    "usage": {"inputTokens": 5, "outputTokens": 2},
                },
            )
        return httpx.Response(
            400, json={"message": "The provided model identifier is invalid."}
        )

    respx.post(
        url__regex=r"https://bedrock-runtime\.[a-z0-9-]+\.amazonaws\.com/model/.+/converse"
    ).mock(side_effect=converse_cb)

    # fable-5 access request is answered "already enabled" so no retest happens
    respx.put(
        url__regex=r"https://bedrock\.[a-z0-9-]+\.amazonaws\.com/foundation-model-entitlement"
    ).mock(
        return_value=httpx.Response(400, json={"message": "Model access already enabled"})
    )


@pytest.mark.asyncio
@respx.mock
async def test_grade_scans_regions_and_probes_claude():
    respx.post("https://sts.amazonaws.com/").mock(
        return_value=httpx.Response(200, text=STS_XML)
    )
    _mock_bedrock_region_scan()

    client, ctx = await _ctx(mode=CheckMode.GRADE)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.GRADE)
    await PLUGIN.grade_check(CRED, r, ctx)
    await client.aclose()

    assert r.status == KeyStatus.GRADED
    assert r.alive is True
    assert r.tier == "Bedrock-1模型(Claude:1)"
    # identity surfaced into details + remarks
    assert r.details["identity"]["Account"] == "123456789012"
    assert r.details["identity"]["Arn"] == "arn:aws:iam::123456789012:user/test-user"
    assert any("123456789012" in note for note in r.remarks)
    # region scan: only us-east-1 had an ACTIVE ON_DEMAND model (LEGACY filtered)
    assert r.details["models_by_region"] == {
        "us-east-1": ["anthropic.claude-sonnet-4-6"]
    }
    assert r.details["by_provider"] == {"anthropic": ["anthropic.claude-sonnet-4-6"]}
    # the listed Claude model conversed successfully; unknown ones did not
    probe = r.details["probes"]["anthropic.claude-sonnet-4-6"]
    assert probe["ok"] is True
    assert probe["usage"] == {"inputTokens": 5, "outputTokens": 2}
    assert r.details["probes"]["anthropic.claude-fable-5"]["ok"] is False
    # per-region Claude detail recorded
    assert "anthropic.claude-sonnet-4-6" in r.details["claude_per_region"]
    # downloadable report attached
    assert r.download_filename == "bedrock-123456789012.json"
    report = json.loads(r.download_text)
    assert report["identity"]["Account"] == "123456789012"
    assert report["regions_scanned"] == ["us-east-1"]
    assert report["total_unique_models"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_grade_dead_on_bad_credentials():
    respx.post("https://sts.amazonaws.com/").mock(
        return_value=httpx.Response(403, text=STS_ERROR_XML)
    )
    client, ctx = await _ctx(mode=CheckMode.GRADE)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.GRADE)
    await PLUGIN.grade_check(CRED, r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.DEAD
    assert r.alive is False
    assert "STS" in r.error
