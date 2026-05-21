"""Global middleware that resolves every request to an ``Identity``.

Order of resolution:

1. ``Authorization: Bearer dft_...`` → ``verify_api_token``.
2. Otherwise, a bearer token (``Authorization: Bearer eyJ...``) *or* the
   session cookie: decode + verify the JWT, then check the Redis session
   is still alive.
3. No credentials, malformed headers, invalid signatures, expired tokens,
   and revoked sessions all fall through to ``Identity.anonymous()``. M2
   never returns 401 here — M3's per-route decorator will decide that
   based on required scopes.

``request.state.identity`` is set for downstream handlers. ContextVars
(``current_identity``, ``current_tenant_id``, ``current_session_id``) are
bound for the duration of the request and reset in ``finally`` so async
context leakage doesn't cross requests.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.gateway.identity.auth.api_token import verify_api_token
from app.gateway.identity.auth.identity import Identity
from app.gateway.identity.auth.jwt import (
    AccessTokenClaims,
    JWTVerificationError,
    verify_access_token,
)
from app.gateway.identity.auth.session import SessionStore
from app.gateway.identity.context import (
    current_identity,
    current_session_id,
    current_tenant_id,
)

logger = logging.getLogger(__name__)


class IdentityMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        public_key_pem: str,
        session_store: SessionStore,
        session_maker,
        issuer: str,
        audience: str,
        cookie_name: str = "deerflow_session",
    ):
        super().__init__(app)
        self._pub = public_key_pem
        self._session_store = session_store
        self._maker = session_maker
        self._iss = issuer
        self._aud = audience
        self._cookie = cookie_name

    async def dispatch(self, request: Request, call_next):
        identity = await self._resolve(request)
        client_ip = request.client.host if request.client else None
        if client_ip is not None and identity.is_authenticated and identity.ip is None:
            identity = replace(identity, ip=client_ip)
        request.state.identity = identity

        t_ident = current_identity.set(identity)
        t_tenant = current_tenant_id.set(identity.tenant_id)
        t_sid = current_session_id.set(identity.session_id)
        try:
            return await call_next(request)
        finally:
            current_identity.reset(t_ident)
            current_tenant_id.reset(t_tenant)
            current_session_id.reset(t_sid)

    async def _resolve(self, request: Request) -> Identity:
        token, kind = self._extract_token(request)
        if token is None:
            return Identity.anonymous()

        if kind == "api_token":
            return await self._resolve_api_token(request, token)
        return await self._resolve_jwt(request, token)

    def _extract_token(self, request: Request) -> tuple[str | None, str | None]:
        auth = request.headers.get("Authorization", "")
        if auth:
            parts = auth.split(None, 1)
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return None, None
            tok = parts[1].strip()
            if not tok:
                return None, None
            if tok.startswith("dft_"):
                return tok, "api_token"
            return tok, "jwt"

        cookie = request.cookies.get(self._cookie)
        if cookie:
            return cookie, "jwt"
        return None, None

    async def _resolve_api_token(self, request: Request, token: str) -> Identity:
        client_ip = request.client.host if request.client else None
        try:
            async with self._maker() as session:
                ident = await verify_api_token(session, token, client_ip=client_ip)
                await session.commit()
        except Exception:
            logger.exception("api token verification crashed")
            return Identity.anonymous()
        return ident or Identity.anonymous()

    async def _resolve_jwt(self, request: Request, token: str) -> Identity:
        try:
            claims: AccessTokenClaims = verify_access_token(
                token,
                public_key_pem=self._pub,
                issuer=self._iss,
                audience=self._aud,
            )
        except JWTVerificationError:
            return Identity.anonymous()
        except Exception:
            logger.exception("jwt verification crashed")
            return Identity.anonymous()

        # Session must still exist + be un-revoked.
        rec = await self._session_store.get(claims.sid)
        if rec is None:
            return Identity.anonymous()

        return Identity(
            token_type="jwt",
            user_id=int(claims.sub),
            email=claims.email,
            tenant_id=claims.tid,
            workspace_ids=tuple(claims.wids),
            permissions=frozenset(claims.permissions),
            roles=claims.roles,
            session_id=claims.sid,
        )
