"""API-token permission cache helpers (Task 8, spec §6.5).

Uses the shared Redis testcontainer fixture. The cache lives under
``identity:perms:{user_id}:{tenant_id|platform}`` with a 300s TTL.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from app.gateway.identity.rbac.permission_cache import (
    DEFAULT_TTL_SEC,
    PermissionCache,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def cache(redis_url):
    import redis.asyncio as aioredis

    prefix = f"test-{uuid.uuid4().hex[:8]}"
    client = aioredis.from_url(redis_url, decode_responses=True)
    yield PermissionCache(client, key_prefix=prefix)
    async for key in client.scan_iter(f"{prefix}:*"):
        await client.delete(key)
    await client.aclose()


class TestRoundTrip:
    async def test_cold_miss(self, cache):
        assert await cache.get(1, 1) is None

    async def test_set_then_get(self, cache):
        await cache.set(1, 1, {"thread:read", "skill:invoke"})
        assert await cache.get(1, 1) == {"thread:read", "skill:invoke"}

    async def test_platform_scope_uses_platform_bucket(self, cache):
        await cache.set(2, None, {"tenant:create"})
        assert await cache.get(2, None) == {"tenant:create"}
        # The tenant-scoped bucket is a different key.
        assert await cache.get(2, 1) is None

    async def test_distinct_tenants_distinct_buckets(self, cache):
        await cache.set(3, 1, {"thread:read"})
        await cache.set(3, 2, {"tenant:read"})
        assert await cache.get(3, 1) == {"thread:read"}
        assert await cache.get(3, 2) == {"tenant:read"}


class TestInvalidation:
    async def test_invalidate_single_tenant(self, cache):
        await cache.set(4, 1, {"a"})
        await cache.set(4, 2, {"b"})
        await cache.invalidate(4, tenant_id=1)
        assert await cache.get(4, 1) is None
        assert await cache.get(4, 2) == {"b"}

    async def test_invalidate_all_tenants_for_user(self, cache):
        await cache.set(5, 1, {"a"})
        await cache.set(5, 2, {"b"})
        await cache.set(5, None, {"platform:read"})
        await cache.invalidate(5)
        assert await cache.get(5, 1) is None
        assert await cache.get(5, 2) is None
        assert await cache.get(5, None) is None


class TestStaleFlag:
    async def test_mark_and_check_and_clear(self, cache):
        assert await cache.is_stale(6) is False
        await cache.mark_stale(6)
        assert await cache.is_stale(6) is True
        await cache.clear_stale(6)
        assert await cache.is_stale(6) is False

    async def test_multiple_stale_users(self, cache):
        await cache.mark_stale(7)
        await cache.mark_stale(8)
        assert await cache.is_stale(7) is True
        assert await cache.is_stale(8) is True


class TestTTL:
    async def test_ttl_applied(self, cache):
        await cache.set(9, 1, {"x"})
        # Verify TTL on the underlying key is close to the default.
        key = cache._perm_key(9, 1)
        ttl = await cache._redis.ttl(key)
        assert DEFAULT_TTL_SEC - 5 <= ttl <= DEFAULT_TTL_SEC

    async def test_custom_ttl_override(self, cache):
        await cache.set(10, 1, {"x"}, ttl_sec=60)
        ttl = await cache._redis.ttl(cache._perm_key(10, 1))
        assert 55 <= ttl <= 60
