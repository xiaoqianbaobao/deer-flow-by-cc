"""Internal JWT signing/verification (RS256) and refresh-token generation.

Access tokens are RS256-signed JWTs containing the full identity claims.
Refresh tokens are opaque random strings — their hash is stored in Redis,
never in the JWT.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt as jose_jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError, JWTError

ALGORITHM = "RS256"


class JWTVerificationError(Exception):
    """Base class for JWT verification failures."""


class TokenExpiredError(JWTVerificationError):
    """Raised when the token's exp has passed."""


class InvalidIssuerError(JWTVerificationError):
    """Raised when iss does not match the expected issuer."""


class InvalidAudienceError(JWTVerificationError):
    """Raised when aud does not match the expected audience."""


class InvalidSignatureError(JWTVerificationError):
    """Raised when the signature is invalid or the token is otherwise malformed."""


@dataclass(frozen=True)
class AccessTokenClaims:
    """Claims encoded in a DeerFlow internal access token."""

    sub: str
    email: str
    tid: int | None
    wids: list[int]
    permissions: list[str]
    roles: dict
    sid: str
    iat: int
    exp: int
    iss: str
    aud: str


def issue_access_token(
    claims: AccessTokenClaims,
    *,
    private_key_pem: str,
    algorithm: str = ALGORITHM,
) -> str:
    """Sign claims and return the compact JWS token."""
    payload = asdict(claims)
    return jose_jwt.encode(payload, private_key_pem, algorithm=algorithm)


def verify_access_token(
    token: str,
    *,
    public_key_pem: str,
    issuer: str,
    audience: str,
    algorithms: tuple[str, ...] = (ALGORITHM,),
) -> AccessTokenClaims:
    """Verify signature + iss + aud + exp, returning the parsed claims.

    Raises one of the subclass-specific ``JWTVerificationError`` types so
    callers (middleware, refresh route) can react differently per case.
    """
    try:
        payload = jose_jwt.decode(
            token,
            public_key_pem,
            algorithms=list(algorithms),
            issuer=issuer,
            audience=audience,
        )
    except ExpiredSignatureError as e:
        raise TokenExpiredError(str(e)) from e
    except JWTClaimsError as e:
        msg = str(e).lower()
        if "issuer" in msg:
            raise InvalidIssuerError(str(e)) from e
        if "audience" in msg:
            raise InvalidAudienceError(str(e)) from e
        raise InvalidSignatureError(str(e)) from e
    except JWTError as e:
        raise InvalidSignatureError(str(e)) from e

    return AccessTokenClaims(
        sub=payload["sub"],
        email=payload["email"],
        tid=payload.get("tid"),
        wids=list(payload.get("wids", [])),
        permissions=list(payload.get("permissions", [])),
        roles=dict(payload.get("roles", {})),
        sid=payload["sid"],
        iat=int(payload["iat"]),
        exp=int(payload["exp"]),
        iss=payload["iss"],
        aud=payload["aud"],
    )


def decode_claims_insecure(token: str) -> dict:
    """Decode JWT claims *without* verifying signature or exp.

    Used by the refresh endpoint to pull ``sid`` out of an expired but
    otherwise intact access token. Never trust the returned data for
    authorization — only use it to look up Redis state that is itself
    authoritative.
    """
    try:
        return jose_jwt.get_unverified_claims(token)
    except JWTError as e:
        raise InvalidSignatureError(str(e)) from e


def generate_refresh_token() -> str:
    """64 random bytes, url-safe base64 encoded (no padding)."""
    raw = secrets.token_bytes(64)
    import base64

    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def ensure_rsa_keypair(private_path: str, public_path: str, *, key_size: int = 2048) -> tuple[str, str]:
    """Load or generate an RS256 keypair.

    If both files exist, return their contents unchanged. Otherwise generate
    a fresh RSA keypair, write the private key to ``private_path`` with
    mode 0600 and the public key to ``public_path`` with mode 0644. Parent
    directories are created if needed.
    """
    priv_p = Path(private_path)
    pub_p = Path(public_path)
    if priv_p.exists() and pub_p.exists():
        return priv_p.read_text(), pub_p.read_text()

    priv_p.parent.mkdir(parents=True, exist_ok=True)
    pub_p.parent.mkdir(parents=True, exist_ok=True)

    priv = rsa.generate_private_key(public_exponent=65537, key_size=key_size, backend=default_backend())
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

    priv_p.write_text(priv_pem)
    pub_p.write_text(pub_pem)
    os.chmod(priv_p, 0o600)
    os.chmod(pub_p, 0o644)
    return priv_pem, pub_pem
