"""OIDC provider configuration loader.

Loads a YAML config file of shape::

    oidc:
      providers:
        okta:
          issuer: https://example.okta.com
          client_id: $OKTA_CLIENT_ID
          client_secret: $OKTA_CLIENT_SECRET
          scopes: [openid, profile, email]
          authorize_url: ...   # optional (usually discovered)
          token_url: ...       # optional
          jwks_uri: ...        # optional

``$VAR`` references are resolved against the process environment.
Missing required fields raise ``ValueError``; unknown keys are ignored
with a warning.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_SCOPES = ["openid", "profile", "email"]
_KNOWN_KEYS = {
    "issuer",
    "client_id",
    "client_secret",
    "scopes",
    "authorize_url",
    "token_url",
    "jwks_uri",
}


@dataclass(frozen=True)
class OIDCProviderConfig:
    """Immutable per-provider OIDC config."""

    name: str
    issuer: str
    client_id: str
    client_secret: str
    scopes: list[str] = field(default_factory=lambda: list(_DEFAULT_SCOPES))
    authorize_url: str | None = None
    token_url: str | None = None
    jwks_uri: str | None = None


def _resolve_env(value: object) -> object:
    """If value is a '$VAR' string, resolve from env; raise if missing."""
    if not isinstance(value, str):
        return value
    if not value.startswith("$"):
        return value
    var = value[1:]
    resolved = os.environ.get(var)
    if resolved is None:
        raise ValueError(f"environment variable {var!r} referenced in identity config is not set")
    return resolved


def _build_provider(name: str, raw: dict) -> OIDCProviderConfig:
    missing = [k for k in ("issuer", "client_id", "client_secret") if k not in raw]
    if missing:
        raise ValueError(f"oidc.providers.{name}: missing required field(s): {missing}")

    for key in list(raw):
        if key not in _KNOWN_KEYS:
            logger.warning("oidc.providers.%s: unknown key %r (ignored)", name, key)

    issuer = _resolve_env(raw["issuer"])
    client_id = _resolve_env(raw["client_id"])
    client_secret = _resolve_env(raw["client_secret"])
    scopes = raw.get("scopes") or list(_DEFAULT_SCOPES)
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise ValueError(f"oidc.providers.{name}: scopes must be a list of strings")
    authorize_url = _resolve_env(raw["authorize_url"]) if raw.get("authorize_url") else None
    token_url = _resolve_env(raw["token_url"]) if raw.get("token_url") else None
    jwks_uri = _resolve_env(raw["jwks_uri"]) if raw.get("jwks_uri") else None

    return OIDCProviderConfig(
        name=name,
        issuer=str(issuer),
        client_id=str(client_id),
        client_secret=str(client_secret),
        scopes=list(scopes),
        authorize_url=authorize_url if authorize_url is None else str(authorize_url),
        token_url=token_url if token_url is None else str(token_url),
        jwks_uri=jwks_uri if jwks_uri is None else str(jwks_uri),
    )


def load_oidc_providers(path: str | None) -> dict[str, OIDCProviderConfig]:
    """Load OIDC providers from a YAML file.

    Returns empty dict when ``path`` is None, the file is absent, or there
    is no ``oidc.providers`` section.
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    providers_raw = (data.get("oidc") or {}).get("providers") or {}
    if not isinstance(providers_raw, dict):
        raise ValueError("oidc.providers must be a mapping")
    out: dict[str, OIDCProviderConfig] = {}
    for name, raw in providers_raw.items():
        if not isinstance(raw, dict):
            raise ValueError(f"oidc.providers.{name} must be a mapping")
        out[name] = _build_provider(name, raw)
    return out
