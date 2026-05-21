"""Tests for OIDC first-login policy + Identity factory."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from app.gateway.identity.auth.identity_factory import (
    build_identity_for_user,
    resolve_active_tenant,
    upsert_oidc_user,
)
from app.gateway.identity.auth.oidc import OIDCUserInfo
from app.gateway.identity.bootstrap import bootstrap


@pytest_asyncio.fixture
async def seeded_db(pg_url, monkeypatch):
    """Migrated + bootstrapped db, with no platform admin."""
    monkeypatch.setenv("DEERFLOW_DATABASE_URL", pg_url)
    from app.gateway.identity.settings import get_identity_settings

    get_identity_settings.cache_clear()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await bootstrap(s, bootstrap_admin_email=None)
    try:
        yield engine
    finally:
        await engine.dispose()
        await asyncio.to_thread(command.downgrade, cfg, "base")


def _info(subject: str = "s-1", email: str = "alice@example.com", provider: str = "okta") -> OIDCUserInfo:
    return OIDCUserInfo(subject=subject, provider=provider, email=email, display_name="Alice", id_token_claims={})


@pytest.mark.asyncio
async def test_upsert_creates_new_user(seeded_db):
    maker = async_sessionmaker(seeded_db, expire_on_commit=False)
    async with maker() as s:
        user = await upsert_oidc_user(s, _info())
        await s.commit()
        assert user.id > 0
        assert user.email == "alice@example.com"
        assert user.oidc_provider == "okta"
        assert user.oidc_subject == "s-1"


@pytest.mark.asyncio
async def test_upsert_matches_by_provider_subject(seeded_db):
    maker = async_sessionmaker(seeded_db, expire_on_commit=False)
    async with maker() as s:
        u1 = await upsert_oidc_user(s, _info(subject="same-sub", email="e1@x.com"))
        await s.commit()
    async with maker() as s:
        # Even if email changes, same (provider, subject) resolves to the same row.
        u2 = await upsert_oidc_user(s, _info(subject="same-sub", email="e2@x.com"))
        await s.commit()
        assert u2.id == u1.id
        assert u2.email == "e2@x.com"  # email updated


@pytest.mark.asyncio
async def test_upsert_falls_back_to_email(seeded_db):
    """Existing user with same email but no oidc binding → bind on first SSO login."""
    maker = async_sessionmaker(seeded_db, expire_on_commit=False)
    from app.gateway.identity.models.user import User

    async with maker() as s:
        s.add(User(email="preexisting@x.com", display_name="Pre", status=1))
        await s.commit()
    async with maker() as s:
        u = await upsert_oidc_user(s, _info(email="preexisting@x.com", subject="new-sub"))
        await s.commit()
        assert u.oidc_subject == "new-sub"
        assert u.oidc_provider == "okta"


@pytest.mark.asyncio
async def test_resolve_active_tenant_no_membership_returns_none(seeded_db):
    maker = async_sessionmaker(seeded_db, expire_on_commit=False)
    async with maker() as s:
        user = await upsert_oidc_user(s, _info())
        await s.commit()
        tenant, ws = await resolve_active_tenant(s, user, auto_provision=False)
        assert tenant is None
        assert ws is None


@pytest.mark.asyncio
async def test_resolve_active_tenant_auto_provision(seeded_db):
    maker = async_sessionmaker(seeded_db, expire_on_commit=False)
    async with maker() as s:
        user = await upsert_oidc_user(s, _info())
        await s.commit()
        tenant, ws = await resolve_active_tenant(s, user, auto_provision=True)
        assert tenant is not None
        assert ws is not None
        assert tenant.name.startswith("Alice") or tenant.slug.startswith("alice")


@pytest.mark.asyncio
async def test_resolve_active_tenant_existing_membership(seeded_db):
    """A user with memberships in two tenants gets the alpha-first tenant active."""
    maker = async_sessionmaker(seeded_db, expire_on_commit=False)
    from app.gateway.identity.models.tenant import Tenant, Workspace
    from app.gateway.identity.models.user import Membership

    async with maker() as s:
        user = await upsert_oidc_user(s, _info())
        tz = Tenant(slug="zebra", name="Zebra", status=1)
        ta = Tenant(slug="alpha", name="Alpha", status=1)
        s.add_all([tz, ta])
        await s.flush()
        s.add(Workspace(tenant_id=ta.id, slug="w", name="Alpha WS"))
        s.add_all(
            [
                Membership(user_id=user.id, tenant_id=tz.id),
                Membership(user_id=user.id, tenant_id=ta.id),
            ]
        )
        await s.commit()
        tenant, ws = await resolve_active_tenant(s, user)
        assert tenant.slug == "alpha"


@pytest.mark.asyncio
async def test_build_identity_flattens_permissions(seeded_db):
    """workspace_admin role → all workspace permissions in the Identity."""
    maker = async_sessionmaker(seeded_db, expire_on_commit=False)
    from sqlalchemy import select

    from app.gateway.identity.models.role import Role
    from app.gateway.identity.models.tenant import Tenant, Workspace
    from app.gateway.identity.models.user import Membership, WorkspaceMember

    async with maker() as s:
        user = await upsert_oidc_user(s, _info())
        tenant = Tenant(slug="acme", name="Acme", status=1)
        s.add(tenant)
        await s.flush()
        ws = Workspace(tenant_id=tenant.id, slug="general", name="General")
        s.add(ws)
        s.add(Membership(user_id=user.id, tenant_id=tenant.id))
        await s.flush()
        ws_admin = (await s.execute(select(Role).where(Role.role_key == "workspace_admin"))).scalar_one()
        s.add(WorkspaceMember(user_id=user.id, workspace_id=ws.id, role_id=ws_admin.id))
        await s.commit()

        ident = await build_identity_for_user(s, user, tenant, ws)
        assert ident.user_id == user.id
        assert ident.tenant_id == tenant.id
        assert ws.id in ident.workspace_ids
        # workspace_admin has all workspace scope perms (seeded in bootstrap)
        assert "thread:read" in ident.permissions
        assert "skill:manage" in ident.permissions
        assert "workflow:manage" in ident.permissions
