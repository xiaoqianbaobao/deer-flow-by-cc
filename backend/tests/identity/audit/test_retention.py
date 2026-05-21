"""Retention job: archive old rows to gzip, delete from PG."""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.gateway.identity.audit.events import AuditEvent
from app.gateway.identity.audit.retention import run_retention_job
from app.gateway.identity.models.audit import AuditLog

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def retention_engine(pg_url, monkeypatch):
    monkeypatch.setenv("DEERFLOW_DATABASE_URL", pg_url)
    from app.gateway.identity.settings import get_identity_settings

    get_identity_settings.cache_clear()

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(text("TRUNCATE TABLE identity.audit_logs RESTART IDENTITY"))
    try:
        yield engine
    finally:
        await engine.dispose()


def _old_row(created_at: datetime, tenant_id: int = 1) -> dict:
    return {
        "tenant_id": tenant_id,
        "user_id": 1,
        "workspace_id": 1,
        "action": "thread.created",
        "resource_type": "thread",
        "resource_id": "x",
        "ip": "127.0.0.1",
        "user_agent": "pytest",
        "result": "success",
        "error_code": None,
        "duration_ms": 10,
        "metadata": {},
        "created_at": created_at,
    }


class _CapturingWriter:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def enqueue(self, event: AuditEvent, *, critical: bool = False) -> None:
        self.events.append(event)


async def test_old_rows_archived_and_deleted(retention_engine, tmp_path):
    maker = async_sessionmaker(retention_engine, expire_on_commit=False)
    # 100 rows backdated 120 days.
    very_old = datetime.now(UTC) - timedelta(days=120)
    recent = datetime.now(UTC) - timedelta(days=5)
    async with maker() as s:
        await s.execute(insert(AuditLog), [_old_row(very_old) for _ in range(100)])
        await s.execute(insert(AuditLog), [_old_row(recent) for _ in range(3)])
        await s.commit()

    archive_dir = tmp_path / "archive"
    writer = _CapturingWriter()
    summary = await run_retention_job(
        maker,
        retention_days=90,
        archive_dir=archive_dir,
        writer=writer,
    )

    # Rows deleted from PG (only recent ones remain).
    async with maker() as s:
        remaining = (await s.execute(select(AuditLog))).scalars().all()
    assert len(remaining) == 3
    assert summary["total_archived"] == 100

    # Archive file exists with 100 entries.
    year_month = f"{very_old.year:04d}-{very_old.month:02d}"
    archive_path: Path = archive_dir / "1" / f"{year_month}.jsonl.gz"
    assert archive_path.exists()
    with gzip.open(archive_path, "rt", encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    assert len(lines) == 100
    assert all(row["action"] == "thread.created" for row in lines)

    # Retention summary event emitted.
    assert any(ev.action == "system.retention.archived" for ev in writer.events)


async def test_no_old_rows_is_noop(retention_engine, tmp_path):
    maker = async_sessionmaker(retention_engine, expire_on_commit=False)
    async with maker() as s:
        await s.execute(insert(AuditLog), [_old_row(datetime.now(UTC))])
        await s.commit()

    summary = await run_retention_job(
        maker,
        retention_days=90,
        archive_dir=tmp_path / "archive",
    )
    assert summary["total_archived"] == 0
    async with maker() as s:
        remaining = (await s.execute(select(AuditLog))).scalars().all()
    assert len(remaining) == 1


async def test_partitions_by_tenant_and_month(retention_engine, tmp_path):
    maker = async_sessionmaker(retention_engine, expire_on_commit=False)
    very_old = datetime(2025, 12, 1, tzinfo=UTC)
    older = datetime(2025, 11, 15, tzinfo=UTC)
    async with maker() as s:
        await s.execute(
            insert(AuditLog),
            [
                _old_row(very_old, tenant_id=1),
                _old_row(older, tenant_id=1),
                _old_row(very_old, tenant_id=2),
            ],
        )
        await s.commit()

    archive_dir = tmp_path / "archive"
    await run_retention_job(maker, retention_days=1, archive_dir=archive_dir)

    assert (archive_dir / "1" / "2025-12.jsonl.gz").exists()
    assert (archive_dir / "1" / "2025-11.jsonl.gz").exists()
    assert (archive_dir / "2" / "2025-12.jsonl.gz").exists()
