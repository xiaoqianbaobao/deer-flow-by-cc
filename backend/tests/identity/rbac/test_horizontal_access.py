"""Matrix of 5 seed roles × key routes (Task 5).

Exercises the stub routes in ``routers/admin_stub.py`` against
synthetic identities built from the bootstrap role→permission map.
This proves that ``@requires`` enforces both permission presence AND
horizontal scoping (tenant/workspace) across all seed roles.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.identity.auth.identity import Identity
from app.gateway.identity.bootstrap import PREDEFINED_ROLE_PERMISSIONS
from app.gateway.identity.routers import admin_stub


def _identity_for_role(role_key: str, *, tenant_id: int, workspace_ids: tuple[int, ...]) -> Identity:
    """Build a realistic Identity for a named seed role.

    - ``platform_admin`` → platform scope, empty permissions set (bypass
      via role role is implicit via `is_platform_admin`).
    - other roles → permission set from the bootstrap mapping, plus a
      properly populated ``roles`` dict so RBAC helpers work.
    """
    platform_roles: list[str] = []
    tenant_roles: list[str] = []
    workspace_role_map: dict[str, str] = {}

    perms: set[str] = set()
    for (key, scope), tags in PREDEFINED_ROLE_PERMISSIONS.items():
        if key == role_key:
            if scope == "platform":
                platform_roles.append(key)
            elif scope == "tenant":
                tenant_roles.append(key)
                perms.update(tags)
            elif scope == "workspace":
                for wid in workspace_ids:
                    workspace_role_map[str(wid)] = key
                perms.update(tags)
            if scope == "tenant" and key == "tenant_owner":
                # tenant_owner inherits workspace perms (matches seed).
                pass

    if role_key == "platform_admin":
        # platform_admin gets every permission, but the RBAC helper uses
        # `is_platform_admin` to bypass — leaving perms empty is OK.
        perms = set()

    return Identity(
        token_type="jwt",
        user_id=1,
        email=f"{role_key}@example.com",
        tenant_id=tenant_id,
        workspace_ids=workspace_ids,
        permissions=frozenset(perms),
        roles={
            "platform": platform_roles,
            "tenant": tenant_roles,
            "workspaces": workspace_role_map,
        },
        session_id=f"sess-{role_key}",
    )


@pytest.fixture
def app_with_stubs():
    app = FastAPI()
    app.include_router(admin_stub.router)
    current: dict = {"identity": Identity.anonymous()}

    @app.middleware("http")
    async def inject_identity(request, call_next):
        request.state.identity = current["identity"]
        return await call_next(request)

    return app, current


MATRIX = [
    # role, method, path, expected_status
    # viewer: can read threads, cannot write
    ("viewer", "GET", "/api/tenants/1/workspaces/1/threads", 200),
    ("viewer", "POST", "/api/tenants/1/workspaces/1/threads", 403),
    # member: can read + write threads, cannot manage skills
    ("member", "GET", "/api/tenants/1/workspaces/1/threads", 200),
    ("member", "POST", "/api/tenants/1/workspaces/1/threads", 201),
    ("member", "DELETE", "/api/tenants/1/workspaces/1/skills/1", 403),
    # workspace_admin: can manage skills
    ("workspace_admin", "DELETE", "/api/tenants/1/workspaces/1/skills/1", 200),
    ("workspace_admin", "POST", "/api/tenants/1/workspaces/1/threads", 201),
    # tenant_owner: can create workspaces, cannot create tenants
    ("tenant_owner", "POST", "/api/tenants/1/workspaces", 201),
    ("tenant_owner", "POST", "/api/admin/tenants", 403),
    # platform_admin: can do anything
    ("platform_admin", "POST", "/api/admin/tenants", 201),
    ("platform_admin", "POST", "/api/tenants/1/workspaces", 201),
    ("platform_admin", "DELETE", "/api/tenants/99/workspaces/99/skills/99", 200),
]


@pytest.mark.parametrize("role,method,path,expected_status", MATRIX)
def test_rbac_matrix(app_with_stubs, role, method, path, expected_status):
    app, holder = app_with_stubs
    holder["identity"] = _identity_for_role(role, tenant_id=1, workspace_ids=(1,))
    with TestClient(app) as c:
        r = c.request(method, path)
    assert r.status_code == expected_status, f"role={role} {method} {path}: expected {expected_status}, got {r.status_code} body={r.text}"


# Horizontal-access (cross-tenant / cross-workspace) denials
HORIZONTAL = [
    # tenant_owner of tenant 1 trying to act on tenant 99 → 403
    ("tenant_owner", "POST", "/api/tenants/99/workspaces", 403),
    # member acting on a workspace they're not in → 403
    ("member", "POST", "/api/tenants/1/workspaces/99/threads", 403),
    # workspace_admin on a workspace they're not in → 403
    ("workspace_admin", "DELETE", "/api/tenants/1/workspaces/99/skills/1", 403),
]


@pytest.mark.parametrize("role,method,path,expected_status", HORIZONTAL)
def test_horizontal_denials(app_with_stubs, role, method, path, expected_status):
    app, holder = app_with_stubs
    holder["identity"] = _identity_for_role(role, tenant_id=1, workspace_ids=(1,))
    with TestClient(app) as c:
        r = c.request(method, path)
    assert r.status_code == expected_status


def test_anonymous_rejected_everywhere(app_with_stubs):
    app, holder = app_with_stubs
    holder["identity"] = Identity.anonymous()
    with TestClient(app) as c:
        for method, path in [
            ("GET", "/api/tenants/1/workspaces/1/threads"),
            ("POST", "/api/tenants/1/workspaces"),
            ("POST", "/api/admin/tenants"),
        ]:
            r = c.request(method, path)
            assert r.status_code == 401, f"{method} {path} returned {r.status_code}"
