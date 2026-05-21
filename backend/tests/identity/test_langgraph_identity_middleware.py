"""LangGraph-side IdentityMiddleware tests (M5 Task 2).

Exercise the middleware's ``before_agent`` hook directly (no full agent
runtime) because the per-hook unit is simple and what we really want to
assert is the header → state contract.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from deerflow.agents.middlewares.identity_middleware import IdentityMiddleware
from deerflow.identity_propagation import (
    InvalidSignatureError,
    StaleTimestampError,
    sign_headers,
)

KEY = b"integration-test-signing-key"


def _valid_headers(*, user_id=42, tenant_id=7, workspace_id=3, permissions=("thread:read", "thread:write"), ts=None) -> dict[str, str]:
    return sign_headers(
        user_id=user_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        permissions=permissions,
        session_id="sess_abc",
        key=KEY,
        ts=ts,
    )


def _run_before_agent(mw: IdentityMiddleware, headers: dict[str, str], state: dict | None = None) -> dict | None:
    """Invoke ``before_agent`` with a patched ``get_config`` returning *headers*.

    The middleware reads configurable["headers"] via
    ``langgraph.config.get_config`` which only resolves inside a runnable
    context. We patch the symbol imported into the middleware module.
    """
    state = state if state is not None else {}
    fake_cfg = {"configurable": {"headers": headers}}
    with patch("deerflow.agents.middlewares.identity_middleware.get_config", return_value=fake_cfg):
        return mw.before_agent(state, runtime=None)  # runtime is unused by the hook


def test_valid_headers_populate_state_identity():
    mw = IdentityMiddleware(signing_key=KEY)
    out = _run_before_agent(mw, _valid_headers())

    assert out is not None
    identity = out["identity"]
    assert identity.user_id == 42
    assert identity.tenant_id == 7
    assert identity.workspace_id == 3
    assert identity.permissions == frozenset({"thread:read", "thread:write"})
    assert identity.session_id == "sess_abc"


def test_missing_headers_no_error_and_no_identity():
    """Flag-off: no headers sent → middleware is a silent no-op."""
    mw = IdentityMiddleware(signing_key=KEY)
    out = _run_before_agent(mw, headers={})
    assert out is None


def test_tampered_signature_raises():
    mw = IdentityMiddleware(signing_key=KEY)
    headers = _valid_headers()
    headers["X-Deerflow-User-Id"] = "999"

    with pytest.raises(InvalidSignatureError):
        _run_before_agent(mw, headers)


def test_stale_timestamp_raises():
    mw = IdentityMiddleware(signing_key=KEY, skew_sec=60)
    stale = int(time.time()) - 9_999
    with pytest.raises(StaleTimestampError):
        _run_before_agent(mw, _valid_headers(ts=stale))


def test_empty_headers_mapping_noop():
    mw = IdentityMiddleware(signing_key=KEY)
    # configurable["headers"] absent entirely
    with patch("deerflow.agents.middlewares.identity_middleware.get_config", return_value={"configurable": {}}):
        assert mw.before_agent({}, runtime=None) is None


def test_state_already_populated_not_overwritten():
    """Subagent inheritance: parent identity is already in state."""
    mw = IdentityMiddleware(signing_key=KEY)
    parent_identity = object()
    out = _run_before_agent(mw, _valid_headers(), state={"identity": parent_identity})
    assert out is None  # no overwrite — middleware treats populated state as trusted


def test_non_dict_headers_ignored():
    """Defensive: if configurable["headers"] is malformed (e.g. a list), ignore gracefully."""
    mw = IdentityMiddleware(signing_key=KEY)
    with patch("deerflow.agents.middlewares.identity_middleware.get_config", return_value={"configurable": {"headers": ["not", "a", "dict"]}}):
        assert mw.before_agent({}, runtime=None) is None


def test_get_config_outside_runnable_context_handled():
    """Unit-test / direct instantiation path: get_config() raises → no-op."""
    mw = IdentityMiddleware(signing_key=KEY)
    with patch("deerflow.agents.middlewares.identity_middleware.get_config", side_effect=RuntimeError("no context")):
        assert mw.before_agent({}, runtime=None) is None


def test_registered_at_position_zero_when_signing_key_set(monkeypatch):
    """When DEERFLOW_INTERNAL_SIGNING_KEY is set, IdentityMiddleware is first in the lead runtime chain."""
    monkeypatch.setenv("DEERFLOW_INTERNAL_SIGNING_KEY", "dummy-key")

    from deerflow.agents.middlewares.tool_error_handling_middleware import build_lead_runtime_middlewares

    chain = build_lead_runtime_middlewares(lazy_init=True)
    assert chain, "expected non-empty middleware chain"
    assert isinstance(chain[0], IdentityMiddleware)


def test_not_registered_when_signing_key_absent(monkeypatch):
    """Flag-off path: no signing key → IdentityMiddleware is *not* registered."""
    monkeypatch.delenv("DEERFLOW_INTERNAL_SIGNING_KEY", raising=False)

    from deerflow.agents.middlewares.tool_error_handling_middleware import build_lead_runtime_middlewares

    chain = build_lead_runtime_middlewares(lazy_init=True)
    assert all(not isinstance(mw, IdentityMiddleware) for mw in chain)


def test_uploads_middleware_still_follows_thread_data_with_identity(monkeypatch):
    """Regression: insert-after-ThreadData logic survives identity prefix."""
    monkeypatch.setenv("DEERFLOW_INTERNAL_SIGNING_KEY", "dummy-key")

    from deerflow.agents.middlewares.thread_data_middleware import ThreadDataMiddleware
    from deerflow.agents.middlewares.tool_error_handling_middleware import build_lead_runtime_middlewares
    from deerflow.agents.middlewares.uploads_middleware import UploadsMiddleware

    chain = build_lead_runtime_middlewares(lazy_init=True)
    tdm_idx = next(i for i, mw in enumerate(chain) if isinstance(mw, ThreadDataMiddleware))
    uploads_idx = next(i for i, mw in enumerate(chain) if isinstance(mw, UploadsMiddleware))
    assert uploads_idx == tdm_idx + 1
