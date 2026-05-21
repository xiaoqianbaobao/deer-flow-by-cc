"""Tests for OIDC provider config loader."""

from pathlib import Path

import pytest

from app.gateway.identity.auth.config import OIDCProviderConfig, load_oidc_providers


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "identity.yaml"
    p.write_text(content)
    return p


def test_missing_file_returns_empty():
    assert load_oidc_providers("/nonexistent/path/identity.yaml") == {}


def test_none_path_returns_empty():
    assert load_oidc_providers(None) == {}


def test_empty_oidc_section(tmp_path):
    p = _write_yaml(tmp_path, "oidc:\n  providers: {}\n")
    assert load_oidc_providers(str(p)) == {}


def test_file_without_oidc_section(tmp_path):
    p = _write_yaml(tmp_path, "something_else: 1\n")
    assert load_oidc_providers(str(p)) == {}


def test_single_provider(tmp_path):
    p = _write_yaml(
        tmp_path,
        """
oidc:
  providers:
    okta:
      issuer: https://example.okta.com
      client_id: abc
      client_secret: shh
""",
    )
    out = load_oidc_providers(str(p))
    assert set(out) == {"okta"}
    cfg = out["okta"]
    assert isinstance(cfg, OIDCProviderConfig)
    assert cfg.name == "okta"
    assert cfg.issuer == "https://example.okta.com"
    assert cfg.client_id == "abc"
    assert cfg.client_secret == "shh"
    assert cfg.scopes == ["openid", "profile", "email"]


def test_multiple_providers(tmp_path):
    p = _write_yaml(
        tmp_path,
        """
oidc:
  providers:
    okta:
      issuer: https://x.okta.com
      client_id: a
      client_secret: b
    keycloak:
      issuer: https://kc
      client_id: c
      client_secret: d
      scopes: [openid, email]
""",
    )
    out = load_oidc_providers(str(p))
    assert set(out) == {"okta", "keycloak"}
    assert out["keycloak"].scopes == ["openid", "email"]


def test_env_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("OKTA_CLIENT_ID", "env-id")
    monkeypatch.setenv("OKTA_CLIENT_SECRET", "env-secret")
    p = _write_yaml(
        tmp_path,
        """
oidc:
  providers:
    okta:
      issuer: https://x.okta.com
      client_id: $OKTA_CLIENT_ID
      client_secret: $OKTA_CLIENT_SECRET
""",
    )
    out = load_oidc_providers(str(p))
    assert out["okta"].client_id == "env-id"
    assert out["okta"].client_secret == "env-secret"


def test_env_substitution_missing_var_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    p = _write_yaml(
        tmp_path,
        """
oidc:
  providers:
    okta:
      issuer: https://x.okta.com
      client_id: $MISSING_VAR
      client_secret: s
""",
    )
    with pytest.raises(ValueError, match="MISSING_VAR"):
        load_oidc_providers(str(p))


def test_missing_required_field_raises(tmp_path):
    p = _write_yaml(
        tmp_path,
        """
oidc:
  providers:
    okta:
      client_id: a
      client_secret: b
""",
    )
    with pytest.raises(ValueError, match="issuer"):
        load_oidc_providers(str(p))


def test_unknown_keys_ignored(tmp_path, caplog):
    p = _write_yaml(
        tmp_path,
        """
oidc:
  providers:
    okta:
      issuer: https://x
      client_id: a
      client_secret: b
      bogus: 42
""",
    )
    with caplog.at_level("WARNING"):
        out = load_oidc_providers(str(p))
    assert out["okta"].issuer == "https://x"
    assert any("bogus" in rec.message for rec in caplog.records)


def test_optional_endpoints(tmp_path):
    p = _write_yaml(
        tmp_path,
        """
oidc:
  providers:
    custom:
      issuer: https://x
      client_id: a
      client_secret: b
      authorize_url: https://x/authorize
      token_url: https://x/token
      jwks_uri: https://x/jwks
""",
    )
    out = load_oidc_providers(str(p))
    cfg = out["custom"]
    assert cfg.authorize_url == "https://x/authorize"
    assert cfg.token_url == "https://x/token"
    assert cfg.jwks_uri == "https://x/jwks"
