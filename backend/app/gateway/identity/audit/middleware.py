"""Gateway audit middleware (spec §9.1).

The middleware sits **outside** :class:`IdentityMiddleware` (registered
last with ``add_middleware`` so it wraps IdentityMiddleware) which means:

- We see the request before identity resolution → start timer.
- After ``call_next``, ``request.state.identity`` is populated by the
  inner middleware → build the event with the resolved caller.
- Response is returned unmodified — audit never blocks traffic.

Routes excluded by default:

- GET /api/me  (every UI tick hits this)
- GET /health, /docs, /openapi.json  (internal)

Routes explicitly audited on reads:

- /api/auth/*            (OIDC callbacks + logout)
- /api/audit/*           (viewing audit logs is itself auditable)
- /api/admin/*           (platform-admin work)
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.gateway.identity.audit.events import (
    AuditEvent,
    is_critical_action,
)
from app.gateway.identity.audit.redact import redact_metadata
from app.gateway.identity.audit.writer import AuditBatchWriter

logger = logging.getLogger(__name__)

# Paths that we audit on reads. Prefix match.
AUDITED_READ_PREFIXES: tuple[str, ...] = (
    "/api/auth/",
    "/api/audit",
    "/api/admin/",
    "/api/tenants/",  # cross-tenant reads we want trace of
)

# Paths that we never audit (noise).
SKIP_PREFIXES: tuple[str, ...] = (
    "/api/me",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/langgraph",  # handled downstream; also extreme volume
    "/internal/",  # HMAC-authed backplane
)


# Per-path resource extraction — ordered (first match wins).
_RESOURCE_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"^/api/tenants/(?P<id>\d+)/workspaces/(?P<wid>\d+)"), "workspace", "wid"),
    (re.compile(r"^/api/tenants/(?P<id>\d+)"), "tenant", "id"),
    (re.compile(r"^/api/threads/(?P<id>[^/]+)"), "thread", "id"),
    (re.compile(r"^/api/skills/(?P<id>[^/]+)"), "skill", "id"),
    (re.compile(r"^/api/agents/(?P<id>[^/]+)"), "agent", "id"),
    (re.compile(r"^/api/roles/(?P<id>[^/]+)"), "role", "id"),
    (re.compile(r"^/api/tokens/(?P<id>[^/]+)"), "api_token", "id"),
    (re.compile(r"^/api/auth/(?P<id>[^/?]+)"), "auth", "id"),
]


def _extract_resource(path: str) -> tuple[str | None, str | None]:
    for rx, rtype, group in _RESOURCE_PATTERNS:
        m = rx.match(path)
        if m is not None:
            return rtype, m.group(group)
    return None, None


def _derive_action(request: Request, response: Response, *, resource_type: str | None) -> str:
    """Map (method, path) → canonical action string.

    Auth routes get action-specific names; everything else falls back to
    ``http.<method>`` which keeps the metadata free-form but searchable.
    """

    path = request.url.path
    method = request.method.upper()

    # Auth flow produces semantic actions.
    if path.startswith("/api/auth/logout"):
        return "user.logout"
    if path.startswith("/api/auth/refresh"):
        return "session.refreshed"
    if "/api/auth/oidc/" in path and "/callback" in path:
        return "user.login.success" if _is_ok(response) else "user.login.failure"
    if "/api/auth/oidc/" in path and "/login" in path:
        # The redirect to the IdP is a step in the flow but not an event
        # we want to log every render of — fall through.
        pass

    # Denied responses (401/403) keep a specific tag so they're searchable
    # even without the RBAC queue hook firing.
    if response.status_code in (401, 403):
        if resource_type == "tool":
            return "authz.tool.denied"
        return "authz.api.denied"

    return f"http.{method.lower()}"


def _is_ok(response: Response) -> bool:
    return 200 <= response.status_code < 400


def _result(response: Response) -> str:
    return "success" if _is_ok(response) else "failure"


def _error_code(response: Response) -> str | None:
    if _is_ok(response):
        return None
    # Prefer a header the route can set; otherwise use "HTTP_<status>".
    header = response.headers.get("x-error-code")
    if header:
        return header[:64]
    return f"HTTP_{response.status_code}"


class AuditMiddleware(BaseHTTPMiddleware):
    """Emit an :class:`AuditEvent` for each interesting HTTP request."""

    def __init__(
        self,
        app,
        *,
        writer: AuditBatchWriter,
        skip_prefixes: Sequence[str] = SKIP_PREFIXES,
        audit_read_prefixes: Sequence[str] = AUDITED_READ_PREFIXES,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        super().__init__(app)
        self._writer = writer
        self._skip = tuple(skip_prefixes)
        self._audit_reads = tuple(audit_read_prefixes)
        self._clock = clock

    async def dispatch(self, request: Request, call_next):
        t0 = self._clock()
        response = await call_next(request)
        if not self._should_audit(request, response):
            return response
        try:
            event = self._build_event(request, response, int((self._clock() - t0) * 1000))
            await self._writer.enqueue(
                event,
                critical=is_critical_action(event.action, http_method=request.method),
            )
            _emit_identity_metric(event.action)
        except Exception:
            # Audit must never break the request.
            logger.exception("audit middleware dispatch failed")
        return response

    # ------------------------------------------------------------------

    def _should_audit(self, request: Request, response: Response) -> bool:
        path = request.url.path
        if any(path.startswith(p) for p in self._skip):
            # Still audit explicit auth failures on skipped paths — but
            # /api/me and /health are fully silent.
            return False

        method = request.method.upper()
        if method != "GET":
            return True

        # Read: audit only on known-interesting prefixes or denied responses.
        if any(path.startswith(p) for p in self._audit_reads):
            return True
        if response.status_code in (401, 403):
            return True
        return False

    def _build_event(self, request: Request, response: Response, dt_ms: int) -> AuditEvent:
        identity = getattr(request.state, "identity", None)
        tenant_id = getattr(identity, "tenant_id", None) if identity is not None else None
        user_id = getattr(identity, "user_id", None) if identity is not None else None

        # Workspace id from path if present.
        resource_type, resource_id = _extract_resource(request.url.path)

        action = _derive_action(request, response, resource_type=resource_type)
        metadata = redact_metadata(
            action,
            {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "query": dict(request.query_params),
            },
        )

        client_ip = request.client.host if request.client else None
        ua = request.headers.get("user-agent")

        workspace_id = self._extract_workspace_id(request)

        return AuditEvent(
            action=action,
            result=_result(response),
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            ip=client_ip,
            user_agent=ua,
            error_code=_error_code(response),
            duration_ms=dt_ms,
            metadata=metadata,
        )

    @staticmethod
    def _extract_workspace_id(request: Request) -> int | None:
        params = request.path_params or {}
        for key in ("wid", "workspace_id", "ws_id"):
            if key in params:
                try:
                    return int(params[key])
                except (TypeError, ValueError):
                    return None
        return None


def _emit_identity_metric(action: str) -> None:
    """Mirror auditable actions onto the Prometheus counters.

    We deliberately *mirror* rather than replace: operators who do not
    scrape ``/metrics`` still get everything from the audit log, and
    operators who do scrape get the low-cardinality counters this module
    exposes. Import is local so a metrics-module typo cannot break the
    audit path.
    """

    try:
        from app.gateway.identity.metrics import record_authz_denied, record_login
    except Exception:  # noqa: BLE001 — metrics are best-effort
        return

    if action == "user.login.success":
        record_login(success=True)
    elif action == "user.login.failure":
        record_login(success=False)
    elif action in ("authz.api.denied", "authz.tool.denied"):
        record_authz_denied()
