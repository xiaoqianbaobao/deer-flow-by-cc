"""Tests for db engine/session factory and context vars."""

import pytest
from sqlalchemy import text

from app.gateway.identity.context import current_identity, current_tenant_id
from app.gateway.identity.db import create_engine_and_sessionmaker


@pytest.mark.asyncio
async def test_engine_sessionmaker_roundtrip(pg_url):
    engine, maker = create_engine_and_sessionmaker(pg_url)
    try:
        async with maker() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1
    finally:
        await engine.dispose()


def test_context_vars_default_none():
    assert current_tenant_id.get() is None
    assert current_identity.get() is None


def test_context_vars_scoped():
    token = current_tenant_id.set(42)
    try:
        assert current_tenant_id.get() == 42
    finally:
        current_tenant_id.reset(token)
    assert current_tenant_id.get() is None
