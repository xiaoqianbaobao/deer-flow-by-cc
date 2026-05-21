"""SQLAlchemy tenant-scope auto-filter middleware (Task 3).

Uses an in-memory SQLite database with test-only `TenantScoped` /
`WorkspaceScoped` models so we don't depend on M4 thread tables.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import BigInteger, ForeignKey, MetaData, String, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.gateway.identity.auth.identity import Identity
from app.gateway.identity.context import (
    current_identity,
    with_platform_privilege,
)
from app.gateway.identity.middlewares.tenant_scope import install_auto_filter
from app.gateway.identity.rbac.errors import PermissionDeniedError

pytestmark = pytest.mark.asyncio

# --- test models -------------------------------------------------------


class _TestBase(DeclarativeBase):
    metadata = MetaData()


class _TenantScoped:
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)


class _WorkspaceScoped(_TenantScoped):
    workspace_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)


class Thread(_TenantScoped, _TestBase):
    __tablename__ = "threads"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)


class Skill(_WorkspaceScoped, _TestBase):
    __tablename__ = "skills"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class ThreadNote(_TenantScoped, _TestBase):
    __tablename__ = "thread_notes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    thread_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("threads.id"), nullable=False)
    body: Mapped[str] = mapped_column(String(200), nullable=False)


# --- helpers -----------------------------------------------------------


def _ident(
    *,
    user_id=1,
    tenant_id=1,
    workspace_ids=(1,),
    permissions=(),
    platform_roles=(),
) -> Identity:
    return Identity(
        token_type="jwt",
        user_id=user_id,
        email="u@example.com",
        tenant_id=tenant_id,
        workspace_ids=tuple(workspace_ids),
        permissions=frozenset(permissions),
        roles={"platform": list(platform_roles), "tenant": [], "workspaces": {}},
        session_id="sess",
    )


@pytest_asyncio.fixture
async def engine_and_maker(monkeypatch):
    # Point the middleware at our test mixins, not the production ones.
    from app.gateway.identity.middlewares import tenant_scope as tenant_scope_mod

    monkeypatch.setattr(tenant_scope_mod, "TenantScoped", _TenantScoped)
    monkeypatch.setattr(tenant_scope_mod, "WorkspaceScoped", _WorkspaceScoped)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(_TestBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    install_auto_filter(maker)
    try:
        yield engine, maker
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded(engine_and_maker):
    _, maker = engine_and_maker
    # Seed without identity so the filter doesn't kick in.
    async with maker() as s:
        s.add_all(
            [
                Thread(id=1, tenant_id=1, title="a1"),
                Thread(id=2, tenant_id=1, title="a2"),
                Thread(id=3, tenant_id=2, title="b1"),
                Skill(id=1, tenant_id=1, workspace_id=10, name="sk-a-1"),
                Skill(id=2, tenant_id=1, workspace_id=11, name="sk-a-2"),
                Skill(id=3, tenant_id=2, workspace_id=20, name="sk-b"),
                ThreadNote(id=1, tenant_id=1, thread_id=1, body="note-a"),
                ThreadNote(id=2, tenant_id=2, thread_id=3, body="note-b"),
            ]
        )
        await s.commit()
    return maker


# --- tests -------------------------------------------------------------


class TestTenantFilter:
    async def test_query_scoped_to_tenant_a(self, seeded):
        maker = seeded
        token = current_identity.set(_ident(tenant_id=1))
        try:
            async with maker() as s:
                rows = (await s.execute(select(Thread))).scalars().all()
            assert [r.id for r in rows] == [1, 2]
        finally:
            current_identity.reset(token)

    async def test_query_scoped_to_tenant_b(self, seeded):
        maker = seeded
        token = current_identity.set(_ident(tenant_id=2))
        try:
            async with maker() as s:
                rows = (await s.execute(select(Thread))).scalars().all()
            assert [r.id for r in rows] == [3]
        finally:
            current_identity.reset(token)

    async def test_no_identity_no_filter(self, seeded):
        maker = seeded
        async with maker() as s:
            rows = (await s.execute(select(Thread))).scalars().all()
        assert len(rows) == 3


class TestPlatformAdmin:
    async def test_admin_sees_all(self, seeded):
        maker = seeded
        admin = _ident(tenant_id=None, platform_roles=("platform_admin",))
        token = current_identity.set(admin)
        try:
            async with maker() as s:
                rows = (await s.execute(select(Thread))).scalars().all()
            assert len(rows) == 3
        finally:
            current_identity.reset(token)


class TestWithPlatformPrivilege:
    async def test_context_manager_bypass(self, seeded):
        maker = seeded
        token = current_identity.set(_ident(tenant_id=1))
        try:
            # Normally scoped to tenant 1
            async with maker() as s:
                assert len((await s.execute(select(Thread))).scalars().all()) == 2
            # With platform privilege: sees everything
            with with_platform_privilege():
                async with maker() as s:
                    assert len((await s.execute(select(Thread))).scalars().all()) == 3
            # After exit: scoped again
            async with maker() as s:
                assert len((await s.execute(select(Thread))).scalars().all()) == 2
        finally:
            current_identity.reset(token)


class TestWorkspaceFilter:
    async def test_workspace_ids_filter(self, seeded):
        maker = seeded
        token = current_identity.set(_ident(tenant_id=1, workspace_ids=(10,)))
        try:
            async with maker() as s:
                rows = (await s.execute(select(Skill))).scalars().all()
            assert [r.id for r in rows] == [1]
        finally:
            current_identity.reset(token)

    async def test_workspace_empty_no_results(self, seeded):
        maker = seeded
        token = current_identity.set(_ident(tenant_id=1, workspace_ids=()))
        try:
            async with maker() as s:
                rows = (await s.execute(select(Skill))).scalars().all()
            assert rows == []
        finally:
            current_identity.reset(token)


class TestInsertGuard:
    async def test_cross_tenant_insert_rejected(self, seeded):
        maker = seeded
        token = current_identity.set(_ident(tenant_id=1))
        try:
            async with maker() as s:
                s.add(Thread(id=99, tenant_id=2, title="sneaky"))
                with pytest.raises(PermissionDeniedError):
                    await s.flush()
        finally:
            current_identity.reset(token)

    async def test_matching_tenant_insert_allowed(self, seeded):
        maker = seeded
        token = current_identity.set(_ident(tenant_id=1))
        try:
            async with maker() as s:
                s.add(Thread(id=100, tenant_id=1, title="ok"))
                await s.flush()
                await s.commit()
        finally:
            current_identity.reset(token)

    async def test_cross_workspace_insert_rejected(self, seeded):
        maker = seeded
        token = current_identity.set(_ident(tenant_id=1, workspace_ids=(10,)))
        try:
            async with maker() as s:
                s.add(Skill(id=99, tenant_id=1, workspace_id=99, name="sneaky"))
                with pytest.raises(PermissionDeniedError):
                    await s.flush()
        finally:
            current_identity.reset(token)

    async def test_platform_admin_can_insert_any_tenant(self, seeded):
        maker = seeded
        admin = _ident(tenant_id=1, platform_roles=("platform_admin",))
        token = current_identity.set(admin)
        try:
            async with maker() as s:
                s.add(Thread(id=101, tenant_id=2, title="cross"))
                await s.flush()
                await s.commit()
        finally:
            current_identity.reset(token)


class TestJoinAcrossTables:
    async def test_join_applies_filter_to_both(self, seeded):
        maker = seeded
        token = current_identity.set(_ident(tenant_id=1))
        try:
            async with maker() as s:
                stmt = select(Thread, ThreadNote).join(ThreadNote, ThreadNote.thread_id == Thread.id)
                rows = (await s.execute(stmt)).all()
            # Only tenant 1 rows on both sides
            assert len(rows) == 1
            thread, note = rows[0]
            assert thread.tenant_id == 1 and note.tenant_id == 1
        finally:
            current_identity.reset(token)
