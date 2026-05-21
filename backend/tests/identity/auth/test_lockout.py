"""Tests for Redis-backed login lockout."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio

from app.gateway.identity.auth.lockout import LoginLockout


@pytest_asyncio.fixture
async def redis_client(redis_url):
    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def lockout(redis_client):
    prefix = f"test-{uuid.uuid4().hex[:8]}"
    lo = LoginLockout(redis_client, max_attempts=3, window_sec=2, block_sec=2, key_prefix=prefix)
    yield lo
    async for k in redis_client.scan_iter(f"{prefix}:*"):
        await redis_client.delete(k)


@pytest.mark.asyncio
async def test_single_failure_not_blocked(lockout):
    triggered = await lockout.record_failure(ip="1.1.1.1", email="u@x.com")
    assert triggered is False
    assert await lockout.is_blocked(ip="1.1.1.1", email="u@x.com") is False


@pytest.mark.asyncio
async def test_max_attempts_triggers_block(lockout):
    for _ in range(2):
        assert (await lockout.record_failure(ip="2.2.2.2", email="a@x.com")) is False
    triggered = await lockout.record_failure(ip="2.2.2.2", email="a@x.com")
    assert triggered is True
    assert await lockout.is_blocked(ip="2.2.2.2", email="a@x.com") is True


@pytest.mark.asyncio
async def test_block_expires(lockout):
    for _ in range(3):
        await lockout.record_failure(ip="3.3.3.3", email="b@x.com")
    assert await lockout.is_blocked(ip="3.3.3.3", email="b@x.com") is True
    await asyncio.sleep(2.1)
    assert await lockout.is_blocked(ip="3.3.3.3", email="b@x.com") is False


@pytest.mark.asyncio
async def test_clear_resets_counters(lockout):
    await lockout.record_failure(ip="4.4.4.4", email="c@x.com")
    await lockout.record_failure(ip="4.4.4.4", email="c@x.com")
    await lockout.clear(ip="4.4.4.4", email="c@x.com")
    triggered = await lockout.record_failure(ip="4.4.4.4", email="c@x.com")
    assert triggered is False  # counter was reset, this is now the first


@pytest.mark.asyncio
async def test_different_keys_independent(lockout):
    for _ in range(3):
        await lockout.record_failure(ip="5.5.5.5", email="d@x.com")
    assert await lockout.is_blocked(ip="5.5.5.5", email="d@x.com") is True
    # Different email on same IP — independent counter.
    assert await lockout.is_blocked(ip="5.5.5.5", email="e@x.com") is False
    # Different IP on same email — independent counter.
    assert await lockout.is_blocked(ip="6.6.6.6", email="d@x.com") is False


@pytest.mark.asyncio
async def test_window_expires_resets_counter(redis_client):
    prefix = f"test-{uuid.uuid4().hex[:8]}"
    short = LoginLockout(redis_client, max_attempts=3, window_sec=1, block_sec=2, key_prefix=prefix)
    try:
        await short.record_failure(ip="7.7.7.7", email="f@x.com")
        await short.record_failure(ip="7.7.7.7", email="f@x.com")
        await asyncio.sleep(1.2)
        # Counter expired — next failure is "first" again.
        triggered = await short.record_failure(ip="7.7.7.7", email="f@x.com")
        assert triggered is False
        assert await short.is_blocked(ip="7.7.7.7", email="f@x.com") is False
    finally:
        async for k in redis_client.scan_iter(f"{prefix}:*"):
            await redis_client.delete(k)


@pytest.mark.asyncio
async def test_email_only_key(lockout):
    # ip=None supported: used for OIDC path where caller may key on email only.
    for _ in range(3):
        await lockout.record_failure(ip=None, email="g@x.com")
    assert await lockout.is_blocked(ip=None, email="g@x.com") is True
