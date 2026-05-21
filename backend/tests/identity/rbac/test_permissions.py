"""Identity dataclass RBAC helpers (Task 1 + Task 8 helpers)."""

from __future__ import annotations

from app.gateway.identity.auth.identity import Identity


def _make(
    *,
    permissions=(),
    tenant_id=1,
    workspace_ids=(1,),
    roles=None,
    token_type="jwt",
    user_id=10,
) -> Identity:
    return Identity(
        token_type=token_type,
        user_id=user_id,
        email="u@example.com",
        tenant_id=tenant_id,
        workspace_ids=tuple(workspace_ids),
        permissions=frozenset(permissions),
        roles=roles or {"platform": [], "tenant": [], "workspaces": {}},
        session_id="sess-1",
    )


class TestHasPermission:
    def test_direct_tag(self):
        ident = _make(permissions={"thread:read"})
        assert ident.has_permission("thread:read") is True

    def test_missing_tag(self):
        ident = _make(permissions={"thread:read"})
        assert ident.has_permission("thread:write") is False

    def test_platform_admin_bypasses_any_tag(self):
        ident = _make(
            permissions=set(),
            roles={"platform": ["platform_admin"], "tenant": [], "workspaces": {}},
        )
        assert ident.is_platform_admin is True
        assert ident.has_permission("thread:write") is True
        assert ident.has_permission("any:random:tag") is True

    def test_anonymous_never_passes(self):
        ident = Identity.anonymous()
        assert ident.has_permission("thread:read") is False
        assert ident.is_platform_admin is False


class TestInTenant:
    def test_match(self):
        ident = _make(tenant_id=7)
        assert ident.in_tenant(7) is True

    def test_mismatch(self):
        ident = _make(tenant_id=7)
        assert ident.in_tenant(8) is False

    def test_platform_admin_any_tenant(self):
        ident = _make(
            tenant_id=7,
            roles={"platform": ["platform_admin"], "tenant": [], "workspaces": {}},
        )
        assert ident.in_tenant(999) is True

    def test_anonymous(self):
        assert Identity.anonymous().in_tenant(1) is False


class TestInWorkspace:
    def test_member(self):
        ident = _make(workspace_ids=(1, 2, 3))
        assert ident.in_workspace(2) is True

    def test_not_member(self):
        ident = _make(workspace_ids=(1, 2))
        assert ident.in_workspace(9) is False

    def test_platform_admin_any_workspace(self):
        ident = _make(
            workspace_ids=(),
            roles={"platform": ["platform_admin"], "tenant": [], "workspaces": {}},
        )
        assert ident.in_workspace(42) is True


class TestIsPlatformAdmin:
    def test_role_present(self):
        ident = _make(roles={"platform": ["platform_admin"], "tenant": [], "workspaces": {}})
        assert ident.is_platform_admin is True

    def test_role_absent(self):
        ident = _make(roles={"platform": [], "tenant": ["tenant_owner"], "workspaces": {}})
        assert ident.is_platform_admin is False

    def test_role_absent_when_platform_key_missing(self):
        ident = _make(roles={"tenant": []})
        assert ident.is_platform_admin is False
