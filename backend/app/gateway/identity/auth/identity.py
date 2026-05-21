"""The ``Identity`` dataclass attached to every request after middleware runs.

This is the *runtime* identity (read by routes, decorators, audit writer),
as distinct from the ORM ``User``/``Tenant``/``Role`` rows that back it.

M3 adds permission-checking helpers (``has_permission``, ``in_tenant``,
``in_workspace``) plus ``is_platform_admin`` bypass. Platform admins
short-circuit every check so operators can always recover access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TokenType = Literal["anonymous", "jwt", "api_token"]

PLATFORM_ADMIN_ROLE_KEY = "platform_admin"


@dataclass(frozen=True)
class Identity:
    """Authenticated request principal.

    ``token_type == "anonymous"`` → everything else is empty/None.
    """

    token_type: TokenType
    user_id: int | None
    email: str | None
    tenant_id: int | None
    workspace_ids: tuple[int, ...] = ()
    permissions: frozenset[str] = field(default_factory=frozenset)
    roles: dict = field(default_factory=dict)
    session_id: str | None = None
    ip: str | None = None

    @classmethod
    def anonymous(cls) -> Identity:
        return cls(
            token_type="anonymous",
            user_id=None,
            email=None,
            tenant_id=None,
            workspace_ids=(),
            permissions=frozenset(),
            roles={},
            session_id=None,
            ip=None,
        )

    @property
    def is_authenticated(self) -> bool:
        return self.token_type != "anonymous" and self.user_id is not None

    @property
    def is_platform_admin(self) -> bool:
        """True if this identity has the platform_admin role grant.

        Anonymous identities never count, even if roles dict is malformed.
        """
        if not self.is_authenticated:
            return False
        platform_roles = self.roles.get("platform") if isinstance(self.roles, dict) else None
        if not platform_roles:
            return False
        return PLATFORM_ADMIN_ROLE_KEY in platform_roles

    def has_permission(self, tag: str) -> bool:
        """Check whether this identity may perform the action tagged ``tag``.

        Platform admin identities bypass the check. Anonymous identities
        always fail.
        """
        if not self.is_authenticated:
            return False
        if self.is_platform_admin:
            return True
        return tag in self.permissions

    def in_tenant(self, tenant_id: int) -> bool:
        """Check whether this identity is acting inside ``tenant_id``."""
        if not self.is_authenticated:
            return False
        if self.is_platform_admin:
            return True
        return self.tenant_id == tenant_id

    def in_workspace(self, workspace_id: int) -> bool:
        """Check whether this identity has a membership in ``workspace_id``."""
        if not self.is_authenticated:
            return False
        if self.is_platform_admin:
            return True
        return workspace_id in self.workspace_ids
