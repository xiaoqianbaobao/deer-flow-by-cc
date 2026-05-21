"""SQLAlchemy event-listener that injects per-request tenant + workspace
filters into every SELECT, plus a flush guard that rejects cross-tenant
inserts. Installed once per sessionmaker at app startup when
``ENABLE_IDENTITY=true``.

Uses the current ``Identity`` from the ContextVar set by
``IdentityMiddleware``. Platform admins bypass the filter entirely; the
``with_platform_privilege()`` context manager temporarily extends that
bypass to regular identities (for admin jobs, migrations, etc.).

The filter is applied to any mapped class that inherits from
``TenantScoped`` / ``WorkspaceScoped``. New M4 tables get filtered
automatically — they just have to declare the mixin.
"""

from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from app.gateway.identity.context import current_identity, is_force_platform_mode
from app.gateway.identity.models.base import TenantScoped, WorkspaceScoped
from app.gateway.identity.rbac.errors import PermissionDeniedError

logger = logging.getLogger(__name__)


def install_auto_filter(session_maker) -> None:
    """Attach ``do_orm_execute`` + ``before_flush`` listeners.

    ``session_maker`` is either a sync ``sessionmaker`` or an
    ``async_sessionmaker``; events are registered on the global ``Session``
    class, so either works. Calling this twice with the same maker is a
    no-op — SQLAlchemy's ``listen()`` idempotently deduplicates.
    """

    @event.listens_for(Session, "do_orm_execute", propagate=False)
    def _filter(execute_state) -> None:
        if not execute_state.is_select:
            return
        identity = current_identity.get()
        if identity is None or not getattr(identity, "is_authenticated", False):
            return
        if getattr(identity, "is_platform_admin", False) and not is_force_platform_mode():
            # Platform admin: no auto-filter unless someone explicitly forced it.
            return
        if is_force_platform_mode():
            return
        tenant_id = identity.tenant_id
        if tenant_id is None:
            return

        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                TenantScoped,
                lambda cls: cls.tenant_id == tenant_id,
                include_aliases=True,
            )
        )
        workspace_ids = tuple(identity.workspace_ids or ())
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                WorkspaceScoped,
                lambda cls: cls.workspace_id.in_(workspace_ids) if workspace_ids else False,
                include_aliases=True,
            )
        )

    @event.listens_for(Session, "before_flush", propagate=False)
    def _insert_guard(session, flush_context, instances) -> None:
        identity = current_identity.get()
        if identity is None or not getattr(identity, "is_authenticated", False):
            return
        if getattr(identity, "is_platform_admin", False):
            return
        if is_force_platform_mode():
            return
        tenant_id = identity.tenant_id
        workspace_ids = set(identity.workspace_ids or ())
        for obj in session.new:
            if isinstance(obj, TenantScoped):
                if getattr(obj, "tenant_id", None) != tenant_id:
                    raise PermissionDeniedError(f"cross-tenant insert rejected (expected {tenant_id}, got {obj.tenant_id})")
            if isinstance(obj, WorkspaceScoped):
                if getattr(obj, "workspace_id", None) not in workspace_ids:
                    raise PermissionDeniedError(f"cross-workspace insert rejected (workspace {obj.workspace_id} not in {sorted(workspace_ids)})")
