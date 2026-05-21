"""Read-only roles + permissions endpoints (M3).

The admin UI calls these to render the role matrix and to know which
permissions to guard buttons with. No write endpoints here — role
assignment is an M7 admin-UI concern. We require only
``require_authenticated`` because every logged-in user needs this data
to know what they can/can't do.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.identity.auth.dependencies import require_authenticated
from app.gateway.identity.db import get_session
from app.gateway.identity.models.role import Permission, Role

router = APIRouter()


@router.get("/api/roles", dependencies=[Depends(require_authenticated)])
async def list_roles(session: AsyncSession = Depends(get_session)) -> dict:
    rows = (await session.execute(select(Role).order_by(Role.scope, Role.role_key))).scalars().all()
    return {
        "roles": [
            {
                "role_key": r.role_key,
                "scope": r.scope,
                "display_name": r.display_name,
                "description": r.description,
                "is_builtin": r.is_builtin,
            }
            for r in rows
        ]
    }


@router.get("/api/permissions", dependencies=[Depends(require_authenticated)])
async def list_permissions(session: AsyncSession = Depends(get_session)) -> dict:
    rows = (await session.execute(select(Permission).order_by(Permission.scope, Permission.tag))).scalars().all()
    return {
        "permissions": [
            {
                "tag": p.tag,
                "scope": p.scope,
                "description": p.description,
            }
            for p in rows
        ]
    }
