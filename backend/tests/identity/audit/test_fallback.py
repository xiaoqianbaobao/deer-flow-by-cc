"""FallbackLog JSONL rotation semantics."""

from __future__ import annotations

import pytest

from app.gateway.identity.audit.events import AuditEvent
from app.gateway.identity.audit.fallback import FallbackLog, fallback_path

pytestmark = pytest.mark.asyncio


def _ev(action: str, **kw) -> AuditEvent:
    return AuditEvent(action=action, result=kw.pop("result", "success"), **kw)


async def test_fallback_path_creates_parent_dir(tmp_path):
    p = fallback_path(tmp_path)
    assert p.parent.exists()
    assert p.parent.name == "_audit"


async def test_write_then_drain_round_trip(tmp_path):
    log = FallbackLog(tmp_path)
    a = _ev("user.login.success", user_id=1, tenant_id=1)
    b = _ev("authz.tool.denied", user_id=1, tenant_id=1)
    await log.write(a)
    await log.write(b)

    drained = await log.drain()
    assert [e.action for e in drained] == ["user.login.success", "authz.tool.denied"]
    assert not log.path.exists()


async def test_drain_empty_returns_empty_list(tmp_path):
    log = FallbackLog(tmp_path)
    assert await log.drain() == []


async def test_write_many(tmp_path):
    log = FallbackLog(tmp_path)
    events = [_ev(f"action.{i}") for i in range(5)]
    await log.write_many(events)

    drained = await log.drain()
    assert [e.action for e in drained] == [f"action.{i}" for i in range(5)]


async def test_drain_tolerates_blank_lines(tmp_path):
    log = FallbackLog(tmp_path)
    await log.write(_ev("user.login.success"))
    # Inject a blank line in the middle.
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    await log.write(_ev("user.logout"))

    drained = await log.drain()
    assert [e.action for e in drained] == ["user.login.success", "user.logout"]
