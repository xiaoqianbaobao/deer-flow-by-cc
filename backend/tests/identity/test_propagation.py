"""HMAC propagation contract tests (M5 Task 1).

These exercise :mod:`app.gateway.identity.propagation` — the bidirectional
header format that the Gateway signs and the LangGraph-side middleware
verifies. Pure unit tests, no containers or I/O.
"""

from __future__ import annotations

import time

import pytest

from app.gateway.identity.auth.identity import Identity
from app.gateway.identity.propagation import (
    HEADER_PERMISSIONS,
    HEADER_SIG,
    HEADER_TENANT_ID,
    HEADER_TS,
    HEADER_USER_ID,
    HEADER_WORKSPACE_ID,
    InvalidSignatureError,
    MissingHeaderError,
    StaleTimestampError,
    sign_identity_headers,
    verify_identity_headers,
)

KEY = b"test-signing-key-super-secret-value"


def _make_identity(**overrides) -> Identity:
    defaults = dict(
        token_type="jwt",
        user_id=42,
        email="user@example.com",
        tenant_id=7,
        workspace_ids=(3, 9),
        permissions=frozenset({"thread:read", "thread:write", "skill:invoke"}),
        roles={"tenant": ["member"]},
        session_id="sess_abc123",
        ip="127.0.0.1",
    )
    defaults.update(overrides)
    return Identity(**defaults)


def test_roundtrip_sign_and_verify():
    identity = _make_identity()
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY)
    restored = verify_identity_headers(headers, key=KEY)

    assert restored.user_id == identity.user_id
    assert restored.tenant_id == identity.tenant_id
    assert restored.workspace_ids == (3,)  # only active workspace transits the wire
    assert restored.permissions == identity.permissions
    assert restored.session_id == identity.session_id
    assert restored.is_authenticated


def test_roundtrip_no_active_workspace():
    identity = _make_identity()
    headers = sign_identity_headers(identity, workspace_id=None, key=KEY)
    restored = verify_identity_headers(headers, key=KEY)

    assert HEADER_WORKSPACE_ID not in headers
    assert restored.workspace_ids == ()


def test_tamper_user_id_fails_signature():
    identity = _make_identity()
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY)
    headers[HEADER_USER_ID] = "999"

    with pytest.raises(InvalidSignatureError):
        verify_identity_headers(headers, key=KEY)


def test_tamper_permissions_fails_signature():
    identity = _make_identity()
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY)
    headers[HEADER_PERMISSIONS] = "admin:write," + headers[HEADER_PERMISSIONS]

    with pytest.raises(InvalidSignatureError):
        verify_identity_headers(headers, key=KEY)


def test_tamper_tenant_id_fails_signature():
    identity = _make_identity()
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY)
    headers[HEADER_TENANT_ID] = "999"

    with pytest.raises(InvalidSignatureError):
        verify_identity_headers(headers, key=KEY)


def test_stale_past_timestamp_rejected():
    identity = _make_identity()
    past = int(time.time()) - 10_000
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY, ts=past)

    with pytest.raises(StaleTimestampError):
        verify_identity_headers(headers, key=KEY, skew_sec=300)


def test_future_timestamp_rejected():
    identity = _make_identity()
    future = int(time.time()) + 10_000
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY, ts=future)

    with pytest.raises(StaleTimestampError):
        verify_identity_headers(headers, key=KEY, skew_sec=300)


def test_timestamp_at_edge_of_skew_accepted():
    identity = _make_identity()
    fixed_now = 1_745_000_000
    within = fixed_now - 299  # inside the 300s window
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY, ts=within)

    restored = verify_identity_headers(headers, key=KEY, skew_sec=300, now=fixed_now)
    assert restored.user_id == 42


@pytest.mark.parametrize("drop", [HEADER_USER_ID, HEADER_TENANT_ID, HEADER_PERMISSIONS, HEADER_TS, HEADER_SIG])
def test_missing_required_header_raises(drop):
    identity = _make_identity()
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY)
    headers.pop(drop)

    with pytest.raises(MissingHeaderError):
        verify_identity_headers(headers, key=KEY)


def test_permissions_order_insensitive():
    """Shuffling permissions on the wire must still verify.

    The signer sorts permissions before signing; verify must also sort
    before hashing so header transport reordering does not invalidate
    the signature.
    """
    identity = _make_identity()
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY)

    # Reverse the permissions header order; signature should still check
    # because verify sorts before recomputing.
    perms = headers[HEADER_PERMISSIONS].split(",")
    headers[HEADER_PERMISSIONS] = ",".join(reversed(perms))

    restored = verify_identity_headers(headers, key=KEY)
    assert restored.permissions == identity.permissions


def test_wrong_key_fails_signature():
    identity = _make_identity()
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY)

    with pytest.raises(InvalidSignatureError):
        verify_identity_headers(headers, key=b"wrong-key")


def test_anonymous_identity_cannot_be_signed():
    with pytest.raises(ValueError):
        sign_identity_headers(Identity.anonymous(), workspace_id=None, key=KEY)


def test_case_insensitive_header_lookup():
    identity = _make_identity()
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY)
    # Lowercase all header names to simulate some proxies / test clients.
    lowered = {k.lower(): v for k, v in headers.items()}

    restored = verify_identity_headers(lowered, key=KEY)
    assert restored.user_id == 42


def test_string_key_accepted():
    """sign/verify accept str keys (convenience wrapper around env vars)."""
    identity = _make_identity()
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY.decode())
    restored = verify_identity_headers(headers, key=KEY.decode())
    assert restored.user_id == 42


def test_empty_permissions_still_signs_and_verifies():
    """Identity with zero permissions is still a valid authenticated caller."""
    identity = _make_identity(permissions=frozenset())
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY)
    assert headers[HEADER_PERMISSIONS] == ""
    restored = verify_identity_headers(headers, key=KEY)
    assert restored.permissions == frozenset()


def test_sign_uses_override_timestamp():
    identity = _make_identity()
    fixed = 1_700_000_000
    headers = sign_identity_headers(identity, workspace_id=3, key=KEY, ts=fixed)
    assert headers[HEADER_TS] == str(fixed)
