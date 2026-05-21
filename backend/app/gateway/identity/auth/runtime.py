"""Runtime handles shared by middleware + routers.

Holds the bootstrapped JWT keys, session store, OIDC clients and lockout
instance. Populated by ``app.gateway.app._init_identity_subsystem`` and
read via ``get_runtime()``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AuthRuntime:
    jwt_private_key_pem: str
    jwt_public_key_pem: str
    issuer: str
    audience: str
    access_ttl_sec: int
    refresh_ttl_sec: int
    cookie_name: str
    cookie_secure: bool
    oidc_clients: dict  # provider name → OIDCClient
    session_store: object  # SessionStore
    lockout: object  # LoginLockout
    redis_client: object
    session_maker: object
    auto_provision: bool


_runtime: AuthRuntime | None = None


def set_runtime(rt: AuthRuntime) -> None:
    global _runtime
    _runtime = rt


def get_runtime() -> AuthRuntime:
    if _runtime is None:
        raise RuntimeError("auth runtime not initialized (ENABLE_IDENTITY=false?)")
    return _runtime


def clear_runtime() -> None:
    global _runtime
    _runtime = None
