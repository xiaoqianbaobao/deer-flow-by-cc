"""GET /api/roles and GET /api/permissions (Task 4).

Integration tests run against the real Postgres container with the
bootstrap seed so the returned shape matches production.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.gateway.identity.auth.identity import Identity
from app.gateway.identity.bootstrap import PREDEFINED_PERMISSIONS, PREDEFINED_ROLES, bootstrap
from app.gateway.identity.db import get_session
from app.gateway.identity.models.base import Base

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def seeded_app(async_engine):
    from app.gateway.identity.routers import roles as roles_router_mod

    async with async_engine.begin() as conn:
        await conn.execute(__import__("sqlalchemy").text("CREATE SCHEMA IF NOT EXISTS identity"))
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker() as s:
        await bootstrap(s, bootstrap_admin_email=None)

    app = FastAPI()
    app.include_router(roles_router_mod.router)

    async def _override_get_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override_get_session

    identity_holder: dict = {"identity": Identity.anonymous()}

    @app.middleware("http")
    async def inject_identity(request, call_next):
        request.state.identity = identity_holder["identity"]
        return await call_next(request)

    yield app, maker, identity_holder

    # Clean up schema so next test starts fresh
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _authed() -> Identity:
    return Identity(
        token_type="jwt",
        user_id=1,
        email="u@example.com",
        tenant_id=1,
        workspace_ids=(1,),
        permissions=frozenset({"role:read"}),
        roles={"platform": [], "tenant": [], "workspaces": {}},
        session_id="s",
    )


async def test_list_roles_returns_all_seed_roles(seeded_app):
    app, _, holder = seeded_app
    holder["identity"] = _authed()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/roles")
    assert r.status_code == 200
    body = r.json()
    assert "roles" in body
    role_keys = [row["role_key"] for row in body["roles"]]
    seeded_keys = [key for key, _, _ in PREDEFINED_ROLES]
    assert sorted(role_keys) == sorted(seeded_keys)
    # Shape contract
    first = body["roles"][0]
    assert {"role_key", "scope", "display_name", "description"}.issubset(first.keys())


async def test_list_permissions_returns_all_seed_permissions(seeded_app):
    app, _, holder = seeded_app
    holder["identity"] = _authed()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/permissions")
    assert r.status_code == 200
    body = r.json()
    assert "permissions" in body
    tags = [row["tag"] for row in body["permissions"]]
    assert sorted(tags) == sorted(tag for tag, _ in PREDEFINED_PERMISSIONS)
    first = body["permissions"][0]
    assert {"tag", "scope"}.issubset(first.keys())


async def test_anonymous_gets_401_on_roles(seeded_app):
    app, _, _ = seeded_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/roles")
    assert r.status_code == 401


async def test_anonymous_gets_401_on_permissions(seeded_app):
    app, _, _ = seeded_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/permissions")
    assert r.status_code == 401
