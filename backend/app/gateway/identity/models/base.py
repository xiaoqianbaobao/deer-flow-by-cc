"""Declarative Base and scope mixins for identity ORM models."""

from sqlalchemy import BigInteger, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_SCHEMA = "identity"


class Base(DeclarativeBase):
    """Declarative base scoped to the `identity` Postgres schema."""

    metadata = MetaData(schema=_SCHEMA)


class TenantScoped:
    """Mixin: adds indexed `tenant_id BIGINT NOT NULL`. Classes using this mixin
    plus Base become candidates for the auto-filter middleware added in M3.
    """

    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)


class WorkspaceScoped(TenantScoped):
    """Mixin: adds indexed `workspace_id BIGINT NOT NULL` in addition to tenant_id."""

    workspace_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
