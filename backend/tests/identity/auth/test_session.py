"""Tests for Redis-backed SessionStore."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from app.gateway.identity.auth.session import SessionStore


@pytest_asyncio.fixture
async def session_store(redis_url):
    import redis.asyncio as aioredis

    # Give each test an isolated namespace by using a fresh prefix.
    prefix = f"test-{uuid.uuid4().hex[:8]}"
    client = aioredis.from_url(redis_url, decode_responses=True)
    store = SessionStore(client, refresh_ttl_sec=3600, key_prefix=prefix)
    yield store
    # Clean up all keys with our prefix
    async for key in client.scan_iter(f"{prefix}:*"):
        await client.delete(key)
    await client.aclose()


@pytest.mark.asyncio
async def test_create_and_get(session_store):
    rec = await session_store.create(
        user_id=1,
        tenant_id=10,
        refresh_token="rt-abc",
        ip="127.0.0.1",
        ua="pytest",
    )
    assert rec.user_id == 1
    assert rec.tenant_id == 10
    assert rec.ip == "127.0.0.1"
    assert rec.ua == "pytest"
    assert rec.revoked is False

    back = await session_store.get(rec.sid)
    assert back is not None
    assert back.sid == rec.sid
    assert back.user_id == 1
    assert back.tenant_id == 10


@pytest.mark.asyncio
async def test_get_nonexistent_returns_none(session_store):
    assert await session_store.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_verify_refresh_correct_and_wrong(session_store):
    rec = await session_store.create(user_id=2, tenant_id=None, refresh_token="rt-secret", ip=None, ua=None)
    assert await session_store.verify_refresh(rec.sid, "rt-secret") is True
    assert await session_store.verify_refresh(rec.sid, "wrong") is False
    assert await session_store.verify_refresh("no-such-sid", "rt-secret") is False


@pytest.mark.asyncio
async def test_revoke_marks_revoked(session_store):
    rec = await session_store.create(user_id=3, tenant_id=None, refresh_token="rt", ip=None, ua=None)
    await session_store.revoke(rec.sid)
    back = await session_store.get(rec.sid)
    # Either gone or marked revoked — store returns None for revoked sessions.
    assert back is None or back.revoked is True
    # After revoke, verify_refresh returns False regardless of secret.
    assert await session_store.verify_refresh(rec.sid, "rt") is False


@pytest.mark.asyncio
async def test_revoke_all_for_user(session_store):
    r1 = await session_store.create(user_id=4, tenant_id=None, refresh_token="a", ip=None, ua=None)
    r2 = await session_store.create(user_id=4, tenant_id=None, refresh_token="b", ip=None, ua=None)
    r3 = await session_store.create(user_id=5, tenant_id=None, refresh_token="c", ip=None, ua=None)

    count = await session_store.revoke_all_for_user(4)
    assert count == 2

    assert await session_store.get(r1.sid) is None
    assert await session_store.get(r2.sid) is None
    # Other user untouched.
    assert (await session_store.get(r3.sid)) is not None


@pytest.mark.asyncio
async def test_list_for_user(session_store):
    r1 = await session_store.create(user_id=6, tenant_id=None, refresh_token="a", ip=None, ua=None)
    r2 = await session_store.create(user_id=6, tenant_id=None, refresh_token="b", ip=None, ua=None)
    await session_store.create(user_id=7, tenant_id=None, refresh_token="c", ip=None, ua=None)

    records = await session_store.list_for_user(6)
    sids = {r.sid for r in records}
    assert sids == {r1.sid, r2.sid}


@pytest.mark.asyncio
async def test_update_tenant(session_store):
    rec = await session_store.create(user_id=8, tenant_id=1, refresh_token="rt", ip=None, ua=None)
    await session_store.update_tenant(rec.sid, 2)
    back = await session_store.get(rec.sid)
    assert back is not None
    assert back.tenant_id == 2
