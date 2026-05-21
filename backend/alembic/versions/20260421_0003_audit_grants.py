"""Enforce audit immutability at the DB layer.

Revokes UPDATE/DELETE on ``identity.audit_logs`` from the ``deerflow`` app
role. INSERT + SELECT remain so the writer can append rows and the query
API can read them. Retention archival is deliberately NOT granted — it
runs under a dedicated role (see ``deerflow_retention``) that has DELETE.

If the ``deerflow`` role does not exist (custom deploy), the migration
silently skips the REVOKEs. Developers running the app with a PG
superuser are not affected by grants (superusers bypass them), so the
protection is only active in production-style setups that use a
non-superuser role.

Revision ID: 20260421_0003
Revises: 20260421_0002
Create Date: 2026-04-21
"""

from __future__ import annotations

from alembic import op

revision: str = "20260421_0003"
down_revision: str | None = "20260421_0002"
branch_labels = None
depends_on = None


_APP_ROLE = "deerflow"
_RETENTION_ROLE = "deerflow_retention"


def _grants_sql() -> str:
    return f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
        EXECUTE 'REVOKE ALL ON identity.audit_logs FROM {_APP_ROLE}';
        EXECUTE 'GRANT INSERT, SELECT ON identity.audit_logs TO {_APP_ROLE}';
        EXECUTE 'GRANT USAGE ON SEQUENCE identity.audit_logs_id_seq TO {_APP_ROLE}';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_RETENTION_ROLE}') THEN
        EXECUTE 'GRANT SELECT, DELETE ON identity.audit_logs TO {_RETENTION_ROLE}';
        EXECUTE 'GRANT USAGE ON SEQUENCE identity.audit_logs_id_seq TO {_RETENTION_ROLE}';
    END IF;
END $$;
"""


def _downgrade_sql() -> str:
    return f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
        EXECUTE 'GRANT ALL ON identity.audit_logs TO {_APP_ROLE}';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_RETENTION_ROLE}') THEN
        EXECUTE 'REVOKE ALL ON identity.audit_logs FROM {_RETENTION_ROLE}';
    END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_grants_sql())


def downgrade() -> None:
    op.execute(_downgrade_sql())
