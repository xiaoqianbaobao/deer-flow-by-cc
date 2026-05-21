"""Shared fixtures for identity tests.

Integration fixtures skip gracefully if Docker/testcontainers is unavailable
(`IDENTITY_TEST_BACKEND=off`) so `make test` stays green on laptops.
"""

import inspect
import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

_BACKEND = os.environ.get("IDENTITY_TEST_BACKEND", "auto").lower()
_SKIP_REASON = "set IDENTITY_TEST_BACKEND=on (or install docker + testcontainers) to run integration tests"


def pytest_collection_modifyitems(config, items):
    """Auto-mark async test functions under tests/identity/ with @pytest.mark.asyncio.

    This gives us pytest-asyncio "auto" mode scoped to identity tests only,
    without flipping the global asyncio_mode — which would affect warning
    capture in other legacy tests.
    """
    for item in items:
        if "tests/identity/" not in str(item.fspath):
            continue
        func = getattr(item, "function", None)
        if func is not None and inspect.iscoroutinefunction(func) and not item.get_closest_marker("asyncio"):
            item.add_marker(pytest.mark.asyncio)


def _skip_if_no_docker():
    if _BACKEND == "off":
        pytest.skip(_SKIP_REASON)
    if _BACKEND == "auto":
        try:
            import docker  # noqa: F401
            import testcontainers.postgres  # noqa: F401
            import testcontainers.redis  # noqa: F401
        except Exception:
            pytest.skip(_SKIP_REASON)


@pytest.fixture(scope="session")
def pg_container() -> Iterator:
    _skip_if_no_docker()
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16-alpine", username="deerflow", password="deerflow", dbname="deerflow", driver="asyncpg")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def pg_url(pg_container) -> str:
    url = pg_container.get_connection_url()
    return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace("postgresql://", "postgresql+asyncpg://")


@pytest.fixture(scope="session")
def redis_container() -> Iterator:
    _skip_if_no_docker()
    from testcontainers.redis import RedisContainer

    container = RedisContainer("redis:7-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def redis_url(redis_container) -> str:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest_asyncio.fixture
async def async_engine(pg_url: str) -> AsyncIterator:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(async_engine) -> AsyncIterator:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker() as session:
        yield session
        await session.rollback()
