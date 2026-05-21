"""Add partial unique index on api_tokens (prefix, token_hash).

The index only covers un-revoked rows so re-issuing a token with the same
prefix after revocation remains legal, while active duplicates are impossible.

Revision ID: 20260421_0002
Revises: 20260421_0001
Create Date: 2026-04-21
"""

from __future__ import annotations

from alembic import op

revision: str = "20260421_0002"
down_revision: str | None = "20260421_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_api_tokens_prefix_hash_active",
        "api_tokens",
        ["prefix", "token_hash"],
        unique=True,
        schema="identity",
        postgresql_where="revoked_at IS NULL",
    )


def downgrade() -> None:
    op.drop_index(
        "uq_api_tokens_prefix_hash_active",
        table_name="api_tokens",
        schema="identity",
    )
