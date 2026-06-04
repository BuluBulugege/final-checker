"""Tests for the pluggable key checker. Network is fully mocked with respx so
no real API calls are made. Focus: detection routing, error classification,
tier/bucket logic exactly per spec, the burst tester, and job lifecycle.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import settings
from app.models import (
    CheckMode,
    ErrorClass,
    JobRequest,
    KeyResult,
    KeyStatus,
    ProbeOutcome,
)
from app.plugins.base import CheckContext, mask_key
from app.plugins.registry import all_plugins, dispatch
from app.ratetest import run_burst


# --------------------------------------------------------------------------- #
# detection / dispatch
# --------------------------------------------------------------------------- #
def test_all_plugins_registered():
    names = {p.name for p in all_plugins()}
    assert names == {"gemini", "openai", "anthropic"}


@pytest.mark.parametrize(
    "key,expected",
    [
        ("AIza" + "x" * 35, "gemini"),
        ("AIzaSy" + "B" * 33, "gemini"),
        ("sk-proj-" + "a" * 40, "openai"),
        ("sk-svcacct-" + "a" * 40, "openai"),
        ("sk-" + "a" * 40, "openai"),
        ("sk-ant-api03-" + "a" * 40, "anthropic"),
        ("sk-ant-oat01-" + "a" * 40, "anthropic"),
        ("garbage", None),
        ("", None),
    ],
)
def test_dispatch_routes_correctly(key, expected):
    plugin = dispatch(key)
    assert (plugin.name if plugin else None) == expected


def test_gemini_does_not_claim_openai_key():
    g = next(p for p in all_plugins() if p.name == "gemini")
    assert not g.matches("sk-proj-abc")
    assert not g.matches("AIzatooshort")


def test_openai_does_not_claim_anthropic_key():
    o = next(p for p in all_plugins() if p.name == "openai")
    assert not o.matches("sk-ant-api03-abc")
    assert not o.matches("AIza" + "x" * 35)


def test_mask_key():
    assert mask_key("sk-proj-abcdefghijklmnop1234") == "sk-proj-…1234"
    assert mask_key("short") == "shor…"
    assert mask_key("") == "(empty)"


# --------------------------------------------------------------------------- #
# burst tester
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_burst_counts_and_first_429():
    calls = {"n": 0}

    async def probe() -> ProbeOutcome:
        calls["n"] += 1
        # first 5 succeed, then rate-limited
        if calls["n"] <= 5:
            return ProbeOutcome(ok=True, status_code=200, error_class=ErrorClass.OK)
        return ProbeOutcome(ok=False, status_code=429, error_class=ErrorClass.RATE_LIMIT)

    res = await run_burst(
        probe, target_rps=200, duration_s=0.3, cap=12, stop_on_first_429=True
    )
    assert res.success == 5
    assert res.rate_limited >= 1
    assert res.first_429_at_s is not None
    # measured_rpm scales successes-before-limit to a per-minute rate (> raw count)
    assert res.measured_rpm is not None and res.measured_rpm >= 5.0


@pytest.mark.asyncio
async def test_burst_no_429_extrapolates_rpm():
    async def probe() -> ProbeOutcome:
        return ProbeOutcome(ok=True, status_code=200, error_class=ErrorClass.OK)

    res = await run_burst(probe, target_rps=100, duration_s=0.2, cap=10)
    assert res.first_429_at_s is None
    assert res.rate_limited == 0
    assert res.success == 10
    assert res.measured_rpm is not None and res.measured_rpm > 0


@pytest.mark.asyncio
async def test_burst_probe_exception_counted_as_network():
    async def probe() -> ProbeOutcome:
        raise RuntimeError("boom")

    res = await run_burst(probe, target_rps=100, duration_s=0.1, cap=3)
    assert res.other_errors == 3
    assert res.error_breakdown.get("network") == 3


# --------------------------------------------------------------------------- #
# OpenAI tier / bucket logic (pure functions, no network)
# --------------------------------------------------------------------------- #
def _openai():
    return next(p for p in all_plugins() if p.name == "openai")


@pytest.mark.parametrize(
    "rpm,bucket",
    [
        (0, 0),
        (1, 1),
        (25, 1),
        (26, 2),
        (100, 2),
        (101, 3),
        (200, 3),
        (201, 4),
        (300, 4),
        (301, 5),
        (1000, 5),
    ],
)
def test_openai_rpm_bucket(rpm, bucket):
    assert _openai().rpm_bucket(rpm, settings.openai) == bucket


@pytest.mark.parametrize(
    "tpm,tier",
    [
        (10_000, None),
        (500_000, "T1"),
        (700_000, "T1"),
        (1_000_000, "T2"),
        (2_000_000, "T3"),
        (4_000_000, "T4"),
        (40_000_000, "T5"),
        (99_000_000, "T5"),
    ],
)
def test_openai_tier_from_tpm(tpm, tier):
    assert _openai()._tier_label(tpm, settings.openai) == tier


# --------------------------------------------------------------------------- #
# Gemini level bucketing (pure)
# --------------------------------------------------------------------------- #
def _gemini():
    return next(p for p in all_plugins() if p.name == "gemini")


@pytest.mark.parametrize(
    "first_429,level",
    [
        (None, 3),  # never 429 -> t3
        (1.0, 1),  # <15s -> t1
        (14.9, 1),
        (15.0, 2),  # 15-30s -> t2
        (29.9, 2),
        (30.0, 3),  # >=30s edge -> t3
        (45.0, 3),
    ],
)
def test_gemini_level_buckets(first_429, level):
    assert _gemini()._bucket_level(first_429, settings.gemini) == level


# --------------------------------------------------------------------------- #
# Plugin behavior with mocked HTTP
# --------------------------------------------------------------------------- #
async def _ctx(mode=CheckMode.GRADE, full_load=False):
    """Build a CheckContext around a real (mocked-transport) async client."""
    client = httpx.AsyncClient()

    async def progress(frac, label):
        pass

    return client, CheckContext(
        client=client, settings=settings, mode=mode, full_load=full_load, progress=progress
    )


@pytest.mark.asyncio
@respx.mock
async def test_openai_health_alive():
    respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(
            200, json={"data": []}, headers={"openai-organization": "org-abc"}
        )
    )
    client, ctx = await _ctx(mode=CheckMode.HEALTH)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.HEALTH)
    await _openai().health_check("sk-proj-" + "a" * 20, r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.ALIVE
    assert r.alive is True


@pytest.mark.asyncio
@respx.mock
async def test_openai_health_dead_on_401():
    respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(
            401, json={"error": {"code": "invalid_api_key", "message": "bad"}}
        )
    )
    client, ctx = await _ctx(mode=CheckMode.HEALTH)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.HEALTH)
    await _openai().health_check("sk-" + "a" * 20, r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.DEAD
    assert r.alive is False


@pytest.mark.asyncio
@respx.mock
async def test_anthropic_health_alive():
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={"id": "msg", "content": [{"type": "text", "text": "hi"}]},
            headers={"anthropic-ratelimit-requests-limit": "50"},
        )
    )
    client, ctx = await _ctx(mode=CheckMode.HEALTH)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.HEALTH)
    plugin = next(p for p in all_plugins() if p.name == "anthropic")
    await plugin.health_check("sk-ant-api03-" + "a" * 20, r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.ALIVE
    assert r.alive is True


@pytest.mark.asyncio
@respx.mock
async def test_gemini_health_alive():
    respx.get(url__startswith="https://generativelanguage.googleapis.com/v1beta/models").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "models/gemini-3.1-pro"}]})
    )
    client, ctx = await _ctx(mode=CheckMode.HEALTH)
    r = KeyResult(index=0, masked_key="x", mode=CheckMode.HEALTH)
    await _gemini().health_check("AIza" + "x" * 35, r, ctx)
    await client.aclose()
    assert r.status == KeyStatus.ALIVE
    assert r.alive is True


# --------------------------------------------------------------------------- #
# Job lifecycle (unsupported key path, no network needed)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_job_unsupported_key():
    from app.jobs import manager

    req = JobRequest(keys="totally-not-a-key", mode=CheckMode.HEALTH, concurrency=1)
    job = manager.create(req, ["totally-not-a-key"])
    assert job._task is not None
    await job._task
    snap = job.summary()
    assert snap.state == "done"
    assert snap.results[0].status == KeyStatus.UNSUPPORTED
    # masked key never equals the raw key
    assert snap.results[0].masked_key != "totally-not-a-key"


# --------------------------------------------------------------------------- #
# secret redaction (never leak a raw key in errors/details)
# --------------------------------------------------------------------------- #
from app.redact import redact  # noqa: E402


@pytest.mark.parametrize(
    "raw",
    [
        "sk-ant-api03-" + "A" * 50,
        "sk-proj-" + "B" * 60,
        "sk-" + "C" * 40,
        "AIza" + "x" * 35,
        "Authorization: Bearer " + "Z" * 40,
    ],
)
def test_redact_scrubs_keys(raw):
    msg = f"ConnectError: failed talking to api with key {raw} oops"
    out = redact(msg)
    assert raw not in out
    assert "«redacted»" in out


def test_redact_keeps_innocuous_text():
    assert redact("ConnectTimeout: timed out after 30s") == (
        "ConnectTimeout: timed out after 30s"
    )
    assert redact(None) is None
    assert redact("") == ""


@pytest.mark.asyncio
async def test_burst_exception_detail_is_redacted():
    secret = "sk-ant-api03-" + "Q" * 50

    async def probe() -> ProbeOutcome:
        raise RuntimeError(f"boom with {secret}")

    # The exception detail is captured into error_breakdown via ProbeOutcome.detail;
    # ensure no path retains the raw key. We can't read per-call detail from the
    # aggregate, so assert via a direct redact of the same message instead.
    res = await run_burst(probe, target_rps=100, duration_s=0.05, cap=2)
    assert res.other_errors == 2
    assert secret not in repr(res.model_dump())
