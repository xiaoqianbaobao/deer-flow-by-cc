"""API token creation, verification, and revocation.

Token format: ``dft_{prefix}_{secret}`` where prefix is 6 base32 chars
(indexable) and secret is 32 base32 chars (never stored; bcrypt-hashed).

Verification flow:

1. Parse prefix from plaintext.
2. Fetch all ApiToken rows sharing that prefix (partial unique index
   guarantees a small set even in the rare collision case).
3. For each, ``bcrypt.verify(plaintext, row.token_hash)``; on match, check
   expiry/revoked, build an ``Identity`` from scopes, and update last-used
   metadata.

Scopes become ``Identity.permissions`` for M3 enforcement — this milestone
only wires them through; M3 decorator does the actual allow/deny check.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from passlib.hash import bcrypt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.identity.auth.identity import Identity
from app.gateway.identity.models.token import ApiToken
from app.gateway.identity.settings import get_identity_settings

_PREFIX_LEN = 6
_SECRET_LEN = 32


@dataclass
class CreatedToken:
    """Return value of ``create_api_token`` — the only time plaintext is visible."""

    token_id: int
    plaintext: str
    prefix: str


def _rand_base32(n_chars: int) -> str:
    """Return ``n_chars`` base32 characters (A–Z, 2–7), no padding."""
    # base32 emits 8 chars per 5 bytes; compute enough input bytes.
    raw_bytes = (n_chars * 5 + 7) // 8
    out = base64.b32encode(secrets.token_bytes(raw_bytes)).decode().rstrip("=")
    return out[:n_chars]


def _generate_token(*, bcrypt_cost: int = 12) -> tuple[str, str, str]:
    """Return ``(plaintext, prefix, bcrypt_hash)``."""
    prefix = _rand_base32(_PREFIX_LEN)
    secret = _rand_base32(_SECRET_LEN)
    plaintext = f"dft_{prefix}_{secret}"
    hashed = bcrypt.using(rounds=bcrypt_cost).hash(plaintext)
    return plaintext, prefix, hashed


async def create_api_token(
    session: AsyncSession,
    *,
    user_id: int,
    tenant_id: int,
    workspace_id: int | None,
    name: str,
    scopes: list[str],
    expires_at: datetime | None,
    created_by: int,
) -> CreatedToken:
    """Persist a new API token and return plaintext exactly once."""
    cost = get_identity_settings().bcrypt_cost
    plaintext, prefix, token_hash = _generate_token(bcrypt_cost=cost)
    row = ApiToken(
        tenant_id=tenant_id,
        user_id=user_id,
        workspace_id=workspace_id,
        name=name,
        prefix=prefix,
        token_hash=token_hash,
        scopes=list(scopes),
        expires_at=expires_at,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    await session.commit()
    return CreatedToken(token_id=row.id, plaintext=plaintext, prefix=prefix)


async def verify_api_token(
    session: AsyncSession,
    plaintext: str,
    *,
    client_ip: str | None = None,
) -> Identity | None:
    """Validate ``plaintext`` against the token table; return an Identity or None.

    Returns ``None`` for malformed input, unknown prefix, wrong secret,
    expired, or revoked tokens.
    """
    parts = plaintext.split("_") if plaintext else []
    if len(parts) != 3 or parts[0] != "dft":
        return None
    prefix = parts[1]
    if len(prefix) != _PREFIX_LEN:
        return None

    rows = (await session.execute(select(ApiToken).where(ApiToken.prefix == prefix, ApiToken.revoked_at.is_(None)))).scalars().all()
    if not rows:
        return None

    now = datetime.now(UTC)
    for row in rows:
        if row.expires_at is not None and row.expires_at <= now:
            continue
        try:
            if not bcrypt.verify(plaintext, row.token_hash):
                continue
        except ValueError:
            continue
        # Match found. Update last-used metadata.
        await session.execute(update(ApiToken).where(ApiToken.id == row.id).values(last_used_at=now, last_used_ip=client_ip))
        return Identity(
            token_type="api_token",
            user_id=row.user_id,
            email=None,
            tenant_id=row.tenant_id,
            workspace_ids=(row.workspace_id,) if row.workspace_id else (),
            permissions=frozenset(row.scopes or ()),
            roles={},
            session_id=None,
        )
    return None


async def revoke_api_token(
    session: AsyncSession,
    *,
    token_id: int,
    by_user_id: int,
) -> None:
    """Mark the token revoked_at = now (idempotent; no-op if already revoked)."""
    await session.execute(update(ApiToken).where(ApiToken.id == token_id, ApiToken.revoked_at.is_(None)).values(revoked_at=datetime.now(UTC)))
    await session.commit()
