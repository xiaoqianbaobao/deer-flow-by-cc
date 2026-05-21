"""Gateway outbound header injection tests (M5 Task 5).

Exercise :func:`app.gateway.services._inject_identity_headers` — the
choke point where the Gateway stamps HMAC-signed identity headers into
``configurable["headers"]`` for the LangGraph runtime to consume.

We don't boot the full FastAPI app here; a fake Request with the
relevant attributes is sufficient to cover the branches.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.gateway.identity.auth.identity import Identity
from app.gateway.identity.propagation import (
    HEADER_SIG,
    HEADER_USER_ID,
    HEADER_WORKSPACE_ID,
    verify_identity_headers,
)
from app.gateway.services import _extract_workspace_id_for_run, _inject_identity_headers

KEY = "m5-test-signing-key"


def _settings(*, enabled=True, key=KEY):
    return SimpleNamespace(enabled=enabled, internal_signing_key=key)


def _request(identity=None, path_params=None):
    return SimpleNamespace(
        state=SimpleNamespace(identity=identity),
        path_params=path_params or {},
    )


def _make_identity(**overrides) -> Identity:
    defaults = dict(
        token_type="jwt",
        user_id=42,
        email="user@example.com",
        tenant_id=7,
        workspace_ids=(3, 9),
        permissions=frozenset({"thread:read", "thread:write"}),
        roles={},
        session_id="sess",
        ip=None,
    )
    defaults.update(overrides)
    return Identity(**defaults)


def test_injects_signed_headers_for_authenticated_caller():
    config: dict = {"configurable": {}}
    req = _request(identity=_make_identity())

    with patch("app.gateway.identity.settings.get_identity_settings", return_value=_settings()):
        _inject_identity_headers(config, req)

    headers = config["configurable"]["headers"]
    assert HEADER_USER_ID in headers
    assert HEADER_SIG in headers

    restored = verify_identity_headers(headers, key=KEY)
    assert restored.user_id == 42
    assert restored.tenant_id == 7


def test_noop_when_identity_subsystem_disabled():
    config: dict = {"configurable": {}}
    req = _request(identity=_make_identity())

    with patch("app.gateway.identity.settings.get_identity_settings", return_value=_settings(enabled=False)):
        _inject_identity_headers(config, req)

    assert "headers" not in config.get("configurable", {})


def test_noop_when_signing_key_missing():
    config: dict = {"configurable": {}}
    req = _request(identity=_make_identity())

    with patch("app.gateway.identity.settings.get_identity_settings", return_value=_settings(key=None)):
        _inject_identity_headers(config, req)

    assert "headers" not in config.get("configurable", {})


def test_noop_for_anonymous_caller():
    config: dict = {"configurable": {}}
    req = _request(identity=Identity.anonymous())

    with patch("app.gateway.identity.settings.get_identity_settings", return_value=_settings()):
        _inject_identity_headers(config, req)

    assert "headers" not in config.get("configurable", {})


def test_noop_when_request_state_has_no_identity_attr():
    config: dict = {"configurable": {}}
    req = _request(identity=None)

    with patch("app.gateway.identity.settings.get_identity_settings", return_value=_settings()):
        _inject_identity_headers(config, req)

    assert "headers" not in config.get("configurable", {})


def test_active_workspace_from_path_param_wins_over_identity_membership():
    config: dict = {"configurable": {}}
    req = _request(
        identity=_make_identity(workspace_ids=(3, 9)),
        path_params={"wid": "9"},
    )

    with patch("app.gateway.identity.settings.get_identity_settings", return_value=_settings()):
        _inject_identity_headers(config, req)

    headers = config["configurable"]["headers"]
    assert headers[HEADER_WORKSPACE_ID] == "9"


def test_active_workspace_from_explicit_configurable_wins_over_path():
    config: dict = {"configurable": {"workspace_id": 3}}
    req = _request(
        identity=_make_identity(workspace_ids=(3, 9)),
        path_params={"wid": "9"},
    )

    with patch("app.gateway.identity.settings.get_identity_settings", return_value=_settings()):
        _inject_identity_headers(config, req)

    assert config["configurable"]["headers"][HEADER_WORKSPACE_ID] == "3"


def test_active_workspace_falls_back_to_first_membership():
    config: dict = {"configurable": {}}
    req = _request(identity=_make_identity(workspace_ids=(42,)))

    with patch("app.gateway.identity.settings.get_identity_settings", return_value=_settings()):
        _inject_identity_headers(config, req)

    assert config["configurable"]["headers"][HEADER_WORKSPACE_ID] == "42"


def test_active_workspace_omitted_when_unresolvable():
    config: dict = {"configurable": {}}
    req = _request(identity=_make_identity(workspace_ids=()))

    with patch("app.gateway.identity.settings.get_identity_settings", return_value=_settings()):
        _inject_identity_headers(config, req)

    headers = config["configurable"]["headers"]
    assert HEADER_WORKSPACE_ID not in headers


def test_headers_merge_with_existing_keys():
    """Pre-existing headers (unusual but valid) should not be trampled."""
    config: dict = {"configurable": {"headers": {"X-Custom": "keepme"}}}
    req = _request(identity=_make_identity())

    with patch("app.gateway.identity.settings.get_identity_settings", return_value=_settings()):
        _inject_identity_headers(config, req)

    headers = config["configurable"]["headers"]
    assert headers["X-Custom"] == "keepme"
    assert HEADER_USER_ID in headers


def test_round_trip_through_langgraph_middleware():
    """End-to-end: sign on Gateway side, verify through the harness middleware."""
    from deerflow.agents.middlewares.identity_middleware import IdentityMiddleware

    config: dict = {"configurable": {}}
    req = _request(identity=_make_identity())

    with patch("app.gateway.identity.settings.get_identity_settings", return_value=_settings()):
        _inject_identity_headers(config, req)

    mw = IdentityMiddleware(signing_key=KEY)
    with patch("deerflow.agents.middlewares.identity_middleware.get_config", return_value=config):
        result = mw.before_agent({}, runtime=None)

    assert result is not None
    identity = result["identity"]
    assert identity.user_id == 42
    assert identity.tenant_id == 7
    assert identity.permissions == frozenset({"thread:read", "thread:write"})


def test_extract_workspace_id_priority_order():
    """Unit test for the resolver, independent of the signer."""
    req_with_explicit = _request(
        identity=_make_identity(workspace_ids=(1,)),
        path_params={"wid": "5"},
    )
    assert _extract_workspace_id_for_run(req_with_explicit, {"configurable": {"workspace_id": 7}}) == 7

    req_path_only = _request(
        identity=_make_identity(workspace_ids=(1,)),
        path_params={"workspace_id": "5"},
    )
    assert _extract_workspace_id_for_run(req_path_only, {"configurable": {}}) == 5

    req_identity_only = _request(identity=_make_identity(workspace_ids=(1,)))
    assert _extract_workspace_id_for_run(req_identity_only, {"configurable": {}}) == 1

    req_none = _request(identity=_make_identity(workspace_ids=()))
    assert _extract_workspace_id_for_run(req_none, {"configurable": {}}) is None
