"""Tests for app.gateway.identity.auth.jwt."""

from __future__ import annotations

import base64
import time

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.gateway.identity.auth.jwt import (
    AccessTokenClaims,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    TokenExpiredError,
    ensure_rsa_keypair,
    generate_refresh_token,
    issue_access_token,
    verify_access_token,
)


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[str, str]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_pem = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv_pem, pub_pem


def _claims(**overrides) -> AccessTokenClaims:
    now = int(time.time())
    defaults = dict(
        sub="42",
        email="u@example.com",
        tid=1,
        wids=[1],
        permissions=["workspace.read"],
        roles={"platform": [], "tenant": ["tenant_admin"], "workspaces": {"1": "workspace_editor"}},
        sid="sess-abc",
        iat=now,
        exp=now + 900,
        iss="deerflow",
        aud="deerflow-api",
    )
    defaults.update(overrides)
    return AccessTokenClaims(**defaults)


def test_sign_and_verify_roundtrip(rsa_keys):
    priv, pub = rsa_keys
    claims = _claims()
    token = issue_access_token(claims, private_key_pem=priv)
    back = verify_access_token(token, public_key_pem=pub, issuer="deerflow", audience="deerflow-api")
    assert back.sub == "42"
    assert back.email == "u@example.com"
    assert back.tid == 1
    assert back.wids == [1]
    assert back.permissions == ["workspace.read"]
    assert back.roles["tenant"] == ["tenant_admin"]
    assert back.sid == "sess-abc"


def test_tid_none_for_platform_admin(rsa_keys):
    priv, pub = rsa_keys
    claims = _claims(tid=None)
    token = issue_access_token(claims, private_key_pem=priv)
    back = verify_access_token(token, public_key_pem=pub, issuer="deerflow", audience="deerflow-api")
    assert back.tid is None


def test_expired_token_raises(rsa_keys):
    priv, pub = rsa_keys
    now = int(time.time())
    claims = _claims(iat=now - 2000, exp=now - 1000)
    token = issue_access_token(claims, private_key_pem=priv)
    with pytest.raises(TokenExpiredError):
        verify_access_token(token, public_key_pem=pub, issuer="deerflow", audience="deerflow-api")


def test_wrong_issuer_raises(rsa_keys):
    priv, pub = rsa_keys
    token = issue_access_token(_claims(iss="attacker"), private_key_pem=priv)
    with pytest.raises(InvalidIssuerError):
        verify_access_token(token, public_key_pem=pub, issuer="deerflow", audience="deerflow-api")


def test_wrong_audience_raises(rsa_keys):
    priv, pub = rsa_keys
    token = issue_access_token(_claims(aud="other-api"), private_key_pem=priv)
    with pytest.raises(InvalidAudienceError):
        verify_access_token(token, public_key_pem=pub, issuer="deerflow", audience="deerflow-api")


def test_wrong_signature_raises(rsa_keys):
    priv, _ = rsa_keys
    # Generate a *different* keypair; use its public key for verification.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    other_pub = (
        other.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    token = issue_access_token(_claims(), private_key_pem=priv)
    with pytest.raises(InvalidSignatureError):
        verify_access_token(token, public_key_pem=other_pub, issuer="deerflow", audience="deerflow-api")


def test_tampered_payload_raises(rsa_keys):
    priv, pub = rsa_keys
    token = issue_access_token(_claims(), private_key_pem=priv)
    h, p, s = token.split(".")
    # Flip one byte in the payload (after decoding).
    raw = base64.urlsafe_b64decode(p + "==")
    tampered = raw.replace(b"u@example.com", b"x@example.com")
    new_p = base64.urlsafe_b64encode(tampered).decode().rstrip("=")
    new_token = ".".join([h, new_p, s])
    with pytest.raises(InvalidSignatureError):
        verify_access_token(new_token, public_key_pem=pub, issuer="deerflow", audience="deerflow-api")


def test_refresh_token_shape_and_uniqueness():
    t1 = generate_refresh_token()
    t2 = generate_refresh_token()
    assert t1 != t2
    # 64 bytes → urlsafe-b64 without padding → 86 chars
    assert 85 <= len(t1) <= 90
    # decodable as urlsafe base64 (add padding back)
    padded = t1 + "=" * (-len(t1) % 4)
    raw = base64.urlsafe_b64decode(padded)
    assert len(raw) == 64


def test_ensure_rsa_keypair_generates_when_missing(tmp_path):
    priv_path = tmp_path / "jwt_private.pem"
    pub_path = tmp_path / "jwt_public.pem"
    priv, pub = ensure_rsa_keypair(str(priv_path), str(pub_path))
    assert priv_path.exists() and pub_path.exists()
    assert "BEGIN PRIVATE KEY" in priv or "BEGIN RSA PRIVATE KEY" in priv
    assert "BEGIN PUBLIC KEY" in pub
    # 0600 on private key
    mode = priv_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_ensure_rsa_keypair_idempotent(tmp_path):
    priv_path = tmp_path / "jwt_private.pem"
    pub_path = tmp_path / "jwt_public.pem"
    p1, u1 = ensure_rsa_keypair(str(priv_path), str(pub_path))
    p2, u2 = ensure_rsa_keypair(str(priv_path), str(pub_path))
    assert p1 == p2 and u1 == u2
