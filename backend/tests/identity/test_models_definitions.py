"""Structural tests: ORM definitions must match spec DDL."""

from app.gateway.identity.models import (
    ApiToken,
    AuditLog,
    Membership,
    Permission,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
    Workspace,
    WorkspaceMember,
)


def _colnames(model) -> set[str]:
    return {c.name for c in model.__table__.columns}


def test_tenant_columns():
    cols = _colnames(Tenant)
    assert cols >= {"id", "slug", "name", "logo_url", "plan", "status", "owner_id", "expires_at", "created_at", "created_by", "updated_at"}


def test_user_columns_and_unique_constraints():
    cols = _colnames(User)
    assert cols >= {"id", "email", "display_name", "avatar_url", "status", "oidc_subject", "oidc_provider", "last_login_at", "last_login_ip", "created_at"}
    uniques = {tuple(sorted(c.name for c in constraint.columns)) for constraint in User.__table__.constraints if constraint.__class__.__name__ == "UniqueConstraint"}
    assert ("email",) in uniques
    assert ("oidc_provider", "oidc_subject") in uniques


def test_workspace_unique_slug_per_tenant():
    uniques = {tuple(sorted(c.name for c in constraint.columns)) for constraint in Workspace.__table__.constraints if constraint.__class__.__name__ == "UniqueConstraint"}
    assert ("slug", "tenant_id") in uniques


def test_membership_unique_user_tenant():
    uniques = {tuple(sorted(c.name for c in constraint.columns)) for constraint in Membership.__table__.constraints if constraint.__class__.__name__ == "UniqueConstraint"}
    assert ("tenant_id", "user_id") in uniques


def test_permission_tag_unique():
    uniques = {tuple(sorted(c.name for c in constraint.columns)) for constraint in Permission.__table__.constraints if constraint.__class__.__name__ == "UniqueConstraint"}
    assert ("tag",) in uniques


def test_role_unique_key_scope():
    uniques = {tuple(sorted(c.name for c in constraint.columns)) for constraint in Role.__table__.constraints if constraint.__class__.__name__ == "UniqueConstraint"}
    assert ("role_key", "scope") in uniques


def test_role_permission_composite_pk():
    pk_cols = {c.name for c in RolePermission.__table__.primary_key.columns}
    assert pk_cols == {"role_id", "permission_id"}


def test_user_role_tuple_unique():
    # Surrogate `id` PK plus a unique constraint on the logical tuple so that
    # `tenant_id` can stay nullable for platform-scoped grants.
    pk_cols = {c.name for c in UserRole.__table__.primary_key.columns}
    assert pk_cols == {"id"}
    uniques = {tuple(sorted(c.name for c in constraint.columns)) for constraint in UserRole.__table__.constraints if constraint.__class__.__name__ == "UniqueConstraint"}
    assert ("role_id", "tenant_id", "user_id") in uniques


def test_workspace_member_composite_pk():
    pk_cols = {c.name for c in WorkspaceMember.__table__.primary_key.columns}
    assert pk_cols == {"user_id", "workspace_id"}


def test_api_token_columns():
    cols = _colnames(ApiToken)
    assert cols >= {
        "id",
        "tenant_id",
        "user_id",
        "workspace_id",
        "name",
        "prefix",
        "token_hash",
        "scopes",
        "expires_at",
        "last_used_at",
        "last_used_ip",
        "revoked_at",
        "created_at",
        "created_by",
    }


def test_audit_log_columns():
    cols = _colnames(AuditLog)
    assert cols >= {
        "id",
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
        "metadata",
        "created_at",
    }


def test_api_token_scopes_is_array_of_strings():
    col = ApiToken.__table__.columns["scopes"]
    assert col.type.__class__.__name__ == "ARRAY"
