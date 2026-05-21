"""Tests for GET /api/auth/providers — lightweight read, no auth required."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def identity_app() -> FastAPI:
    """Minimal FastAPI app mounting only the identity auth router.

    No DB / Redis / OIDC runtime required for the providers endpoint because
    the route only reads ``runtime.oidc_clients`` — tests patch ``get_runtime``
    with a MagicMock.
    """
    from app.gateway.identity.routers import auth as auth_router_module

    app = FastAPI()
    app.include_router(auth_router_module.router)
    return app


@pytest.mark.asyncio
async def test_providers_returns_configured_providers(identity_app, monkeypatch):
    """When OIDC clients are configured, /api/auth/providers lists them with display metadata."""
    from app.gateway.identity.routers import auth as auth_router_module

    fake_runtime = MagicMock()
    fake_runtime.oidc_clients = {
        "okta": MagicMock(display_name="Okta", icon_url="/icons/okta.svg"),
        "azure": MagicMock(display_name="Azure AD", icon_url=None),
    }
    monkeypatch.setattr(auth_router_module, "get_runtime", lambda: fake_runtime)

    async with AsyncClient(transport=ASGITransport(app=identity_app), base_url="http://test") as client:
        resp = await client.get("/api/auth/providers")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "providers": [
            {"id": "okta", "display_name": "Okta", "icon_url": "/icons/okta.svg"},
            {"id": "azure", "display_name": "Azure AD", "icon_url": None},
        ]
    }


@pytest.mark.asyncio
async def test_providers_returns_empty_list_when_none_configured(identity_app, monkeypatch):
    from app.gateway.identity.routers import auth as auth_router_module

    fake_runtime = MagicMock()
    fake_runtime.oidc_clients = {}
    monkeypatch.setattr(auth_router_module, "get_runtime", lambda: fake_runtime)

    async with AsyncClient(transport=ASGITransport(app=identity_app), base_url="http://test") as client:
        resp = await client.get("/api/auth/providers")

    assert resp.status_code == 200
    assert resp.json() == {"providers": []}
