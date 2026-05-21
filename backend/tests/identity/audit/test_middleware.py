"""AuditMiddleware: which requests get audited and with what metadata."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.gateway.identity.audit.events import AuditEvent
from app.gateway.identity.audit.middleware import AuditMiddleware

# pytestmark is intentionally not set here; these are synchronous TestClient
# tests. Conftest auto-marks async ones elsewhere.


@dataclass
class _FakeIdentity:
    user_id: int | None = 7
    tenant_id: int | None = 42
    workspace_ids: tuple = ()


class _FakeWriter:
    def __init__(self) -> None:
        self.events: list[tuple[AuditEvent, bool]] = []

    async def enqueue(self, event: AuditEvent, *, critical: bool = False) -> None:
        self.events.append((event, critical))


class _InjectIdentityMiddleware(BaseHTTPMiddleware):
    """Pretend to be IdentityMiddleware — populates ``state.identity``."""

    def __init__(self, app, identity) -> None:
        super().__init__(app)
        self._identity = identity

    async def dispatch(self, request: Request, call_next):
        request.state.identity = self._identity
        return await call_next(request)


def _build_app(writer, *, identity=None) -> FastAPI:
    app = FastAPI()

    @app.get("/api/me")
    async def me():
        return {"ok": True}

    @app.get("/api/audit/list")
    async def audit_list():
        return {"items": []}

    @app.get("/api/tenants/{tid}/workspaces/{wid}")
    async def ws(tid: int, wid: int):
        return {"tid": tid, "wid": wid}

    @app.post("/api/threads/{id}")
    async def make_thread(id: str):
        return {"id": id}

    @app.get("/api/threads/{id}")
    async def get_thread(id: str):
        if id == "forbidden":
            raise HTTPException(status_code=403, detail="no")
        return {"id": id}

    @app.post("/api/broken")
    async def broken():
        raise HTTPException(status_code=500, detail="boom")

    @app.get("/health")
    async def health():
        return {"ok": True}

    # Outer: AuditMiddleware. Inner: identity stub. add_middleware stacks
    # outermost-last, so we register audit after identity.
    app.add_middleware(_InjectIdentityMiddleware, identity=identity or _FakeIdentity())
    app.add_middleware(AuditMiddleware, writer=writer)
    return app


def _last_event(writer: _FakeWriter) -> AuditEvent:
    assert writer.events, "no events recorded"
    return writer.events[-1][0]


def test_post_request_is_audited():
    writer = _FakeWriter()
    app = _build_app(writer)
    with TestClient(app) as c:
        r = c.post("/api/threads/abc")
    assert r.status_code == 200
    assert len(writer.events) == 1
    ev = _last_event(writer)
    assert ev.action == "http.post"
    assert ev.result == "success"
    assert ev.resource_type == "thread"
    assert ev.resource_id == "abc"
    assert ev.user_id == 7
    assert ev.tenant_id == 42
    assert ev.duration_ms is not None and ev.duration_ms >= 0


def test_get_me_not_audited():
    writer = _FakeWriter()
    app = _build_app(writer)
    with TestClient(app) as c:
        c.get("/api/me")
    assert writer.events == []


def test_audit_read_prefix_is_audited():
    writer = _FakeWriter()
    app = _build_app(writer)
    with TestClient(app) as c:
        c.get("/api/audit/list")
    assert len(writer.events) == 1


def test_500_response_is_failure_with_error_code():
    writer = _FakeWriter()
    app = _build_app(writer)
    with TestClient(app) as c:
        r = c.post("/api/broken")
    assert r.status_code == 500
    ev = _last_event(writer)
    assert ev.result == "failure"
    assert ev.error_code == "HTTP_500"


def test_403_read_captured_as_authz_denied():
    writer = _FakeWriter()
    app = _build_app(writer)
    with TestClient(app) as c:
        r = c.get("/api/threads/forbidden")
    assert r.status_code == 403
    ev = _last_event(writer)
    assert ev.action == "authz.api.denied"
    assert ev.result == "failure"


def test_workspace_path_extracts_resource_id():
    writer = _FakeWriter()
    app = _build_app(writer)
    with TestClient(app) as c:
        c.get("/api/tenants/1/workspaces/9")
    ev = _last_event(writer)
    assert ev.resource_type == "workspace"
    assert ev.resource_id == "9"
    assert ev.workspace_id == 9


def test_writes_are_marked_critical():
    writer = _FakeWriter()
    app = _build_app(writer)
    with TestClient(app) as c:
        c.post("/api/threads/abc")
    _, critical = writer.events[-1]
    assert critical is True


def test_reads_on_audit_prefix_not_critical():
    writer = _FakeWriter()
    app = _build_app(writer)
    with TestClient(app) as c:
        c.get("/api/audit/list")
    _, critical = writer.events[-1]
    assert critical is False


def test_identity_is_read_from_downstream_state():
    """Confirms the ordering: inner identity middleware sets state before
    outer AuditMiddleware reads it on the way out."""
    writer = _FakeWriter()
    custom = _FakeIdentity(user_id=123, tenant_id=456)
    app = _build_app(writer, identity=custom)
    with TestClient(app) as c:
        c.post("/api/threads/abc")
    ev = _last_event(writer)
    assert ev.user_id == 123
    assert ev.tenant_id == 456
