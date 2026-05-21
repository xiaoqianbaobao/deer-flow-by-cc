"""Internal, HMAC-authenticated endpoints called by the LangGraph runtime.

M5 ships a **stub** for ``POST /internal/audit`` — it verifies the HMAC
signature and appends the payload to an in-memory queue. M6 replaces the
queue with a real audit writer (DB insert + Redis-backed fallback file).

The signature scheme mirrors identity propagation (§5.4) but signs the
payload JSON + timestamp instead of identity fields:

    sig = base64url(HMAC-SHA256(key, body_bytes + "|" + ts))

``X-Deerflow-Internal-Sig`` and ``X-Deerflow-Internal-Ts`` must both be
present. Any tampering fails closed with 401.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.gateway.identity.settings import get_identity_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["identity-internal"])


class AuditEventPayload(BaseModel):
    """Minimal envelope the harness uses to report events back to Gateway.

    The schema is intentionally loose at M5 — M6 will tighten it once
    the audit writer's column set is finalized.
    """

    action: str = Field(..., description="Audit action string, e.g. 'authz.tool.denied'")
    tenant_id: int | None = None
    user_id: int | None = None
    workspace_id: int | None = None
    thread_id: str | None = None
    resource: str | None = None
    outcome: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


# M5 in-memory queue. Thread-safe because LangGraph worker threads may
# POST concurrently. M6 swaps this for a writer that hits Postgres.
_audit_queue: list[dict[str, Any]] = []
_audit_queue_lock = threading.Lock()

# Clock skew window for the internal-sig timestamp. Reuses the same
# default as identity propagation.
_INTERNAL_SKEW_SEC = 300


def _verify_internal_signature(body: bytes, sig: str, ts: str, key: str) -> None:
    try:
        ts_int = int(ts)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid timestamp") from exc

    now = int(time.time())
    if abs(now - ts_int) > _INTERNAL_SKEW_SEC:
        raise HTTPException(status_code=401, detail="stale timestamp")

    payload = body + b"|" + ts.encode("ascii")
    expected = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(expected_b64, sig):
        raise HTTPException(status_code=401, detail="invalid signature")


def sign_internal_payload(body: bytes, *, ts: int | None = None, key: str) -> tuple[str, str]:
    """Helper used by callers (and tests) to sign a body + ts.

    Returns ``(sig, ts_str)``. The matching ``verify`` path expects the
    same ``ts`` value on ``X-Deerflow-Internal-Ts``.
    """
    ts_str = str(int(ts) if ts is not None else int(time.time()))
    payload = body + b"|" + ts_str.encode("ascii")
    digest = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii"), ts_str


@router.post("/audit")
async def ingest_audit_event(
    request: Request,
    x_deerflow_internal_sig: str = Header(..., alias="X-Deerflow-Internal-Sig"),
    x_deerflow_internal_ts: str = Header(..., alias="X-Deerflow-Internal-Ts"),
) -> dict[str, str]:
    """Accept an audit event from the LangGraph runtime.

    M5 stub: append to in-memory queue and ACK. M6 will flush to DB.
    """
    settings = get_identity_settings()
    if not settings.internal_signing_key:
        # Misconfiguration — fail closed rather than silently accept.
        raise HTTPException(status_code=503, detail="internal signing key not configured")

    body = await request.body()
    _verify_internal_signature(
        body=body,
        sig=x_deerflow_internal_sig,
        ts=x_deerflow_internal_ts,
        key=settings.internal_signing_key,
    )

    try:
        payload = AuditEventPayload.model_validate_json(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid payload: {exc}") from exc

    # M6: forward to real AuditBatchWriter when present; otherwise fall
    # back to the M5 in-memory queue (kept for tests that haven't moved).
    writer = getattr(getattr(request.app, "state", None), "audit_writer", None)
    if writer is not None:
        from app.gateway.identity.audit.events import AuditEvent, is_critical_action
        from app.gateway.identity.audit.redact import redact_metadata

        meta = dict(payload.extra or {})
        if payload.thread_id:
            meta["thread_id"] = payload.thread_id
        if payload.resource:
            meta["resource"] = payload.resource
        if payload.outcome:
            meta["outcome"] = payload.outcome

        ev = AuditEvent(
            action=payload.action,
            result="failure" if (payload.outcome and payload.outcome != "success") else "success",
            tenant_id=payload.tenant_id,
            user_id=payload.user_id,
            workspace_id=payload.workspace_id,
            metadata=redact_metadata(payload.action, meta),
        )
        await writer.enqueue(ev, critical=is_critical_action(payload.action))
    else:
        with _audit_queue_lock:
            _audit_queue.append(payload.model_dump())

    logger.debug(
        "audit event ingested: action=%s user=%s tenant=%s",
        payload.action,
        payload.user_id,
        payload.tenant_id,
    )
    return {"status": "queued"}


def drain_audit_queue_for_testing() -> list[dict[str, Any]]:
    """Test-only: return and clear the in-memory queue.

    Not exposed over HTTP — only Python callers can reach this.
    """
    with _audit_queue_lock:
        events = list(_audit_queue)
        _audit_queue.clear()
    return events
