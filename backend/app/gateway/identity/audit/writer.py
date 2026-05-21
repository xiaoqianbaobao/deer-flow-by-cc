"""Async batch writer for audit events (spec §9.3).

Pipeline per event:

1. ``enqueue(event, critical=bool)`` — puts on a bounded ``asyncio.Queue``.
   On queue-full + critical → sync write (blocks for one round trip).
   On queue-full + non-critical → drop + metric.
2. Background ``_flush_loop`` drains up to ``batch_size`` every
   ``flush_interval_sec`` seconds and issues one ``executemany`` insert.
3. PG failures route critical events to :class:`FallbackLog`; on next
   flush we opportunistically backfill the JSONL before new writes.

Metrics are opaque counters on the instance — easy to expose in M7.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.gateway.identity.audit.events import AuditEvent
from app.gateway.identity.audit.fallback import FallbackLog
from app.gateway.identity.models.audit import AuditLog

logger = logging.getLogger(__name__)

# Drain wait on stop() before giving up.
_STOP_DRAIN_TIMEOUT_SEC = 5.0


def _event_to_row(event: AuditEvent) -> dict:
    """Map an AuditEvent to an INSERT-ready dict keyed by column name.

    The ``metadata`` column is renamed ``log_metadata`` on the ORM to avoid
    the SQLAlchemy reserved attribute; we must use the column name here.
    """

    d = asdict(event)
    d["metadata"] = d.pop("metadata", {}) or {}
    return d


class AuditBatchWriter:
    """Background-task batch inserter for :class:`AuditEvent`s.

    The writer starts a single flush loop in :meth:`start` and cleans it
    up in :meth:`stop` (drains up to ``_STOP_DRAIN_TIMEOUT_SEC`` seconds).

    :param session_maker: async sessionmaker bound to the identity engine.
    :param fallback: local JSONL log used on PG failure + queue overflow
        + critical writes.
    :param queue_max: bound for the in-memory queue.
    :param flush_interval_sec: max wait between flushes (even if batch
        isn't full).
    :param batch_size: max rows per flush.
    :param on_drop: test hook — called when a non-critical event is
        dropped because the queue is full.
    """

    def __init__(
        self,
        session_maker: async_sessionmaker,
        *,
        fallback: FallbackLog,
        queue_max: int = 10_000,
        flush_interval_sec: float = 1.0,
        batch_size: int = 500,
        on_drop: Callable[[AuditEvent], Awaitable[None] | None] | None = None,
    ) -> None:
        self._maker = session_maker
        self._fallback = fallback
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(maxsize=queue_max)
        self._flush_interval_sec = flush_interval_sec
        self._batch_size = batch_size
        self._on_drop = on_drop

        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()

        # Metrics — read-only from the outside.
        self.metrics: dict[str, int] = {
            "enqueued": 0,
            "written": 0,
            "dropped": 0,
            "fallback_written": 0,
            "fallback_backfilled": 0,
            "flush_errors": 0,
            "sync_writes": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._flush_loop(), name="audit-batch-writer")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_evt.set()
        try:
            await asyncio.wait_for(self._task, timeout=_STOP_DRAIN_TIMEOUT_SEC)
        except TimeoutError:
            logger.warning("audit writer stop timed out; forcing cancel")
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    # ------------------------------------------------------------------
    # Producer API
    # ------------------------------------------------------------------

    async def enqueue(self, event: AuditEvent, *, critical: bool = False) -> None:
        """Enqueue ``event``.

        - Queue has room → nonblocking put + return.
        - Queue full + critical → synchronous insert (PG or fallback).
        - Queue full + non-critical → drop + metric.
        """

        self.metrics["enqueued"] += 1
        try:
            self._queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass

        if critical:
            self.metrics["sync_writes"] += 1
            try:
                await self._insert_batch([event])
            except Exception:
                self.metrics["flush_errors"] += 1
                self.metrics["fallback_written"] += 1
                logger.exception("sync audit insert failed; writing to fallback")
                await self._fallback.write(event)
            return

        self.metrics["dropped"] += 1
        if self._on_drop is not None:
            res = self._on_drop(event)
            if asyncio.iscoroutine(res):
                await res

    # ------------------------------------------------------------------
    # Flush loop
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        deadline = time.monotonic() + self._flush_interval_sec
        batch: list[AuditEvent] = []
        while not self._stop_evt.is_set():
            timeout = max(0.0, deadline - time.monotonic())
            try:
                ev = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                batch.append(ev)
                if len(batch) < self._batch_size:
                    # Drain opportunistically without blocking.
                    while len(batch) < self._batch_size:
                        try:
                            batch.append(self._queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
            except TimeoutError:
                pass

            if batch and (len(batch) >= self._batch_size or time.monotonic() >= deadline):
                await self._attempt_backfill()
                await self._flush(batch)
                batch = []
                deadline = time.monotonic() + self._flush_interval_sec
            elif not batch:
                deadline = time.monotonic() + self._flush_interval_sec

        # On stop: drain whatever is left, bounded by the total timeout.
        stop_deadline = time.monotonic() + _STOP_DRAIN_TIMEOUT_SEC
        while time.monotonic() < stop_deadline:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
            if len(batch) >= self._batch_size:
                await self._flush(batch)
                batch = []
        if batch:
            await self._flush(batch)
        # Best-effort backfill on shutdown.
        try:
            await self._attempt_backfill()
        except Exception:
            logger.debug("shutdown backfill failed", exc_info=True)

    async def _flush(self, batch: list[AuditEvent]) -> None:
        try:
            await self._insert_batch(batch)
            self.metrics["written"] += len(batch)
        except Exception:
            self.metrics["flush_errors"] += 1
            logger.exception("audit flush failed; routing criticals to fallback")
            critical = [ev for ev in batch if _is_event_critical(ev)]
            if critical:
                await self._fallback.write_many(critical)
                self.metrics["fallback_written"] += len(critical)
            dropped = len(batch) - len(critical)
            if dropped:
                self.metrics["dropped"] += dropped

    async def _insert_batch(self, batch: list[AuditEvent]) -> None:
        if not batch:
            return
        rows = [_event_to_row(ev) for ev in batch]
        async with self._maker() as session:
            await session.execute(insert(AuditLog), rows)
            await session.commit()

    async def _attempt_backfill(self) -> None:
        """Flush any previously-fallback'd events into PG.

        Cheap happy path: the file doesn't exist → ``drain`` returns ``[]``.
        On PG failure we restore the file via ``write_many``.
        """

        events = await self._fallback.drain()
        if not events:
            return
        try:
            await self._insert_batch(events)
        except Exception:
            logger.exception("backfill insert failed; restoring fallback log")
            await self._fallback.write_many(events)
            raise
        self.metrics["fallback_backfilled"] += len(events)

    # ------------------------------------------------------------------
    # Introspection (used by tests)
    # ------------------------------------------------------------------

    def qsize(self) -> int:
        return self._queue.qsize()


def _is_event_critical(event: AuditEvent) -> bool:
    from app.gateway.identity.audit.events import KEY_CRITICAL_ACTIONS

    return event.action in KEY_CRITICAL_ACTIONS
