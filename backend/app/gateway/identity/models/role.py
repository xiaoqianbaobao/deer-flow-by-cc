"""Role, Permission, RolePermission, UserRole ORM models."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.gateway.identity.models.base import Base

# user_roles needs a nullable tenant_id (NULL means platform-scoped grant),
# but Postgres primary keys must be NOT NULL. We therefore use a surrogate
# BIGINT id primary key plus a unique constraint on the logical tuple, and a
# partial unique index (in the migration) to enforce singleton platform grants.


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tag: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("role_key", "scope", name="uq_roles_key_scope"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role_key: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, server_default="true")
    display_name: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("identity.roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("identity.permissions.id", ondelete="CASCADE"), primary_key=True)


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id", "role_id", name="uq_user_roles_tuple"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("identity.users.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("identity.tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    role_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("identity.roles.id"), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
