"""HMAC-signed identity propagation between Gateway and LangGraph runtime.

Thin Gateway-side wrapper around :mod:`deerflow.identity_propagation`. The
canonical form, HMAC helpers, and verification logic live in the harness so
the LangGraph-side ``IdentityMiddleware`` can share them without violating
the harness → app import firewall.

Gateway-side additions:

* :func:`sign_identity_headers` — accepts the Gateway :class:`Identity`
  dataclass and marshals it into the flat field list the harness signs.
* :func:`verify_identity_headers` — reconstructs a Gateway :class:`Identity`
  from verified fields (useful for debugging and for tests that want to
  round-trip through the full dataclass).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from deerflow.identity_propagation import (
    HEADER_PERMISSIONS,
    HEADER_SESSION_ID,
    HEADER_SIG,
    HEADER_TENANT_ID,
    HEADER_TS,
    HEADER_USER_ID,
    HEADER_WORKSPACE_ID,
    InvalidSignatureError,
    MissingHeaderError,
    PropagationError,
    StaleTimestampError,
    sign_headers,
    verify_headers,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.gateway.identity.auth.identity import Identity

__all__ = [
    "HEADER_PERMISSIONS",
    "HEADER_SESSION_ID",
    "HEADER_SIG",
    "HEADER_TENANT_ID",
    "HEADER_TS",
    "HEADER_USER_ID",
    "HEADER_WORKSPACE_ID",
    "InvalidSignatureError",
    "MissingHeaderError",
    "PropagationError",
    "StaleTimestampError",
    "sign_identity_headers",
    "verify_identity_headers",
]


def sign_identity_headers(
    identity: Identity,
    *,
    workspace_id: int | None,
    key: bytes | str,
    ts: int | None = None,
    session_id: str | None = None,
) -> dict[str, str]:
    """Sign headers for the Gateway :class:`Identity`.

    ``workspace_id`` is the **currently active** workspace (typically the
    path parameter of the Gateway route), distinct from
    ``identity.workspace_ids`` which is the set of accessible workspaces.
    LangGraph only needs the active one; the full set stays Gateway-side.
    """
    if not identity.is_authenticated or identity.user_id is None or identity.tenant_id is None:
        raise ValueError("Cannot sign headers for anonymous identity")

    return sign_headers(
        user_id=identity.user_id,
        tenant_id=identity.tenant_id,
        workspace_id=workspace_id,
        permissions=identity.permissions,
        session_id=session_id if session_id is not None else identity.session_id,
        key=key,
        ts=ts,
    )


def verify_identity_headers(
    headers: Mapping[str, str],
    *,
    key: bytes | str,
    skew_sec: int = 300,
    now: int | None = None,
) -> Identity:
    """Verify and reconstruct a Gateway :class:`Identity` from *headers*."""
    from app.gateway.identity.auth.identity import Identity

    verified = verify_headers(headers, key=key, skew_sec=skew_sec, now=now)
    workspace_ids = (verified.workspace_id,) if verified.workspace_id is not None else ()

    # token_type="jwt" is a wire-level label; the originating auth
    # mechanism (OIDC cookie vs API token) is not carried across the
    # internal boundary — downstream middlewares only care that the
    # identity is authenticated.
    return Identity(
        token_type="jwt",
        user_id=verified.user_id,
        email=None,
        tenant_id=verified.tenant_id,
        workspace_ids=workspace_ids,
        permissions=verified.permissions,
        roles={},
        session_id=verified.session_id,
        ip=None,
    )
