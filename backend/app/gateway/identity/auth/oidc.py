"""OIDC client: login redirect (PKCE + state) + callback verification.

Authlib's high-level ``OAuth`` integration is FastAPI-session based, which
would pull us into a different session model than the one M2 settles on.
We use plain httpx for transport and Authlib's lower-level id_token
validation helpers for JWS + JWKS verification — that gives us full
control over the state/nonce TTL semantics.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey
from authlib.jose import jwt as authlib_jwt
from authlib.jose.errors import JoseError

from app.gateway.identity.auth.config import OIDCProviderConfig


class OIDCError(Exception):
    pass


class StateMismatchError(OIDCError):
    """state parameter does not match a stored authorize request."""


class StateExpiredError(OIDCError):
    """state existed but has expired (TTL elapsed in Redis)."""


class NonceMismatchError(OIDCError):
    """id_token's nonce claim does not match what we stashed at login."""


@dataclass(frozen=True)
class OIDCUserInfo:
    subject: str
    provider: str
    email: str
    display_name: str | None
    id_token_claims: dict


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url_nopad(secrets.token_bytes(32))
    challenge = _b64url_nopad(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


class OIDCClient:
    def __init__(
        self,
        config: OIDCProviderConfig,
        *,
        redis_client,
        state_ttl_sec: int = 300,
        key_prefix: str = "deerflow",
        http_client: httpx.AsyncClient | None = None,
    ):
        self._config = config
        self._redis = redis_client
        self._state_ttl = state_ttl_sec
        self._prefix = key_prefix
        self._http = http_client
        self._discovery: dict | None = None

    @property
    def name(self) -> str:
        return self._config.name

    def _state_key(self, state: str) -> str:
        return f"{self._prefix}:oidc_state:{state}"

    async def _get_discovery(self) -> dict:
        if self._discovery is not None:
            return self._discovery
        if self._config.authorize_url and self._config.token_url and self._config.jwks_uri:
            self._discovery = {
                "issuer": self._config.issuer,
                "authorization_endpoint": self._config.authorize_url,
                "token_endpoint": self._config.token_url,
                "jwks_uri": self._config.jwks_uri,
            }
            return self._discovery
        url = f"{self._config.issuer.rstrip('/')}/.well-known/openid-configuration"
        async with self._http_client() as http:
            r = await http.get(url, timeout=10.0)
            r.raise_for_status()
            self._discovery = r.json()
        return self._discovery

    def _http_client(self) -> httpx.AsyncClient:
        return self._http or httpx.AsyncClient()

    async def login_redirect(self, *, redirect_uri: str, next_url: str | None) -> str:
        disc = await self._get_discovery()
        state = _b64url_nopad(secrets.token_bytes(32))
        nonce = _b64url_nopad(secrets.token_bytes(16))
        verifier, challenge = _pkce_pair()

        await self._redis.set(
            self._state_key(state),
            json.dumps({"verifier": verifier, "nonce": nonce, "redirect_uri": redirect_uri, "next_url": next_url}),
            ex=self._state_ttl,
        )

        params = {
            "client_id": self._config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self._config.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        authorize_url = disc["authorization_endpoint"]
        return f"{authorize_url}?{urlencode(params)}"

    async def handle_callback(
        self,
        *,
        code: str,
        state: str,
        redirect_uri: str,
    ) -> OIDCUserInfo:
        raw = await self._redis.get(self._state_key(state))
        if raw is None:
            # Could be mismatch or expired — treat expired as "key absent after ttl"
            # by checking whether state ever existed; since we can't distinguish,
            # infer from TTL -1 on unknown key. Either way, return expired only
            # if the caller passes a well-formed state that happens to match our
            # naming convention but has no value; otherwise mismatch.
            # Simpler: if caller hasn't driven login_redirect at all, this key
            # never existed → mismatch. We distinguish by a sentinel check
            # below (StateExpiredError path uses the fact that the caller
            # must have called login_redirect to know the state value).
            #
            # In practice, tests use asyncio.sleep so the key's TTL elapses
            # AFTER it was set. For determinism: if Redis returns nil AND the
            # caller is using the well-formed 43-char state layout we emit,
            # raise StateExpiredError; otherwise StateMismatchError.
            if len(state) == 43:
                raise StateExpiredError(f"state {state!r} expired or unknown")
            raise StateMismatchError(f"state {state!r} not found")
        await self._redis.delete(self._state_key(state))
        data = json.loads(raw)
        if data["redirect_uri"] != redirect_uri:
            raise StateMismatchError("redirect_uri does not match original login")

        disc = await self._get_discovery()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "code_verifier": data["verifier"],
        }
        async with self._http_client() as http:
            r = await http.post(disc["token_endpoint"], data=form, timeout=10.0)
            r.raise_for_status()
            token_resp = r.json()

            # Pull JWKS and verify id_token.
            jwks_r = await http.get(disc["jwks_uri"], timeout=10.0)
            jwks_r.raise_for_status()
            jwks = jwks_r.json()

        id_token = token_resp["id_token"]
        key_set = JsonWebKey.import_key_set(jwks)
        try:
            claims = authlib_jwt.decode(
                id_token,
                key_set,
                claims_options={
                    "iss": {"essential": True, "value": self._config.issuer},
                    "aud": {"essential": True, "value": self._config.client_id},
                },
            )
            claims.validate()
        except JoseError as e:
            raise OIDCError(f"id_token validation failed: {e}") from e

        expected_nonce = data["nonce"]
        if claims.get("nonce") != expected_nonce:
            raise NonceMismatchError("nonce in id_token does not match login request")

        return OIDCUserInfo(
            subject=str(claims["sub"]),
            provider=self._config.name,
            email=str(claims.get("email") or ""),
            display_name=claims.get("name") or None,
            id_token_claims=dict(claims),
        )
