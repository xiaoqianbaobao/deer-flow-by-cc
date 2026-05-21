"""`@requires(tag, scope)` — FastAPI dependency factory for permission checks.

Three decision points per request:

1. Authentication: anonymous → 401 with ``UNAUTHENTICATED``.
2. Permission: identity lacks ``tag`` → 403 with ``PERMISSION_DENIED``
   (``missing`` field carries the tag for UI messaging).
3. Horizontal scope (tenant / workspace): when the route carries the
   matching path param, the identity's tenant/workspace set must contain
   the requested id. Mismatch → 403 with ``horizontal=True`` on the
   audit queue.

Denials are forwarded to ``_queue_denied`` which M6 replaces with the
audit-event writer. Until then it's a best-effort logger hook — tests
monkeypatch it.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import HTTPException, Request, status

from app.gateway.identity.auth.identity import Identity

logger = logging.getLogger(__name__)

Scope = Literal["platform", "tenant", "workspace"]

_TENANT_PARAM_KEYS: tuple[str, ...] = ("tid", "tenant_id")
_WORKSPACE_PARAM_KEYS: tuple[str, ...] = ("wid", "workspace_id", "ws_id")


def requires(tag: str, scope: Scope):
    """Return a FastAPI dependency enforcing ``tag`` + scope constraint.

    Usage::

        @router.get("/api/tenants/{tid}/something",
                    dependencies=[Depends(requires("tenant:read", "tenant"))])

    - ``scope="platform"``: only the permission check runs.
    - ``scope="tenant"``: if the route has ``tid`` / ``tenant_id`` in
      path params, the identity must be in that tenant (unless
      platform admin). Missing path param means this is a cross-tenant
      list endpoint like ``/api/admin/tenants`` — falls through to
      permission check only.
    - ``scope="workspace"``: same pattern for ``wid`` / ``workspace_id`` /
      ``ws_id``.
    """

    async def dep(request: Request) -> Identity:
        identity: Identity = getattr(request.state, "identity", None) or Identity.anonymous()
        if not identity.is_authenticated:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_code": "UNAUTHENTICATED"},
            )
        if not identity.has_permission(tag):
            _queue_denied(identity, tag, scope, request)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_code": "PERMISSION_DENIED", "missing": tag},
            )
        if scope == "tenant":
            tid = _extract_int(request, _TENANT_PARAM_KEYS)
            if tid is not None and not identity.in_tenant(tid):
                _queue_denied(identity, tag, scope, request, horizontal=True)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error_code": "PERMISSION_DENIED"},
                )
        elif scope == "workspace":
            wid = _extract_int(request, _WORKSPACE_PARAM_KEYS)
            if wid is not None and not identity.in_workspace(wid):
                _queue_denied(identity, tag, scope, request, horizontal=True)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error_code": "PERMISSION_DENIED"},
                )
        return identity

    dep.__name__ = f"requires_{tag.replace(':', '_')}_{scope}"
    return dep


def _extract_int(request: Request, keys: tuple[str, ...]) -> int | None:
    params = request.path_params or {}
    for key in keys:
        if key in params:
            try:
                return int(params[key])
            except (TypeError, ValueError):
                return None
    return None


def _queue_denied(
    identity: Identity,
    tag: str,
    scope: Scope,
    request: Request,
    *,
    horizontal: bool = False,
) -> None:
    """Audit hook for denied requests.

    Logs and (when M6 audit writer is mounted on ``app.state``) enqueues
    a real ``authz.api.denied`` AuditEvent marked critical so PG outage
    routes it to the fallback file.
    """
    logger.info(
        "authz.api.denied",
        extra={
            "user_id": identity.user_id,
            "tenant_id": identity.tenant_id,
            "tag": tag,
            "scope": scope,
            "horizontal": horizontal,
            "method": request.method,
            "path": request.url.path,
            "ip": identity.ip,
        },
    )

    writer = getattr(getattr(request.app, "state", None), "audit_writer", None)
    if writer is None:
        return
    try:
        from app.gateway.identity.audit.events import AuditEvent
        from app.gateway.identity.audit.redact import redact_metadata

        ev = AuditEvent(
            action="authz.api.denied",
            result="failure",
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            ip=identity.ip,
            error_code="PERMISSION_DENIED",
            metadata=redact_metadata(
                "authz.api.denied",
                {
                    "tag": tag,
                    "scope": scope,
                    "horizontal": horizontal,
                    "method": request.method,
                    "path": request.url.path,
                },
            ),
        )
        # Run on the running loop without awaiting — RBAC dependency is sync.
        import asyncio

        asyncio.create_task(writer.enqueue(ev, critical=True))
    except Exception:
        logger.debug("audit enqueue from RBAC failed", exc_info=True)
