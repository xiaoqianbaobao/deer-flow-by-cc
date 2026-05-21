"""Tests for IdentityMiddleware.

A tiny FastAPI app with only the middleware + a /_echo route is wired up
per test; /_echo returns ``request.state.identity`` as JSON so assertions
stay concise.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx
import pytest
import pytest_asyncio
from alembic.config import Config
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from app.gateway.identity.auth.api_token import create_api_token
from app.gateway.identity.auth.jwt import AccessTokenClaims, issue_access_token
from app.gateway.identity.auth.session import SessionStore
from app.gateway.identity.middlewares.identity import IdentityMiddleware
from app.gateway.identity.models.tenant import Tenant, Workspace
from app.gateway.identity.models.user import User


@pytest.fixture(scope="module")
def rsa_pair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_pem = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv_pem, pub_pem


@pytest_asyncio.fixture
async def fresh_db(pg_url, monkeypatch):
    monkeypatch.setenv("DEERFLOW_DATABASE_URL", pg_url)
    from app.gateway.identity.settings import get_identity_settings

    get_identity_settings.cache_clear()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url)
    try:
        yield engine
    finally:
        await engine.dispose()
        await asyncio.to_thread(command.downgrade, cfg, "base")


@pytest_asyncio.fixture
async def session_maker(fresh_db):
    return async_sessionmaker(fresh_db, expire_on_commit=False)


@pytest_asyncio.fixture
async def redis_client(redis_url):
    import redis.asyncio as aioredis

    c = aioredis.from_url(redis_url, decode_responses=True)
    yield c
    await c.aclose()


@pytest_asyncio.fixture
async def session_store(redis_client):
    prefix = f"test-{uuid.uuid4().hex[:8]}"
    store = SessionStore(redis_client, refresh_ttl_sec=3600, key_prefix=prefix)
    yield store
    async for k in redis_client.scan_iter(f"{prefix}:*"):
        await redis_client.delete(k)


def _build_app(public_key_pem: str, session_store: SessionStore, session_maker) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        IdentityMiddleware,
        public_key_pem=public_key_pem,
        session_store=session_store,
        session_maker=session_maker,
        issuer="deerflow",
        audience="deerflow-api",
        cookie_name="deerflow_session",
    )

    @app.get("/_echo")
    async def echo(request: Request):
        ident = request.state.identity
        return {
            "token_type": ident.token_type,
            "user_id": ident.user_id,
            "tenant_id": ident.tenant_id,
            "session_id": ident.session_id,
            "permissions": sorted(ident.permissions),
        }

    return app


# --- Helper ---


def _async_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


# --- Cases ---


@pytest.mark.asyncio
async def test_no_auth_is_anonymous(rsa_pair, session_store, session_maker):
    _, pub = rsa_pair
    async with _async_client(_build_app(pub, session_store, session_maker)) as c:
        r = await c.get("/_echo")
    assert r.status_code == 200
    assert r.json()["token_type"] == "anonymous"
    assert r.json()["user_id"] is None


@pytest.mark.asyncio
async def test_malformed_bearer_is_anonymous(rsa_pair, session_store, session_maker):
    _, pub = rsa_pair
    async with _async_client(_build_app(pub, session_store, session_maker)) as c:
        r = await c.get("/_echo", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 200
    assert r.json()["token_type"] == "anonymous"


@pytest.mark.asyncio
async def test_valid_jwt_with_session(rsa_pair, session_store, session_maker):
    priv, pub = rsa_pair
    # Seed a session in Redis.
    rec = await session_store.create(user_id=42, tenant_id=1, refresh_token="rt", ip=None, ua=None)
    now = int(time.time())
    claims = AccessTokenClaims(
        sub="42",
        email="u@x.com",
        tid=1,
        wids=[1],
        permissions=["workspace.read"],
        roles={},
        sid=rec.sid,
        iat=now,
        exp=now + 900,
        iss="deerflow",
        aud="deerflow-api",
    )
    token = issue_access_token(claims, private_key_pem=priv)
    async with _async_client(_build_app(pub, session_store, session_maker)) as c:
        r = await c.get("/_echo", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "jwt"
    assert body["user_id"] == 42
    assert body["tenant_id"] == 1
    assert body["session_id"] == rec.sid
    assert "workspace.read" in body["permissions"]


@pytest.mark.asyncio
async def test_valid_jwt_but_session_revoked(rsa_pair, session_store, session_maker):
    priv, pub = rsa_pair
    rec = await session_store.create(user_id=1, tenant_id=1, refresh_token="rt", ip=None, ua=None)
    await session_store.revoke(rec.sid)
    now = int(time.time())
    claims = AccessTokenClaims(
        sub="1",
        email="u@x.com",
        tid=1,
        wids=[],
        permissions=[],
        roles={},
        sid=rec.sid,
        iat=now,
        exp=now + 900,
        iss="deerflow",
        aud="deerflow-api",
    )
    token = issue_access_token(claims, private_key_pem=priv)
    async with _async_client(_build_app(pub, session_store, session_maker)) as c:
        r = await c.get("/_echo", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["token_type"] == "anonymous"


@pytest.mark.asyncio
async def test_expired_jwt_is_anonymous(rsa_pair, session_store, session_maker):
    priv, pub = rsa_pair
    rec = await session_store.create(user_id=1, tenant_id=1, refresh_token="rt", ip=None, ua=None)
    now = int(time.time())
    claims = AccessTokenClaims(
        sub="1",
        email="u@x.com",
        tid=1,
        wids=[],
        permissions=[],
        roles={},
        sid=rec.sid,
        iat=now - 2000,
        exp=now - 1000,
        iss="deerflow",
        aud="deerflow-api",
    )
    token = issue_access_token(claims, private_key_pem=priv)
    async with _async_client(_build_app(pub, session_store, session_maker)) as c:
        r = await c.get("/_echo", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200  # M2 never 401s; M3 decorator handles that
    assert r.json()["token_type"] == "anonymous"


@pytest.mark.asyncio
async def test_valid_api_token(rsa_pair, session_store, session_maker):
    _, pub = rsa_pair
    async with session_maker() as s:
        tenant = Tenant(slug="t", name="T", status=1)
        s.add(tenant)
        await s.flush()
        ws = Workspace(tenant_id=tenant.id, slug="w", name="W")
        u = User(email="u@x.com", display_name="U", status=1)
        s.add_all([ws, u])
        await s.commit()
        token_info = await create_api_token(
            s,
            user_id=u.id,
            tenant_id=tenant.id,
            workspace_id=ws.id,
            name="t",
            scopes=["thread:read"],
            expires_at=None,
            created_by=u.id,
        )

    async with _async_client(_build_app(pub, session_store, session_maker)) as c:
        r = await c.get("/_echo", headers={"Authorization": f"Bearer {token_info.plaintext}"})
    body = r.json()
    assert body["token_type"] == "api_token"
    assert "thread:read" in body["permissions"]


@pytest.mark.asyncio
async def test_cookie_auth_equivalent_to_bearer(rsa_pair, session_store, session_maker):
    priv, pub = rsa_pair
    rec = await session_store.create(user_id=99, tenant_id=1, refresh_token="rt", ip=None, ua=None)
    now = int(time.time())
    claims = AccessTokenClaims(
        sub="99",
        email="u@x.com",
        tid=1,
        wids=[],
        permissions=[],
        roles={},
        sid=rec.sid,
        iat=now,
        exp=now + 900,
        iss="deerflow",
        aud="deerflow-api",
    )
    token = issue_access_token(claims, private_key_pem=priv)
    async with _async_client(_build_app(pub, session_store, session_maker)) as c:
        c.cookies.set("deerflow_session", token)
        r = await c.get("/_echo")
    assert r.json()["token_type"] == "jwt"
    assert r.json()["user_id"] == 99
