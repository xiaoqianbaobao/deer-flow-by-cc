"""End-to-end producer wiring (RBAC denies, internal/audit, middleware).

These tests assemble a minimal FastAPI app with the audit writer wired
in and exercise the producers without hitting Postgres. Real DB writes
are covered by ``test_writer.py`` and ``test_audit_api.py``.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.gateway.identity.audit.events import AuditEvent
from app.gateway.identity.audit.middleware import AuditMiddleware
from app.gateway.identity.auth.identity import Identity
from app.gateway.identity.rbac.decorator import requires
from app.gateway.identity.routers import internal as internal_router
from app.gateway.identity.routers.internal import sign_internal_payload


class _CapturingWriter:
    def __init__(self) -> None:
        self.events: list[tuple[AuditEvent, bool]] = []

    async def enqueue(self, event: AuditEvent, *, critical: bool = False) -> None:
        self.events.append((event, critical))


class _StubIdentityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, identity: Identity) -> None:
        super().__init__(app)
        self._identity = identity

    async def dispatch(self, request: Request, call_next):
        request.state.identity = self._identity
        return await call_next(request)


def _make_authenticated_identity(
    *,
    user_id: int = 1,
    tenant_id: int | None = 1,
    permissions: frozenset = frozenset(),
) -> Identity:
    return Identity(
        token_type="jwt",
        user_id=user_id,
        email="x@example.com",
        tenant_id=tenant_id,
        workspace_ids=(1,),
        permissions=permissions,
        roles={},
        session_id="sess1",
    )


def _build_app(writer, identity: Identity) -> FastAPI:
    app = FastAPI()
    app.state.audit_writer = writer

    @app.get(
        "/api/tenants/{tid}/protected",
        dependencies=[Depends(requires("audit:read", "tenant"))],
    )
    async def protected(tid: int):
        return {"ok": True, "tid": tid}

    app.include_router(internal_router.router)

    app.add_middleware(_StubIdentityMiddleware, identity=identity)
    app.add_middleware(AuditMiddleware, writer=writer)
    return app


def test_rbac_deny_emits_authz_api_denied(monkeypatch):
    writer = _CapturingWriter()
    # Identity without the `audit:read` permission → 403.
    ident = _make_authenticated_identity(permissions=frozenset())
    app = _build_app(writer, ident)

    with TestClient(app) as c:
        r = c.get("/api/tenants/1/protected")
    assert r.status_code == 403

    # Two events expected:
    # 1. RBAC's _queue_denied scheduled task → "authz.api.denied" critical
    # 2. AuditMiddleware HTTP outer-layer → "authz.api.denied" critical
    actions = [(ev.action, crit) for ev, crit in writer.events]
    assert ("authz.api.denied", True) in actions


def test_internal_audit_endpoint_forwards_to_writer(monkeypatch):
    monkeypatch.setenv("DEERFLOW_INTERNAL_SIGNING_KEY", "test-key")
    from app.gateway.identity.settings import get_identity_settings

    get_identity_settings.cache_clear()

    writer = _CapturingWriter()
    ident = _make_authenticated_identity()
    app = _build_app(writer, ident)

    body = b'{"action":"authz.tool.denied","tenant_id":1,"user_id":2,"workspace_id":3,"thread_id":"t1","resource":"bash","outcome":"failure","extra":{"missing":"tool:bash"}}'
    sig, ts = sign_internal_payload(body, key="test-key")

    with TestClient(app) as c:
        r = c.post(
            "/internal/audit",
            content=body,
            headers={
                "X-Deerflow-Internal-Sig": sig,
                "X-Deerflow-Internal-Ts": ts,
                "Content-Type": "application/json",
            },
        )
    assert r.status_code == 200, r.text

    # The internal endpoint should have enqueued an AuditEvent on our writer.
    actions = [ev.action for ev, _ in writer.events]
    assert "authz.tool.denied" in actions
    ev = next(e for e, _ in writer.events if e.action == "authz.tool.denied")
    assert ev.tenant_id == 1
    assert ev.user_id == 2
    assert ev.workspace_id == 3
    assert ev.metadata.get("thread_id") == "t1"
    assert ev.metadata.get("resource") == "bash"


def test_successful_request_emits_http_event_with_identity():
    writer = _CapturingWriter()
    ident = _make_authenticated_identity(permissions=frozenset({"audit:read"}))
    app = _build_app(writer, ident)

    with TestClient(app) as c:
        r = c.get("/api/tenants/1/protected")
    assert r.status_code == 200

    # AuditMiddleware audits authorized reads on /api/tenants/* (in
    # AUDITED_READ_PREFIXES). Confirm event has identity attached.
    assert any(ev.user_id == 1 and ev.tenant_id == 1 for ev, _ in writer.events)
