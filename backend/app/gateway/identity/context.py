"""ContextVars used by middleware/filters across the identity subsystem.

M1 added ``current_identity`` + ``current_tenant_id`` so later milestones
can read them without further structural changes. M2 adds
``current_session_id`` for audit logging and /me route convenience.
M3 adds ``_force_platform_mode`` + ``with_platform_privilege()`` so
maintenance code paths can bypass the tenant-scope auto-filter.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

current_identity: ContextVar[Any | None] = ContextVar("current_identity", default=None)
current_tenant_id: ContextVar[int | None] = ContextVar("current_tenant_id", default=None)
current_session_id: ContextVar[str | None] = ContextVar("current_session_id", default=None)

_force_platform_mode: ContextVar[bool] = ContextVar("force_platform_mode", default=False)


def is_force_platform_mode() -> bool:
    return _force_platform_mode.get()


@contextmanager
def with_platform_privilege():
    """Temporarily bypass the tenant-scope auto-filter.

    Use for maintenance scripts, admin jobs, or migrations that need to
    see every tenant. M6 hooks this to emit an audit event; for now we
    log at INFO so privileged access leaves a breadcrumb.
    """
    identity = current_identity.get()
    user_id = getattr(identity, "user_id", None) if identity else None
    logger.info("identity.platform_privilege.enter", extra={"user_id": user_id})
    token = _force_platform_mode.set(True)
    try:
        yield
    finally:
        _force_platform_mode.reset(token)
        logger.info("identity.platform_privilege.exit", extra={"user_id": user_id})
