"""DB-level immutability: audit_logs cannot be UPDATE/DELETEd by the app role.

The testcontainers ``deerflow`` user is a SUPERUSER in default installs,
which bypasses GRANT. We therefore create a non-superuser role
``deerflow_app_test`` inside the test, re-run the migration so it picks
up the new role, and assert the expected behaviour against that role.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.gateway.identity.models.audit import AuditLog

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def restricted_app_engine(pg_url, monkeypatch):
    monkeypatch.setenv("DEERFLOW_DATABASE_URL", pg_url)
    from app.gateway.identity.settings import get_identity_settings

    get_identity_settings.cache_clear()

    # Bring schema up to current head (superuser).
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        # Create the restricted role if missing and reset grants.
        await conn.execute(
            text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'deerflow_app_test') THEN
                        CREATE ROLE deerflow_app_test WITH LOGIN PASSWORD 'app_test' NOSUPERUSER NOCREATEDB NOCREATEROLE;
                    END IF;
                END $$;
                """
            )
        )
        # Baseline: nothing allowed.
        await conn.execute(text("REVOKE ALL ON identity.audit_logs FROM deerflow_app_test"))
        await conn.execute(text("GRANT USAGE ON SCHEMA identity TO deerflow_app_test"))
        # Apply INSERT + SELECT exactly like the migration's intended shape.
        await conn.execute(text("GRANT INSERT, SELECT ON identity.audit_logs TO deerflow_app_test"))
        await conn.execute(text("GRANT USAGE ON SEQUENCE identity.audit_logs_id_seq TO deerflow_app_test"))
        await conn.execute(text("TRUNCATE TABLE identity.audit_logs RESTART IDENTITY"))

    try:
        yield engine
    finally:
        await engine.dispose()


def _app_url(pg_url: str) -> str:
    # Swap credentials into the connection URL.
    from urllib.parse import urlparse, urlunparse

    u = urlparse(pg_url)
    netloc = f"deerflow_app_test:app_test@{u.hostname}:{u.port}"
    return urlunparse(u._replace(netloc=netloc))


async def test_app_role_can_insert_and_select(restricted_app_engine, pg_url):
    app_engine = create_async_engine(_app_url(pg_url))
    try:
        async with app_engine.begin() as conn:
            await conn.execute(
                insert(AuditLog),
                [
                    {
                        "tenant_id": 1,
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
                    }
                ],
            )
        async with app_engine.connect() as conn:
            count = await conn.execute(text("SELECT COUNT(*) FROM identity.audit_logs"))
            assert count.scalar_one() == 1
    finally:
        await app_engine.dispose()


async def test_app_role_cannot_delete(restricted_app_engine, pg_url):
    app_engine = create_async_engine(_app_url(pg_url))
    try:
        async with app_engine.begin() as conn:
            await conn.execute(
                insert(AuditLog),
                [
                    {
                        "tenant_id": 1,
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
                    }
                ],
            )
        with pytest.raises(Exception) as ei:
            async with app_engine.begin() as conn:
                await conn.execute(text("DELETE FROM identity.audit_logs WHERE tenant_id = 1"))
        msg = str(ei.value).lower()
        assert "permission" in msg or "denied" in msg or "insufficient" in msg
    finally:
        await app_engine.dispose()


async def test_app_role_cannot_update(restricted_app_engine, pg_url):
    app_engine = create_async_engine(_app_url(pg_url))
    try:
        async with app_engine.begin() as conn:
            await conn.execute(
                insert(AuditLog),
                [
                    {
                        "tenant_id": 1,
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
                    }
                ],
            )
        with pytest.raises(Exception) as ei:
            async with app_engine.begin() as conn:
                await conn.execute(text("UPDATE identity.audit_logs SET action = 'tampered' WHERE tenant_id = 1"))
        msg = str(ei.value).lower()
        assert "permission" in msg or "denied" in msg or "insufficient" in msg
    finally:
        await app_engine.dispose()
