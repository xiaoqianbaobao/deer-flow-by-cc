"""End-to-end OIDC flow tests against the mock IdP."""

from __future__ import annotations

import asyncio
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from app.gateway.identity.auth.config import OIDCProviderConfig
from app.gateway.identity.auth.oidc import (
    NonceMismatchError,
    OIDCClient,
    StateExpiredError,
    StateMismatchError,
)


@pytest_asyncio.fixture
async def redis_client(redis_url):
    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def provider_config(mock_idp):
    return OIDCProviderConfig(
        name="mock",
        issuer=mock_idp.base_url,
        client_id="test-client",
        client_secret="test-secret",
        scopes=["openid", "profile", "email"],
    )


@pytest_asyncio.fixture
async def client(provider_config, redis_client):
    prefix = f"test-{uuid.uuid4().hex[:8]}"
    c = OIDCClient(provider_config, redis_client=redis_client, state_ttl_sec=5, key_prefix=prefix)
    yield c
    async for k in redis_client.scan_iter(f"{prefix}:*"):
        await redis_client.delete(k)


@pytest.mark.asyncio
async def test_login_redirect_builds_authorize_url(client, mock_idp):
    url = await client.login_redirect(redirect_uri="http://localhost/cb", next_url="/dashboard")
    parsed = urlparse(url)
    assert parsed.netloc.endswith(mock_idp.base_url.removeprefix("http://"))
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["test-client"]
    assert qs["redirect_uri"] == ["http://localhost/cb"]
    assert qs["response_type"] == ["code"]
    assert "openid" in qs["scope"][0]
    assert qs["state"][0]  # non-empty
    assert qs["code_challenge"][0]
    assert qs["code_challenge_method"] == ["S256"]


@pytest.mark.asyncio
async def test_state_stored_with_ttl(client, redis_client):
    url = await client.login_redirect(redirect_uri="http://localhost/cb", next_url=None)
    parsed = urlparse(url)
    state = parse_qs(parsed.query)["state"][0]
    ttl = await redis_client.ttl(client._state_key(state))
    assert 0 < ttl <= 5


@pytest.mark.asyncio
async def test_callback_happy_path(client, mock_idp):
    # Pin user on the session-scoped mock IdP (other tests may mutate it).
    mock_idp.idp.set_user(email="user@mock.com", subject="mock-sub-123")

    # Drive the login-redirect → IdP /authorize → extract code
    url = await client.login_redirect(redirect_uri="http://localhost/cb", next_url=None)
    state = parse_qs(urlparse(url).query)["state"][0]

    # Simulate browser visiting IdP /authorize — it 302s back with code.
    import httpx

    async with httpx.AsyncClient() as http:
        r = await http.get(url, follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]

    info = await client.handle_callback(code=code, state=state, redirect_uri="http://localhost/cb")
    assert info.subject == "mock-sub-123"
    assert info.email == "user@mock.com"
    assert info.provider == "mock"


@pytest.mark.asyncio
async def test_callback_state_mismatch(client, mock_idp):
    await client.login_redirect(redirect_uri="http://localhost/cb", next_url=None)
    with pytest.raises(StateMismatchError):
        await client.handle_callback(code="any", state="bogus", redirect_uri="http://localhost/cb")


@pytest.mark.asyncio
async def test_callback_state_expired(provider_config, redis_client, mock_idp):
    import httpx

    prefix = f"test-{uuid.uuid4().hex[:8]}"
    c = OIDCClient(provider_config, redis_client=redis_client, state_ttl_sec=1, key_prefix=prefix)
    url = await c.login_redirect(redirect_uri="http://localhost/cb", next_url=None)
    state = parse_qs(urlparse(url).query)["state"][0]
    async with httpx.AsyncClient() as http:
        r = await http.get(url, follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    await asyncio.sleep(1.2)
    with pytest.raises(StateExpiredError):
        await c.handle_callback(code=code, state=state, redirect_uri="http://localhost/cb")
    async for k in redis_client.scan_iter(f"{prefix}:*"):
        await redis_client.delete(k)


@pytest.mark.asyncio
async def test_callback_nonce_mismatch(client, mock_idp, redis_client):
    """Simulate a tampered nonce: stash wrong nonce in state cache then call."""
    url = await client.login_redirect(redirect_uri="http://localhost/cb", next_url=None)
    state = parse_qs(urlparse(url).query)["state"][0]
    # Overwrite the stored nonce so the id_token's nonce will no longer match.
    import json

    raw = await redis_client.get(client._state_key(state))
    data = json.loads(raw)
    data["nonce"] = "different-nonce-value"
    await redis_client.set(client._state_key(state), json.dumps(data))

    import httpx

    async with httpx.AsyncClient() as http:
        r = await http.get(url, follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    with pytest.raises(NonceMismatchError):
        await client.handle_callback(code=code, state=state, redirect_uri="http://localhost/cb")
