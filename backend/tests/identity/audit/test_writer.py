"""AuditBatchWriter lifecycle, fallback, and batching."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.gateway.identity.audit.events import AuditEvent
from app.gateway.identity.audit.fallback import FallbackLog
from app.gateway.identity.audit.writer import AuditBatchWriter

pytestmark = pytest.mark.asyncio


def _ev(action: str = "thread.created", result: str = "success", **kw) -> AuditEvent:
    return AuditEvent(action=action, result=result, **kw)


class _FakeSession:
    """Minimal async session stand-in that records executed inserts."""

    def __init__(self, sink: list[list[dict]], *, raise_on_execute: bool = False) -> None:
        self._sink = sink
        self._raise = raise_on_execute

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, _stmt, rows=None):
        if self._raise:
            raise RuntimeError("pg offline")
        if rows is not None:
            self._sink.append(list(rows))
        return MagicMock()

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _FakeMaker:
    def __init__(self, sink: list[list[dict]], *, raise_on_execute: bool = False) -> None:
        self._sink = sink
        self._raise = raise_on_execute

    def __call__(self):
        return _FakeSession(self._sink, raise_on_execute=self._raise)


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


async def test_enqueue_and_flush_writes_batch(tmp_path):
    sink: list[list[dict]] = []
    writer = AuditBatchWriter(
        _FakeMaker(sink),
        fallback=FallbackLog(tmp_path),
        queue_max=100,
        flush_interval_sec=0.05,
        batch_size=50,
    )
    await writer.start()
    try:
        for i in range(3):
            await writer.enqueue(_ev(action="thread.created", user_id=i))
        # Wait up to ~0.5s for flush.
        for _ in range(20):
            if writer.metrics["written"] >= 3:
                break
            await asyncio.sleep(0.05)
    finally:
        await writer.stop()

    assert writer.metrics["written"] == 3
    assert sum(len(b) for b in sink) == 3


async def test_stop_drains_remaining_events(tmp_path):
    sink: list[list[dict]] = []
    writer = AuditBatchWriter(
        _FakeMaker(sink),
        fallback=FallbackLog(tmp_path),
        flush_interval_sec=10.0,  # long, force draining via stop
        batch_size=5,
    )
    await writer.start()
    for i in range(7):
        await writer.enqueue(_ev(action="thread.created", user_id=i))
    await writer.stop()

    total = sum(len(b) for b in sink)
    assert total == 7


# ----------------------------------------------------------------------
# Queue full + critical → sync write
# ----------------------------------------------------------------------


async def test_queue_full_critical_sync_writes(tmp_path):
    sink: list[list[dict]] = []
    writer = AuditBatchWriter(
        _FakeMaker(sink),
        fallback=FallbackLog(tmp_path),
        queue_max=1,
        flush_interval_sec=10.0,
    )
    # Don't start flush loop — we want the queue to stay full.
    # Manually put one event so queue is at capacity.
    writer._queue.put_nowait(_ev("thread.created"))

    await writer.enqueue(_ev("authz.tool.denied", user_id=1), critical=True)

    assert writer.metrics["sync_writes"] == 1
    # One sync batch written.
    assert any(len(batch) == 1 and batch[0]["action"] == "authz.tool.denied" for batch in sink)


async def test_queue_full_noncritical_dropped(tmp_path):
    sink: list[list[dict]] = []
    dropped: list[AuditEvent] = []
    writer = AuditBatchWriter(
        _FakeMaker(sink),
        fallback=FallbackLog(tmp_path),
        queue_max=1,
        flush_interval_sec=10.0,
        on_drop=lambda ev: dropped.append(ev),
    )
    writer._queue.put_nowait(_ev("thread.created"))

    await writer.enqueue(_ev("thread.created", user_id=99), critical=False)

    assert writer.metrics["dropped"] == 1
    assert dropped and dropped[0].user_id == 99


# ----------------------------------------------------------------------
# PG offline → fallback write + backfill on recovery
# ----------------------------------------------------------------------


async def test_pg_offline_fallback_writes_critical_events(tmp_path):
    sink: list[list[dict]] = []
    writer = AuditBatchWriter(
        _FakeMaker(sink, raise_on_execute=True),
        fallback=FallbackLog(tmp_path),
        queue_max=1,
        flush_interval_sec=10.0,
    )
    writer._queue.put_nowait(_ev("thread.created"))  # filler

    await writer.enqueue(_ev("authz.tool.denied", user_id=1), critical=True)

    assert writer.metrics["sync_writes"] == 1
    assert writer.metrics["flush_errors"] == 1
    assert writer.metrics["fallback_written"] == 1
    # JSONL must contain the event.
    content = writer._fallback.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(content) == 1
    assert "authz.tool.denied" in content[0]


async def test_backfill_after_pg_recovers(tmp_path):
    """On recovery, the next flush should replay queued fallback events."""

    sink: list[list[dict]] = []

    class FlakyMaker:
        def __init__(self) -> None:
            self.down = True

        def __call__(self):
            return _FakeSession(sink, raise_on_execute=self.down)

    maker = FlakyMaker()
    writer = AuditBatchWriter(
        maker,
        fallback=FallbackLog(tmp_path),
        flush_interval_sec=0.05,
    )

    # Pre-populate fallback (simulate prior outage).
    await writer._fallback.write(_ev("authz.tool.denied", user_id=5))
    await writer._fallback.write(_ev("user.login.success", user_id=5))
    maker.down = False  # PG back online

    await writer.start()
    try:
        # Push a normal event to trigger the flush loop.
        await writer.enqueue(_ev("thread.created", user_id=5))
        for _ in range(20):
            if writer.metrics["fallback_backfilled"] >= 2:
                break
            await asyncio.sleep(0.05)
    finally:
        await writer.stop()

    assert writer.metrics["fallback_backfilled"] == 2
    assert not writer._fallback.path.exists()
