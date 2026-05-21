"""Audit query + export API (spec §9.5).

``GET /api/tenants/{tid}/audit``
    Paginated list of audit rows scoped to one tenant. Filters:
    ``user_id``, ``action``, ``resource_type``, ``result``, ``date_from``,
    ``date_to``, ``cursor``, ``limit``.

``GET /api/tenants/{tid}/audit/export``
    CSV streaming of the same filter set, hard-capped at 100k rows (413
    if the filter matches more). Emits its own ``audit.exported`` event
    so the export action is itself captured.

``GET /api/admin/audit``
    Platform-admin cross-tenant listing. Same filter shape. Requires
    ``audit:read.all``.

Cursor encoding: ``base64url("{created_at_iso}|{id}")``. Decoded pairs
are pushed into a ``(created_at, id) < (cursor_created_at, cursor_id)``
predicate, which is stable under concurrent writes because ``id`` is a
monotonically increasing BIGSERIAL.
"""

from __future__ import annotations

import base64
import csv
import io
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.identity.audit.events import AuditEvent
from app.gateway.identity.db import get_session
from app.gateway.identity.models.audit import AuditLog
from app.gateway.identity.rbac.decorator import requires

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity-audit"])

DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 90
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
EXPORT_HARD_CAP = 100_000


# ---------------------------------------------------------------------------
# Cursor encoding
# ---------------------------------------------------------------------------


def encode_cursor(created_at: datetime, row_id: int) -> str:
    raw = f"{created_at.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    pad = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + pad).decode("utf-8")
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), int(id_str)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


# ---------------------------------------------------------------------------
# Filter assembly
# ---------------------------------------------------------------------------


def _resolve_window(date_from: datetime | None, date_to: datetime | None) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    end = date_to or now
    start = date_from or (end - timedelta(days=DEFAULT_WINDOW_DAYS))
    if end < start:
        raise HTTPException(status_code=400, detail="date_to must be >= date_from")
    if (end - start) > timedelta(days=MAX_WINDOW_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"window exceeds {MAX_WINDOW_DAYS} days",
        )
    return start, end


def _build_filters(
    *,
    tenant_id: int | None,
    user_id: int | None,
    action: str | None,
    resource_type: str | None,
    result: str | None,
    window: tuple[datetime, datetime],
    cursor: tuple[datetime, int] | None,
):
    start, end = window
    conditions = [
        AuditLog.created_at >= start,
        AuditLog.created_at <= end,
    ]
    if tenant_id is not None:
        conditions.append(AuditLog.tenant_id == tenant_id)
    if user_id is not None:
        conditions.append(AuditLog.user_id == user_id)
    if action is not None:
        conditions.append(AuditLog.action == action)
    if resource_type is not None:
        conditions.append(AuditLog.resource_type == resource_type)
    if result is not None:
        if result not in ("success", "failure"):
            raise HTTPException(status_code=400, detail="result must be success|failure")
        conditions.append(AuditLog.result == result)
    if cursor is not None:
        cur_ts, cur_id = cursor
        # Strictly-older-than-cursor: ORDER BY created_at DESC, id DESC.
        conditions.append(
            or_(
                AuditLog.created_at < cur_ts,
                and_(AuditLog.created_at == cur_ts, AuditLog.id < cur_id),
            )
        )
    return and_(*conditions)


def _row_to_dict(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "tenant_id": row.tenant_id,
        "user_id": row.user_id,
        "workspace_id": row.workspace_id,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "ip": str(row.ip) if row.ip is not None else None,
        "user_agent": row.user_agent,
        "result": row.result,
        "error_code": row.error_code,
        "duration_ms": row.duration_ms,
        "metadata": row.log_metadata or {},
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def _query_rows(
    session: AsyncSession,
    *,
    tenant_id: int | None,
    user_id: int | None,
    action: str | None,
    resource_type: str | None,
    result: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    cursor: str | None,
    limit: int,
) -> tuple[list[dict], str | None]:
    if limit <= 0 or limit > MAX_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit must be 1..{MAX_LIMIT}")
    window = _resolve_window(date_from, date_to)
    decoded_cursor = decode_cursor(cursor) if cursor else None

    stmt = (
        select(AuditLog)
        .where(
            _build_filters(
                tenant_id=tenant_id,
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                result=result,
                window=window,
                cursor=decoded_cursor,
            )
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit + 1)  # Peek-ahead for next_cursor.
    )
    rows = (await session.execute(stmt)).scalars().all()
    next_cursor: str | None = None
    if len(rows) > limit:
        tail = rows[limit - 1]
        next_cursor = encode_cursor(tail.created_at, tail.id)
        rows = rows[:limit]

    return [_row_to_dict(r) for r in rows], next_cursor


@router.get(
    "/api/tenants/{tid}/audit",
    dependencies=[Depends(requires("audit:read", "tenant"))],
)
async def list_audit(
    tid: int,
    user_id: int | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    result: Literal["success", "failure"] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
) -> dict:
    items, next_cursor = await _query_rows(
        session,
        tenant_id=tid,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        result=result,
        date_from=date_from,
        date_to=date_to,
        cursor=cursor,
        limit=limit,
    )
    return {"items": items, "next_cursor": next_cursor}


@router.get(
    "/api/admin/audit",
    dependencies=[Depends(requires("audit:read.all", "platform"))],
)
async def list_audit_cross_tenant(
    tenant_id: int | None = None,
    user_id: int | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    result: Literal["success", "failure"] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
) -> dict:
    items, next_cursor = await _query_rows(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        result=result,
        date_from=date_from,
        date_to=date_to,
        cursor=cursor,
        limit=limit,
    )
    return {"items": items, "next_cursor": next_cursor}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


_CSV_COLUMNS = (
    "id",
    "created_at",
    "tenant_id",
    "user_id",
    "workspace_id",
    "action",
    "resource_type",
    "resource_id",
    "ip",
    "user_agent",
    "result",
    "error_code",
    "duration_ms",
)


async def _count_matches(session: AsyncSession, filters) -> int:
    from sqlalchemy import func

    stmt = select(func.count(AuditLog.id)).where(filters)
    value = (await session.execute(stmt)).scalar_one()
    return int(value or 0)


async def _stream_csv(
    session: AsyncSession,
    filters,
    *,
    limit: int,
):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)

    stmt = select(AuditLog).where(filters).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
    result = await session.stream(stmt)
    async for (row,) in result:
        writer.writerow(
            [
                row.id,
                row.created_at.isoformat() if row.created_at else "",
                row.tenant_id if row.tenant_id is not None else "",
                row.user_id if row.user_id is not None else "",
                row.workspace_id if row.workspace_id is not None else "",
                row.action,
                row.resource_type or "",
                row.resource_id or "",
                row.ip or "",
                row.user_agent or "",
                row.result,
                row.error_code or "",
                row.duration_ms if row.duration_ms is not None else "",
            ]
        )
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


@router.get(
    "/api/tenants/{tid}/audit/export",
    dependencies=[Depends(requires("audit:read", "tenant"))],
)
async def export_audit_csv(
    request: Request,
    tid: int,
    user_id: int | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    result: Literal["success", "failure"] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    session: AsyncSession = Depends(get_session),
):
    window = _resolve_window(date_from, date_to)
    filters = _build_filters(
        tenant_id=tid,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        result=result,
        window=window,
        cursor=None,
    )

    total = await _count_matches(session, filters)
    if total > EXPORT_HARD_CAP:
        raise HTTPException(
            status_code=413,
            detail=f"export matches {total} rows; limit is {EXPORT_HARD_CAP}",
        )

    # Emit the export event itself (best-effort).
    await _enqueue_export_event(
        request,
        tid=tid,
        filters_view={
            "user_id": user_id,
            "action": action,
            "resource_type": resource_type,
            "result": result,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "matched": total,
        },
    )

    headers = {
        "Content-Disposition": f'attachment; filename="audit_tenant{tid}_{int(window[1].timestamp())}.csv"',
    }
    return StreamingResponse(
        _stream_csv(session, filters, limit=EXPORT_HARD_CAP),
        media_type="text/csv",
        headers=headers,
    )


async def _enqueue_export_event(request: Request, *, tid: int, filters_view: dict) -> None:
    writer = getattr(request.app.state, "audit_writer", None)
    if writer is None:
        return
    identity = getattr(request.state, "identity", None)
    ev = AuditEvent(
        action="audit.exported",
        result="success",
        tenant_id=tid,
        user_id=getattr(identity, "user_id", None),
        metadata=filters_view,
    )
    try:
        await writer.enqueue(ev, critical=False)
    except Exception:
        logger.exception("failed to enqueue audit.exported event")
