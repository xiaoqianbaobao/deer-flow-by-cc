"""Tests for API token module (dft_* format, bcrypt-hashed)."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from app.gateway.identity.auth.api_token import (
    CreatedToken,
    create_api_token,
    revoke_api_token,
    verify_api_token,
)
from app.gateway.identity.models.tenant import Tenant, Workspace
from app.gateway.identity.models.user import User


@pytest_asyncio.fixture
async def fresh_db(pg_url, monkeypatch):
    monkeypatch.setenv("DEERFLOW_DATABASE_URL", pg_url)
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.gateway.identity.settings import get_identity_settings

    get_identity_settings.cache_clear()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    engine = create_async_engine(pg_url)
    try:
        yield engine
    finally:
        await engine.dispose()
        await asyncio.to_thread(command.downgrade, cfg, "base")
        get_identity_settings.cache_clear()


@pytest_asyncio.fixture
async def seeded_session(fresh_db):
    maker = async_sessionmaker(fresh_db, expire_on_commit=False)
    async with maker() as s:
        tenant = Tenant(slug="t1", name="T1", status=1)
        s.add(tenant)
        await s.flush()
        ws = Workspace(tenant_id=tenant.id, slug="w1", name="W1")
        s.add(ws)
        u = User(email="u@x.com", display_name="U", status=1)
        s.add(u)
        await s.commit()
        yield s, u, tenant, ws


@pytest.mark.asyncio
async def test_create_returns_plaintext_and_prefix(seeded_session):
    session, user, tenant, ws = seeded_session
    created = await create_api_token(
        session,
        user_id=user.id,
        tenant_id=tenant.id,
        workspace_id=ws.id,
        name="test token",
        scopes=["workspace.read"],
        expires_at=None,
        created_by=user.id,
    )
    assert isinstance(created, CreatedToken)
    # Format: dft_<6 char prefix>_<32 char secret>
    assert re.match(r"^dft_[A-Z2-7]{6}_[A-Z2-7]{32}$", created.plaintext), created.plaintext
    assert len(created.prefix) == 6
    assert created.token_id > 0


@pytest.mark.asyncio
async def test_plaintext_not_stored(seeded_session):
    session, user, tenant, ws = seeded_session
    created = await create_api_token(
        session,
        user_id=user.id,
        tenant_id=tenant.id,
        workspace_id=None,
        name="t",
        scopes=[],
        expires_at=None,
        created_by=user.id,
    )
    from sqlalchemy import select

    from app.gateway.identity.models.token import ApiToken

    row = (await session.execute(select(ApiToken).where(ApiToken.id == created.token_id))).scalar_one()
    assert row.prefix == created.prefix
    # The hash must NOT equal plaintext; bcrypt hashes start with $2
    assert row.token_hash != created.plaintext
    assert row.token_hash.startswith("$2")


@pytest.mark.asyncio
async def test_verify_with_correct_plaintext(seeded_session):
    session, user, tenant, ws = seeded_session
    created = await create_api_token(
        session,
        user_id=user.id,
        tenant_id=tenant.id,
        workspace_id=ws.id,
        name="t",
        scopes=["workspace.read", "workspace.write"],
        expires_at=None,
        created_by=user.id,
    )
    ident = await verify_api_token(session, created.plaintext)
    assert ident is not None
    assert ident.user_id == user.id
    assert ident.tenant_id == tenant.id
    assert ident.token_type == "api_token"
    assert ident.permissions == frozenset({"workspace.read", "workspace.write"})


@pytest.mark.asyncio
async def test_verify_wrong_secret_returns_none(seeded_session):
    session, user, tenant, ws = seeded_session
    created = await create_api_token(
        session,
        user_id=user.id,
        tenant_id=tenant.id,
        workspace_id=None,
        name="t",
        scopes=[],
        expires_at=None,
        created_by=user.id,
    )
    # Same prefix, wrong secret.
    bad = f"dft_{created.prefix}_{'A' * 32}"
    assert (await verify_api_token(session, bad)) is None


@pytest.mark.asyncio
async def test_verify_expired_returns_none(seeded_session):
    session, user, tenant, ws = seeded_session
    past = datetime.now(UTC) - timedelta(hours=1)
    created = await create_api_token(
        session,
        user_id=user.id,
        tenant_id=tenant.id,
        workspace_id=None,
        name="t",
        scopes=[],
        expires_at=past,
        created_by=user.id,
    )
    assert (await verify_api_token(session, created.plaintext)) is None


@pytest.mark.asyncio
async def test_verify_revoked_returns_none(seeded_session):
    session, user, tenant, ws = seeded_session
    created = await create_api_token(
        session,
        user_id=user.id,
        tenant_id=tenant.id,
        workspace_id=None,
        name="t",
        scopes=[],
        expires_at=None,
        created_by=user.id,
    )
    await revoke_api_token(session, token_id=created.token_id, by_user_id=user.id)
    assert (await verify_api_token(session, created.plaintext)) is None


@pytest.mark.asyncio
async def test_verify_malformed_returns_none(seeded_session):
    session, *_ = seeded_session
    assert (await verify_api_token(session, "not a token")) is None
    assert (await verify_api_token(session, "dft_missing")) is None
    assert (await verify_api_token(session, "Bearer abc")) is None


@pytest.mark.asyncio
async def test_verify_prefix_collision_picks_matching_hash(seeded_session, monkeypatch):
    """Two tokens with the same prefix: lookup returns both, verify finds the
    one whose hash matches the presented plaintext."""
    session, user, tenant, ws = seeded_session

    # Force identical prefixes for two tokens by stubbing the prefix generator.
    import app.gateway.identity.auth.api_token as mod

    real_gen = mod._generate_token
    calls = 0

    def fixed_prefix(*, bcrypt_cost=12):
        nonlocal calls
        calls += 1
        # Always same prefix, different secret.
        prefix = "AAAAAA"
        secret = ("B" * 32) if calls == 1 else ("C" * 32)
        plaintext = f"dft_{prefix}_{secret}"
        from passlib.hash import bcrypt as _bcrypt

        hashed = _bcrypt.using(rounds=4).hash(plaintext)
        return plaintext, prefix, hashed

    monkeypatch.setattr(mod, "_generate_token", fixed_prefix)

    t1 = await create_api_token(
        session,
        user_id=user.id,
        tenant_id=tenant.id,
        workspace_id=None,
        name="one",
        scopes=["a"],
        expires_at=None,
        created_by=user.id,
    )
    t2 = await create_api_token(
        session,
        user_id=user.id,
        tenant_id=tenant.id,
        workspace_id=None,
        name="two",
        scopes=["b"],
        expires_at=None,
        created_by=user.id,
    )
    assert t1.prefix == t2.prefix
    monkeypatch.setattr(mod, "_generate_token", real_gen)

    i1 = await verify_api_token(session, t1.plaintext)
    i2 = await verify_api_token(session, t2.plaintext)
    assert i1 is not None and "a" in i1.permissions
    assert i2 is not None and "b" in i2.permissions


@pytest.mark.asyncio
async def test_verify_updates_last_used(seeded_session):
    session, user, tenant, ws = seeded_session
    created = await create_api_token(
        session,
        user_id=user.id,
        tenant_id=tenant.id,
        workspace_id=None,
        name="t",
        scopes=[],
        expires_at=None,
        created_by=user.id,
    )
    from sqlalchemy import select

    from app.gateway.identity.models.token import ApiToken

    before = (await session.execute(select(ApiToken).where(ApiToken.id == created.token_id))).scalar_one().last_used_at
    assert before is None
    await verify_api_token(session, created.plaintext, client_ip="127.0.0.1")
    await session.commit()
    after = (await session.execute(select(ApiToken).where(ApiToken.id == created.token_id))).scalar_one().last_used_at
    assert after is not None
