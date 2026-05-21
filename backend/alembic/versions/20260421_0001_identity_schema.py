"""identity schema initial

Revision ID: 20260421_0001
Revises: None
Create Date: 2026-04-21

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB

from alembic import op

revision: str = "20260421_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS identity")

    op.create_table(
        "tenants",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("logo_url", sa.Text),
        sa.Column("plan", sa.String(32), server_default="free"),
        sa.Column("status", sa.SmallInteger, server_default="1"),
        sa.Column("owner_id", sa.BigInteger),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_by", sa.BigInteger),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="identity",
    )

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("display_name", sa.String(128)),
        sa.Column("avatar_url", sa.Text),
        sa.Column("status", sa.SmallInteger, server_default="1"),
        sa.Column("oidc_subject", sa.String(255)),
        sa.Column("oidc_provider", sa.String(64)),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("last_login_ip", INET),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("oidc_provider", "oidc_subject", name="uq_users_oidc"),
        schema="identity",
    )

    op.create_table(
        "memberships",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("identity.users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.BigInteger, sa.ForeignKey("identity.tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.SmallInteger, server_default="1"),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "tenant_id", name="uq_memberships_user_tenant"),
        schema="identity",
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"], schema="identity")
    op.create_index("ix_memberships_tenant_id", "memberships", ["tenant_id"], schema="identity")

    op.create_table(
        "workspaces",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.BigInteger, sa.ForeignKey("identity.tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("created_by", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_workspaces_tenant_slug"),
        schema="identity",
    )
    op.create_index("ix_workspaces_tenant_id", "workspaces", ["tenant_id"], schema="identity")

    op.create_table(
        "permissions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tag", sa.String(64), nullable=False, unique=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("description", sa.Text),
        schema="identity",
    )

    op.create_table(
        "roles",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("role_key", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("is_builtin", sa.Boolean, server_default="true"),
        sa.Column("display_name", sa.String(128)),
        sa.Column("description", sa.Text),
        sa.UniqueConstraint("role_key", "scope", name="uq_roles_key_scope"),
        schema="identity",
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.BigInteger, sa.ForeignKey("identity.roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission_id", sa.BigInteger, sa.ForeignKey("identity.permissions.id", ondelete="CASCADE"), primary_key=True),
        schema="identity",
    )

    op.create_table(
        "user_roles",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("identity.users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.BigInteger, sa.ForeignKey("identity.tenants.id", ondelete="CASCADE"), nullable=True),
        sa.Column("role_id", sa.BigInteger, sa.ForeignKey("identity.roles.id"), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "tenant_id", "role_id", name="uq_user_roles_tuple"),
        schema="identity",
    )
    op.create_index("ix_user_roles_user_id", "user_roles", ["user_id"], schema="identity")
    op.create_index("ix_user_roles_tenant_id", "user_roles", ["tenant_id"], schema="identity")
    # Platform-admin rows have NULL tenant_id; enforce at-most-one platform grant
    # per (user, role) via a partial unique index (SQL standard treats NULLs as distinct).
    op.execute("""
        CREATE UNIQUE INDEX uq_user_roles_platform
        ON identity.user_roles (user_id, role_id)
        WHERE tenant_id IS NULL
    """)

    op.create_table(
        "workspace_members",
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("identity.users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("workspace_id", sa.BigInteger, sa.ForeignKey("identity.workspaces.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role_id", sa.BigInteger, sa.ForeignKey("identity.roles.id"), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="identity",
    )

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.BigInteger, sa.ForeignKey("identity.tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("identity.users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.BigInteger, sa.ForeignKey("identity.workspaces.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("scopes", ARRAY(sa.String), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_ip", INET),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_by", sa.BigInteger),
        schema="identity",
    )
    op.create_index("ix_api_tokens_tenant_revoked", "api_tokens", ["tenant_id", "revoked_at"], schema="identity")
    op.create_index("ix_api_tokens_prefix", "api_tokens", ["prefix"], schema="identity")

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.BigInteger),
        sa.Column("user_id", sa.BigInteger),
        sa.Column("workspace_id", sa.BigInteger),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64)),
        sa.Column("resource_id", sa.String(128)),
        sa.Column("ip", INET),
        sa.Column("user_agent", sa.Text),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("metadata", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="identity",
    )
    op.create_index("ix_audit_logs_tenant_created", "audit_logs", ["tenant_id", sa.text("created_at DESC")], schema="identity")
    op.create_index("ix_audit_logs_user_created", "audit_logs", ["user_id", sa.text("created_at DESC")], schema="identity")
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"], schema="identity")


def downgrade() -> None:
    # Drop our tables in dependency-safe order. Leave the identity schema
    # and alembic_version table in place so alembic can record the downgrade;
    # the schema itself is dropped outside the migration by tooling if needed.
    for name in ["audit_logs", "api_tokens", "workspace_members", "user_roles", "role_permissions", "roles", "permissions", "workspaces", "memberships", "users", "tenants"]:
        op.drop_table(name, schema="identity")
