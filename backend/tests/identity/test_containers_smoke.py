"""Verify pg/redis fixtures bootstrap cleanly."""

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_pg_container_reachable(async_engine):
    async with async_engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


def test_redis_container_reachable(redis_url):
    import redis

    r = redis.Redis.from_url(redis_url, decode_responses=True)
    assert r.ping() is True
