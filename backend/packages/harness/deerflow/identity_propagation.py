"""Shared HMAC identity-propagation primitives (harness side).

Mirror image of :mod:`app.gateway.identity.propagation`. The harness layer
cannot import ``app.*`` (enforced by ``tests/test_harness_boundary.py``), so
the header constants and the canonical-payload / HMAC helpers live here and
the Gateway imports *this* module when signing outbound headers.

The Gateway-side wrapper adds:

* ``sign_identity_headers(Identity, ...)`` — accepts the Gateway
  :class:`~app.gateway.identity.auth.identity.Identity` dataclass and
  marshals it into the flat field list consumed here.
* ``verify_identity_headers(headers, ...) -> Identity`` — reconstructs the
  Gateway dataclass from verified fields.

Inside the agent runtime we only need the fields (tenant id, permissions,
etc.) — not the dataclass — so the harness ships a minimal
:func:`verify_identity_headers` that returns a plain dict.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

HEADER_USER_ID = "X-Deerflow-User-Id"
HEADER_TENANT_ID = "X-Deerflow-Tenant-Id"
HEADER_WORKSPACE_ID = "X-Deerflow-Workspace-Id"
HEADER_PERMISSIONS = "X-Deerflow-Permissions"
HEADER_SESSION_ID = "X-Deerflow-Session-Id"
HEADER_TS = "X-Deerflow-Identity-Ts"
HEADER_SIG = "X-Deerflow-Identity-Sig"

REQUIRED_HEADERS = (HEADER_USER_ID, HEADER_TENANT_ID, HEADER_PERMISSIONS, HEADER_TS, HEADER_SIG)


class PropagationError(Exception):
    """Base class for all identity-propagation failures."""


class MissingHeaderError(PropagationError):
    """One of the required ``X-Deerflow-*`` headers is absent."""


class InvalidSignatureError(PropagationError):
    """Signature verification failed — payload tampered or wrong key."""


class StaleTimestampError(PropagationError):
    """Timestamp is outside the allowed clock-skew window."""


@dataclass(frozen=True)
class VerifiedIdentity:
    """Minimal identity view for the harness / LangGraph runtime.

    Intentionally smaller than the Gateway ``Identity`` dataclass — middleware
    downstream only reads ``tenant_id``, ``workspace_id``, ``permissions``,
    and (for audit) ``user_id``/``session_id``.
    """

    user_id: int
    tenant_id: int
    workspace_id: int | None
    permissions: frozenset[str] = field(default_factory=frozenset)
    session_id: str | None = None

    def has_permission(self, tag: str) -> bool:
        return tag in self.permissions


def canonical_payload(
    user_id: int,
    tenant_id: int,
    workspace_id: int | None,
    permissions: tuple[str, ...],
    ts: int,
) -> bytes:
    """Build the string that HMAC is computed over."""
    perms_joined = ",".join(permissions)
    ws_str = "" if workspace_id is None else str(workspace_id)
    return f"{user_id}|{tenant_id}|{ws_str}|{perms_joined}|{ts}".encode()


def compute_signature(payload: bytes, key: bytes | str) -> str:
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    digest = hmac.new(key_bytes, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    if name in headers:
        return headers[name]
    lowered = name.lower()
    for k, v in headers.items():
        if k.lower() == lowered:
            return v
    return None


def verify_headers(
    headers: Mapping[str, str],
    *,
    key: bytes | str,
    skew_sec: int = 300,
    now: int | None = None,
) -> VerifiedIdentity:
    """Verify and decode identity headers into a :class:`VerifiedIdentity`.

    Raises :class:`MissingHeaderError`, :class:`InvalidSignatureError`, or
    :class:`StaleTimestampError` on failure.
    """
    missing = [h for h in REQUIRED_HEADERS if _get_header(headers, h) is None]
    if missing:
        raise MissingHeaderError(f"Missing required header(s): {', '.join(missing)}")

    raw_user_id = _get_header(headers, HEADER_USER_ID)
    raw_tenant_id = _get_header(headers, HEADER_TENANT_ID)
    raw_workspace_id = _get_header(headers, HEADER_WORKSPACE_ID)
    raw_permissions = _get_header(headers, HEADER_PERMISSIONS) or ""
    raw_ts = _get_header(headers, HEADER_TS)
    raw_sig = _get_header(headers, HEADER_SIG) or ""
    raw_session_id = _get_header(headers, HEADER_SESSION_ID)

    try:
        user_id = int(raw_user_id)  # type: ignore[arg-type]
        tenant_id = int(raw_tenant_id)  # type: ignore[arg-type]
        ts = int(raw_ts)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise InvalidSignatureError(f"Non-numeric identity field: {exc}") from exc

    workspace_id: int | None
    if raw_workspace_id is None or raw_workspace_id == "":
        workspace_id = None
    else:
        try:
            workspace_id = int(raw_workspace_id)
        except ValueError as exc:
            raise InvalidSignatureError(f"Non-numeric workspace id: {exc}") from exc

    perms_sorted = tuple(sorted(p for p in raw_permissions.split(",") if p))

    now_ts = int(now) if now is not None else int(time.time())
    if abs(now_ts - ts) > skew_sec:
        raise StaleTimestampError(f"Timestamp {ts} outside skew window ±{skew_sec}s of {now_ts}")

    expected_sig = compute_signature(canonical_payload(user_id, tenant_id, workspace_id, perms_sorted, ts), key)
    if not hmac.compare_digest(expected_sig, raw_sig):
        raise InvalidSignatureError("HMAC mismatch")

    return VerifiedIdentity(
        user_id=user_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        permissions=frozenset(perms_sorted),
        session_id=raw_session_id,
    )


def sign_headers(
    *,
    user_id: int,
    tenant_id: int,
    workspace_id: int | None,
    permissions: tuple[str, ...] | frozenset[str],
    session_id: str | None,
    key: bytes | str,
    ts: int | None = None,
) -> dict[str, str]:
    """Produce the header dict for a given set of identity fields.

    Gateway-side wrappers marshal their ``Identity`` dataclass into these
    arguments. Permissions are sorted before signing so order on the wire
    is never part of the signature contract.
    """
    ts = int(ts) if ts is not None else int(time.time())
    perms_sorted = tuple(sorted(permissions))
    sig = compute_signature(canonical_payload(user_id, tenant_id, workspace_id, perms_sorted, ts), key)

    headers: dict[str, str] = {
        HEADER_USER_ID: str(user_id),
        HEADER_TENANT_ID: str(tenant_id),
        HEADER_PERMISSIONS: ",".join(perms_sorted),
        HEADER_TS: str(ts),
        HEADER_SIG: sig,
    }
    if workspace_id is not None:
        headers[HEADER_WORKSPACE_ID] = str(workspace_id)
    if session_id:
        headers[HEADER_SESSION_ID] = session_id
    return headers
