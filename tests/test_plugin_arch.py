"""Plugin architecture hardening tests: metadata protocol, duplicate-name
guard, explicit dispatch priority, plugin-driven unsupported reason, and
Azure config wiring. Network is fully mocked with respx.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import settings
from app.models import CheckMode, JobRequest, KeyResult
from app.plugins.base import CheckContext, CheckerPlugin, PluginMeta
from app.plugins.registry import all_plugins, dispatch, register


# --------------------------------------------------------------------------- #
# S4 — duplicate plugin names are a loud error
# --------------------------------------------------------------------------- #
class _DummyGemini(CheckerPlugin):
    meta = PluginMeta(name="gemini")  # collides with the real gemini plugin

    def matches(self, key: str) -> bool:
        return False

    async def health_check(self, key, result, ctx) -> None:
        pass

    async def grade_check(self, key, result, ctx) -> None:
        pass


def test_register_duplicate_name_raises():
    all_plugins()  # force discovery so the real gemini is registered
    with pytest.raises(ValueError) as excinfo:
        register(_DummyGemini())
    msg = str(excinfo.value)
    assert "duplicate plugin name 'gemini'" in msg
    # both source modules are named: the newcomer and the existing one
    assert __name__ in msg
    assert "app.plugins.gemini" in msg


# --------------------------------------------------------------------------- #
# M9 — explicit priority reproduces the historical dispatch order
# --------------------------------------------------------------------------- #
def test_dispatch_order_is_explicit_and_openai_is_last():
    plugins = all_plugins()
    assert [p.name for p in plugins] == [
        "anthropic",
        "aws_bedrock",
        "azure",
        "gcp",
        "gemini",
        "openai",
    ]
    priorities = [p.meta.priority for p in plugins]
    assert priorities == sorted(priorities)
    assert plugins[-1].meta.priority == 90  # openai: permissive sk- matcher last
    # the ordering actually matters: sk-ant- must hit anthropic, not openai
    assert dispatch("sk-ant-api03-" + "a" * 40).name == "anthropic"
    assert dispatch("sk-" + "a" * 40).name == "openai"


def test_all_plugins_declare_complete_meta():
    for p in all_plugins():
        assert p.meta.version
        assert p.meta.description
        assert p.meta.key_format_hint
        assert p.meta.capabilities == ["health", "grade"]
        assert p.meta.enabled is True
        assert p.name == p.meta.name


# --------------------------------------------------------------------------- #
# S3 — /api/plugins returns full metadata objects
# --------------------------------------------------------------------------- #
def test_plugins_endpoint_returns_full_metadata():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.get("/api/plugins")
    assert response.status_code == 200
    plugins = response.json()["plugins"]
    assert [p["name"] for p in plugins] == [
        "anthropic",
        "aws_bedrock",
        "azure",
        "gcp",
        "gemini",
        "openai",
    ]
    for p in plugins:
        assert set(p) >= {
            "name",
            "version",
            "description",
            "key_format_hint",
            "capabilities",
            "priority",
            "enabled",
        }
        assert p["version"]
        assert p["description"]
        assert p["key_format_hint"]
        assert p["capabilities"] == ["health", "grade"]
        assert isinstance(p["priority"], int)
        assert p["enabled"] is True


# --------------------------------------------------------------------------- #
# S2 — unsupported reason is generated from plugin metadata (all providers)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unsupported_reason_lists_every_registered_plugin():
    from app.jobs import manager

    req = JobRequest(keys="totally-not-a-key", mode=CheckMode.HEALTH, concurrency=1)
    job = manager.create(req, ["totally-not-a-key"])
    await job._task
    result = job.summary().results[0]
    assert result.error is not None
    for p in all_plugins():
        assert p.meta.key_format_hint in result.error
    # providers missing from the old hardcoded text are now mentioned
    assert "AKIA" in result.error
    assert "azure" in result.error.lower()


# --------------------------------------------------------------------------- #
# M5 — AzureConfig is actually wired in (api_version + timeout)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_azure_uses_configured_api_version_and_timeout(monkeypatch):
    import app.plugins.azure as azure_mod
    from app.plugins.azure import PLUGIN as azure

    monkeypatch.setattr(settings.azure, "api_version", "2099-01-01-preview")
    monkeypatch.setattr(settings.azure, "request_timeout_s", 12.5)

    seen: dict[str, list] = {"urls": [], "timeouts": []}
    real_timed_request = azure_mod.timed_request

    async def spy(client, method, url, **kw):
        seen["urls"].append(url)
        seen["timeouts"].append(kw.get("timeout"))
        return await real_timed_request(client, method, url, **kw)

    monkeypatch.setattr(azure_mod, "timed_request", spy)

    async def noop_progress(frac, label) -> None:
        pass

    with respx.mock:
        respx.get(url__regex=r"https://res\.openai\.azure\.com/openai/deployments.*").respond(
            200, json={"data": []}
        )
        respx.get(url__regex=r"https://res\.openai\.azure\.com/openai/models.*").respond(
            200, json={"data": [{"id": "gpt-4o"}]}
        )
        async with httpx.AsyncClient() as client:
            ctx = CheckContext(
                client=client,
                settings=settings,
                mode=CheckMode.HEALTH,
                full_load=False,
                progress=noop_progress,
            )
            r = KeyResult(index=0, masked_key="x", mode=CheckMode.HEALTH)
            await azure.health_check("https://res.openai.azure.com|" + "k" * 32, r, ctx)

    assert r.alive is True
    assert seen["urls"], "plugin made no requests"
    assert all("api-version=2099-01-01-preview" in u for u in seen["urls"])
    assert seen["timeouts"] and all(t == 12.5 for t in seen["timeouts"])


def test_azure_config_defaults_match_previous_hardcoded_behavior():
    # api_version is the first entry of the plugin's built-in fallback list,
    # and the timeout equals the job client's 60s default — wiring the config
    # in must not change runtime behavior.
    assert settings.azure.api_version == "2024-10-21"
    assert settings.azure.request_timeout_s == 60.0
