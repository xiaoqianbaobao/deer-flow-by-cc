"""Tests for models.base: declarative Base, TenantScoped, WorkspaceScoped mixins."""

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.gateway.identity.models.base import Base, TenantScoped, WorkspaceScoped


def test_base_has_metadata():
    assert Base.metadata is not None
    assert Base.metadata.schema == "identity"


def test_tenant_scoped_adds_tenant_id_column():
    class Widget(TenantScoped, Base):
        __tablename__ = "widgets_test_ts"
        id: Mapped[int] = mapped_column(Integer, primary_key=True)

    cols = {c.name for c in Widget.__table__.columns}
    assert "tenant_id" in cols
    assert Widget.__table__.columns["tenant_id"].nullable is False


def test_workspace_scoped_adds_both_columns():
    class Gadget(WorkspaceScoped, Base):
        __tablename__ = "gadgets_test_ws"
        id: Mapped[int] = mapped_column(Integer, primary_key=True)

    cols = {c.name for c in Gadget.__table__.columns}
    assert "tenant_id" in cols
    assert "workspace_id" in cols


def test_mixins_create_indexes_on_scope_cols():
    class Thing(WorkspaceScoped, Base):
        __tablename__ = "things_test_idx"
        id: Mapped[int] = mapped_column(Integer, primary_key=True)

    indexed_cols = set()
    for idx in Thing.__table__.indexes:
        for col in idx.columns:
            indexed_cols.add(col.name)
    assert "tenant_id" in indexed_cols
    assert "workspace_id" in indexed_cols
