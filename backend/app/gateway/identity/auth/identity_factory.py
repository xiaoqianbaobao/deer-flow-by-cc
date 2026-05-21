"""First-login policy + Identity builder.

Three pieces make up the OIDC → session flow:

1. ``upsert_oidc_user`` — match by ``(provider, subject)``; fall back to
   email for existing in-DB users who have not yet bound an IdP; else
   create a new ``User`` row.
2. ``resolve_active_tenant`` — pick the active tenant at login time.
   v1 default is ``auto_provision=False`` (spec §5.5): no membership →
   return ``(None, None)`` so the router can redirect to the "not invited"
   page. With auto-provision on, we create a personal tenant + workspace
   and make the user ``tenant_owner`` + ``workspace_admin`` there.
3. ``build_identity_for_user`` — flatten user_roles + workspace_members
   into the ``Identity`` dataclass consumed by middleware/routers.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.identity.auth.identity import Identity
from app.gateway.identity.auth.oidc import OIDCUserInfo
from app.gateway.identity.models.role import Permission, Role, RolePermission, UserRole
from app.gateway.identity.models.tenant import Tenant, Workspace
from app.gateway.identity.models.user import Membership, User, WorkspaceMember


async def upsert_oidc_user(session: AsyncSession, info: OIDCUserInfo) -> User:
    # 1) Match by (provider, subject).
    q = select(User).where(User.oidc_provider == info.provider, User.oidc_subject == info.subject)
    user = (await session.execute(q)).scalar_one_or_none()
    if user is not None:
        if info.email and user.email != info.email:
            user.email = info.email
        if info.display_name and user.display_name != info.display_name:
            user.display_name = info.display_name
        user.last_login_at = datetime.now(UTC)
        await session.flush()
        return user

    # 2) Fall back to email (bind existing unlinked account).
    q = select(User).where(User.email == info.email)
    user = (await session.execute(q)).scalar_one_or_none()
    if user is not None:
        user.oidc_provider = info.provider
        user.oidc_subject = info.subject
        if info.display_name and not user.display_name:
            user.display_name = info.display_name
        user.last_login_at = datetime.now(UTC)
        await session.flush()
        return user

    # 3) Fresh user.
    user = User(
        email=info.email,
        display_name=info.display_name or (info.email.split("@")[0] if info.email else None),
        status=1,
        oidc_provider=info.provider,
        oidc_subject=info.subject,
        last_login_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    return user


async def resolve_active_tenant(
    session: AsyncSession,
    user: User,
    *,
    auto_provision: bool = False,
) -> tuple[Tenant | None, Workspace | None]:
    # Find memberships → pick alpha-first active tenant.
    q = select(Tenant).join(Membership, Membership.tenant_id == Tenant.id).where(Membership.user_id == user.id, Membership.status == 1).order_by(Tenant.slug)
    tenant = (await session.execute(q)).scalars().first()

    if tenant is not None:
        ws = (await session.execute(select(Workspace).where(Workspace.tenant_id == tenant.id).order_by(Workspace.slug))).scalars().first()
        return tenant, ws

    if not auto_provision:
        return None, None

    # Auto-provision: create personal tenant + workspace, grant tenant_owner + workspace_admin.
    name_root = (user.display_name or (user.email.split("@")[0] if user.email else "user")).strip() or "user"
    slug = _safe_slug(user.email.split("@")[0] if user.email else f"user-{user.id}")
    tenant = Tenant(slug=slug, name=f"{name_root}'s workspace", status=1, owner_id=user.id, created_by=user.id)
    session.add(tenant)
    await session.flush()
    ws = Workspace(tenant_id=tenant.id, slug="default", name="Default", created_by=user.id)
    session.add(ws)
    session.add(Membership(user_id=user.id, tenant_id=tenant.id, status=1))
    await session.flush()

    tenant_owner = (await session.execute(select(Role).where(Role.role_key == "tenant_owner"))).scalar_one()
    ws_admin = (await session.execute(select(Role).where(Role.role_key == "workspace_admin"))).scalar_one()
    session.add(UserRole(user_id=user.id, tenant_id=tenant.id, role_id=tenant_owner.id))
    session.add(WorkspaceMember(user_id=user.id, workspace_id=ws.id, role_id=ws_admin.id))
    await session.flush()
    return tenant, ws


async def build_identity_for_user(
    session: AsyncSession,
    user: User,
    tenant: Tenant,
    workspace: Workspace | None,
) -> Identity:
    # Collect role grants relevant at login: platform (tenant_id IS NULL) + this tenant.
    user_roles_q = (
        select(UserRole, Role)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            UserRole.user_id == user.id,
            (UserRole.tenant_id.is_(None)) | (UserRole.tenant_id == tenant.id),
        )
    )
    platform_roles: list[str] = []
    tenant_roles: list[str] = []
    role_ids: set[int] = set()
    for ur, role in (await session.execute(user_roles_q)).all():
        role_ids.add(role.id)
        if role.scope == "platform":
            platform_roles.append(role.role_key)
        elif role.scope == "tenant":
            tenant_roles.append(role.role_key)

    # Workspace memberships inside the active tenant.
    ws_q = select(WorkspaceMember, Workspace, Role).join(Workspace, Workspace.id == WorkspaceMember.workspace_id).join(Role, Role.id == WorkspaceMember.role_id).where(WorkspaceMember.user_id == user.id, Workspace.tenant_id == tenant.id)
    workspaces: dict[int, str] = {}
    for wm, ws, role in (await session.execute(ws_q)).all():
        workspaces[ws.id] = role.role_key
        role_ids.add(role.id)

    # Flatten permissions from every role in play.
    permissions: set[str] = set()
    if role_ids:
        perm_q = select(Permission.tag).join(RolePermission, RolePermission.permission_id == Permission.id).where(RolePermission.role_id.in_(role_ids))
        permissions = {tag for (tag,) in (await session.execute(perm_q)).all()}

    return Identity(
        token_type="jwt",
        user_id=user.id,
        email=user.email,
        tenant_id=tenant.id,
        workspace_ids=tuple(sorted(workspaces.keys())),
        permissions=frozenset(permissions),
        roles={
            "platform": sorted(set(platform_roles)),
            "tenant": sorted(set(tenant_roles)),
            "workspaces": {str(ws_id): key for ws_id, key in workspaces.items()},
        },
        session_id=None,
    )


def _safe_slug(raw: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return s or "user"
