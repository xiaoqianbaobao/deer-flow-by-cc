"""Async engine + session factory used by the identity subsystem.

A single engine is created at gateway startup when `ENABLE_IDENTITY=true`
and disposed on shutdown. Milestones after M1 register a
`Depends(get_session)` for routers.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_engine_and_sessionmaker(database_url: str, *, pool_size: int = 10, max_overflow: int = 5) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, maker


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def set_global_engine(engine: AsyncEngine, maker: async_sessionmaker[AsyncSession]) -> None:
    global _engine, _sessionmaker
    _engine = engine
    _sessionmaker = maker


def clear_global_engine() -> None:
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency; raises if identity subsystem was not initialised.

    Reserved for M2+; M1 just validates the scaffold.
    """
    if _sessionmaker is None:
        raise RuntimeError("Identity subsystem not initialised (ENABLE_IDENTITY=false?)")
    async with _sessionmaker() as session:
        try:
            yield session
        finally:
            await session.close()
