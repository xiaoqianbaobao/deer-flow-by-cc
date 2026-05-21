"""@requires decorator behavior (Task 2)."""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.gateway.identity.auth.identity import Identity
from app.gateway.identity.rbac.decorator import requires


def _build_app(dependency) -> FastAPI:
    app = FastAPI()

    @app.get("/platform", dependencies=[Depends(dependency("admin:tenants:create", "platform"))])
    async def platform_route():
        return {"ok": True}

    @app.get("/tenants/{tid}", dependencies=[Depends(dependency("tenant:read", "tenant"))])
    async def tenant_route(tid: int):
        return {"tid": tid}

    @app.get("/workspaces/{wid}", dependencies=[Depends(dependency("thread:read", "workspace"))])
    async def workspace_route(wid: int):
        return {"wid": wid}

    # scope=tenant but no path param — allows platform-level lists
    @app.get("/admin/tenants", dependencies=[Depends(dependency("admin:tenants:list", "tenant"))])
    async def admin_tenants_route():
        return {"ok": True}

    return app


def _set_identity(client: TestClient, identity: Identity) -> None:
    """Hook IdentityMiddleware-style state injection for tests.

    TestClient doesn't run full ASGI middleware, so we monkey-patch
    request.state via a custom middleware installed on the app.
    """


def _client_with(identity: Identity | None) -> TestClient:
    app = _build_app(requires)

    @app.middleware("http")
    async def inject_identity(request, call_next):
        request.state.identity = identity if identity is not None else Identity.anonymous()
        return await call_next(request)

    return TestClient(app)


def _ident(
    *,
    user_id=1,
    tenant_id=1,
    workspace_ids=(1,),
    permissions=(),
    platform_roles=(),
    token_type="jwt",
) -> Identity:
    return Identity(
        token_type=token_type,
        user_id=user_id,
        email="u@example.com",
        tenant_id=tenant_id,
        workspace_ids=tuple(workspace_ids),
        permissions=frozenset(permissions),
        roles={"platform": list(platform_roles), "tenant": [], "workspaces": {}},
        session_id="sess",
    )


class TestAnonymous:
    def test_anonymous_returns_401(self):
        c = _client_with(Identity.anonymous())
        r = c.get("/tenants/1")
        assert r.status_code == 401
        assert r.json()["detail"]["error_code"] == "UNAUTHENTICATED"


class TestPermissionCheck:
    def test_authenticated_without_perm_403(self):
        c = _client_with(_ident(permissions=set()))
        r = c.get("/tenants/1")
        assert r.status_code == 403
        assert r.json()["detail"]["error_code"] == "PERMISSION_DENIED"
        assert r.json()["detail"]["missing"] == "tenant:read"

    def test_authenticated_with_perm_and_tenant_match(self):
        c = _client_with(_ident(permissions={"tenant:read"}, tenant_id=1))
        r = c.get("/tenants/1")
        assert r.status_code == 200
        assert r.json() == {"tid": 1}


class TestHorizontalScope:
    def test_tenant_mismatch_is_403(self):
        c = _client_with(_ident(permissions={"tenant:read"}, tenant_id=1))
        r = c.get("/tenants/99")
        assert r.status_code == 403
        assert r.json()["detail"]["error_code"] == "PERMISSION_DENIED"

    def test_workspace_mismatch_is_403(self):
        c = _client_with(_ident(permissions={"thread:read"}, workspace_ids=(1,)))
        r = c.get("/workspaces/99")
        assert r.status_code == 403

    def test_workspace_match(self):
        c = _client_with(_ident(permissions={"thread:read"}, workspace_ids=(1, 2)))
        r = c.get("/workspaces/2")
        assert r.status_code == 200


class TestPlatformAdmin:
    def test_platform_admin_passes_regardless_of_tenant_id(self):
        c = _client_with(_ident(tenant_id=1, platform_roles=("platform_admin",)))
        r = c.get("/tenants/99")
        assert r.status_code == 200

    def test_platform_admin_workspace_bypass(self):
        c = _client_with(_ident(workspace_ids=(), platform_roles=("platform_admin",)))
        r = c.get("/workspaces/42")
        assert r.status_code == 200

    def test_platform_admin_explicit_platform_route(self):
        c = _client_with(_ident(platform_roles=("platform_admin",)))
        r = c.get("/platform")
        assert r.status_code == 200


class TestScopeTenantMissingParam:
    """scope=tenant but route has no {tid} → permission check only, no
    horizontal check (e.g. /api/admin/tenants list endpoint).
    """

    def test_with_permission(self):
        c = _client_with(_ident(permissions={"admin:tenants:list"}))
        r = c.get("/admin/tenants")
        assert r.status_code == 200

    def test_without_permission(self):
        c = _client_with(_ident(permissions=set()))
        r = c.get("/admin/tenants")
        assert r.status_code == 403


class TestAuditQueue:
    """Verify that denials fire the audit hook (M6 will actually write)."""

    def test_denial_queues_audit_event(self, monkeypatch):
        from app.gateway.identity.rbac import decorator as decorator_mod

        events = []

        def fake_queue(identity, tag, scope, request, *, horizontal=False):
            events.append({"tag": tag, "scope": scope, "horizontal": horizontal})

        monkeypatch.setattr(decorator_mod, "_queue_denied", fake_queue)

        c = _client_with(_ident(permissions=set()))
        r = c.get("/tenants/1")
        assert r.status_code == 403
        assert events == [{"tag": "tenant:read", "scope": "tenant", "horizontal": False}]

    def test_horizontal_denial_flagged(self, monkeypatch):
        from app.gateway.identity.rbac import decorator as decorator_mod

        events = []

        def fake_queue(identity, tag, scope, request, *, horizontal=False):
            events.append({"tag": tag, "scope": scope, "horizontal": horizontal})

        monkeypatch.setattr(decorator_mod, "_queue_denied", fake_queue)

        c = _client_with(_ident(permissions={"tenant:read"}, tenant_id=1))
        r = c.get("/tenants/99")
        assert r.status_code == 403
        assert events == [{"tag": "tenant:read", "scope": "tenant", "horizontal": True}]
