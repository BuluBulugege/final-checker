"""Focused tests for LongTermKeyManager._test_key_health status mapping.

These lock in the production regression fix where the monitor must call the
current plugin API (`health_check`, not the removed `check`) and map the
current KeyStatus vocabulary (ALIVE/GRADED → "alive") rather than the removed
KeyStatus.SUCCESS. Network is never touched: a fake plugin mutates the result.
"""

from __future__ import annotations

import pytest

import app.long_term_monitor as ltm
from app.models import KeyStatus


class _FakePlugin:
    """Stand-in plugin whose health_check sets a chosen status (or raises)."""

    name = "fake"

    def __init__(self, status=None, raises=False):
        self._status = status
        self._raises = raises

    def matches(self, key: str) -> bool:  # pragma: no cover - not used here
        return True

    async def health_check(self, key, result, ctx) -> None:
        if self._raises:
            raise RuntimeError("boom")
        result.status = self._status
        result.alive = self._status in {KeyStatus.ALIVE, KeyStatus.GRADED}

    async def grade_check(self, key, result, ctx) -> None:  # pragma: no cover
        pass


@pytest.fixture
def manager():
    # No db_path → uses default in-memory-ish init; _test_key_health never writes.
    return ltm.LongTermKeyManager.__new__(ltm.LongTermKeyManager)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (KeyStatus.ALIVE, "alive"),
        (KeyStatus.GRADED, "alive"),
        (KeyStatus.DEAD, "dead"),
        (KeyStatus.ERROR, "dead"),
    ],
)
async def test_test_key_health_maps_status(monkeypatch, manager, status, expected):
    monkeypatch.setattr(ltm, "dispatch", lambda key: _FakePlugin(status=status))
    out = await manager._test_key_health("sk-fake")
    assert out["status"] == expected


@pytest.mark.asyncio
async def test_test_key_health_exception_is_dead(monkeypatch, manager):
    monkeypatch.setattr(ltm, "dispatch", lambda key: _FakePlugin(raises=True))
    out = await manager._test_key_health("sk-fake")
    assert out["status"] == "dead"
    assert out["error_detail"]


@pytest.mark.asyncio
async def test_test_key_health_unsupported(monkeypatch, manager):
    monkeypatch.setattr(ltm, "dispatch", lambda key: None)
    out = await manager._test_key_health("garbage")
    assert out["status"] == "dead"
    assert out["error_class"] == "unsupported"
