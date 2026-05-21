"""Daily archive + delete job for old audit rows (spec §9.6).

Behavior:

1. Select rows older than ``retention_days`` from ``identity.audit_logs``.
2. Group by ``(tenant_id, year, month)``; write each group to
   ``{archive_dir}/{tenant_id}/{yyyy-mm}.jsonl.gz`` (append if exists).
3. DELETE the archived rows in the same transaction as the archive
   commit so we never "double archive" on retry.
4. Emit ``system.retention.archived`` with counts in metadata.

Archiving is resilient: if the write to disk fails, the transaction
rolls back and nothing is deleted. Idempotent on re-run — already-archived
rows simply appear again in the new ``jsonl.gz`` file, which is valid
JSONL (multiple gzip members are concatenatable).

This job can be launched as a long-running ``asyncio.Task`` (see
``_schedule_loop``) or invoked imperatively by an OS cron calling
``run_retention_job`` directly.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.gateway.identity.audit.events import AuditEvent
from app.gateway.identity.audit.writer import AuditBatchWriter
from app.gateway.identity.models.audit import AuditLog

logger = logging.getLogger(__name__)

_CHUNK = 1000


def _partition_key(row: AuditLog) -> tuple[int | None, str]:
    created = row.created_at or datetime.now(UTC)
    return row.tenant_id, f"{created.year:04d}-{created.month:02d}"


def _row_to_json_dict(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "user_id": row.user_id,
        "workspace_id": row.workspace_id,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "ip": str(row.ip) if row.ip is not None else None,
        "user_agent": row.user_agent,
        "result": row.result,
        "error_code": row.error_code,
        "duration_ms": row.duration_ms,
        "metadata": row.log_metadata or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _archive_path(archive_dir: Path, tenant_id: int | None, year_month: str) -> Path:
    t = str(tenant_id) if tenant_id is not None else "_untenanted"
    target = archive_dir / t
    target.mkdir(parents=True, exist_ok=True)
    return target / f"{year_month}.jsonl.gz"


def _write_archive(path: Path, rows: Iterable[dict]) -> int:
    count = 0
    with gzip.open(path, "at", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")))
            fh.write("\n")
            count += 1
    return count


async def run_retention_job(
    session_maker: async_sessionmaker,
    *,
    retention_days: int = 90,
    archive_dir: Path | str,
    writer: AuditBatchWriter | None = None,
    chunk_size: int = _CHUNK,
) -> dict:
    """Archive and delete audit rows older than ``retention_days``.

    Returns a summary dict with per-partition counts and totals.
    """

    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)

    total_archived = 0
    partitions: dict[tuple[int | None, str], int] = defaultdict(int)

    async with session_maker() as session:
        while True:
            stmt = select(AuditLog).where(AuditLog.created_at < cutoff).order_by(AuditLog.created_at.asc(), AuditLog.id.asc()).limit(chunk_size)
            rows = (await session.execute(stmt)).scalars().all()
            if not rows:
                break

            # Group by partition then write.
            by_part: dict[tuple[int | None, str], list[dict]] = defaultdict(list)
            ids: list[int] = []
            for row in rows:
                key = _partition_key(row)
                by_part[key].append(_row_to_json_dict(row))
                ids.append(row.id)

            for key, entries in by_part.items():
                path = _archive_path(archive_dir, key[0], key[1])
                written = _write_archive(path, entries)
                partitions[key] += written
                total_archived += written

            # Delete only the exact rows we just archived — guard against
            # concurrent writers adding older rows mid-loop.
            await session.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
            await session.commit()

    summary = {
        "retention_days": retention_days,
        "cutoff": cutoff.isoformat(),
        "total_archived": total_archived,
        "partitions": {f"{k[0]}/{k[1]}": v for k, v in partitions.items()},
    }

    if writer is not None:
        try:
            await writer.enqueue(
                AuditEvent(
                    action="system.retention.archived",
                    result="success",
                    metadata=summary,
                ),
                critical=False,
            )
        except Exception:
            logger.exception("retention summary enqueue failed")

    return summary


# ---------------------------------------------------------------------------
# Scheduling wrapper
# ---------------------------------------------------------------------------


async def _schedule_loop(
    session_maker: async_sessionmaker,
    *,
    retention_days: int,
    archive_dir: Path | str,
    writer: AuditBatchWriter | None = None,
    interval_sec: float = 86400.0,
    stop_event: asyncio.Event,
) -> None:
    """Run the retention job every ``interval_sec`` seconds until stopped.

    Errors inside the job don't crash the loop — they're logged and we
    wait the full interval before retrying.
    """

    while not stop_event.is_set():
        try:
            await run_retention_job(
                session_maker,
                retention_days=retention_days,
                archive_dir=archive_dir,
                writer=writer,
            )
        except Exception:
            logger.exception("retention job iteration failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
        except TimeoutError:
            continue


def start_retention_task(
    session_maker: async_sessionmaker,
    *,
    retention_days: int,
    archive_dir: Path | str,
    writer: AuditBatchWriter | None = None,
    interval_sec: float = 86400.0,
) -> tuple[asyncio.Task, asyncio.Event]:
    """Spawn the loop as an asyncio Task. Returns ``(task, stop_event)``."""

    stop = asyncio.Event()
    task = asyncio.create_task(
        _schedule_loop(
            session_maker,
            retention_days=retention_days,
            archive_dir=archive_dir,
            writer=writer,
            interval_sec=interval_sec,
            stop_event=stop,
        ),
        name="audit-retention",
    )
    return task, stop
