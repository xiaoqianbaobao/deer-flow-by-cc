"""Identity-driven tool authorization tests (M5 Task 3).

Cover the whitelist default-deny policy, permission-to-tool mapping, MCP
tool metadata handling, and flag-off regression path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from deerflow.guardrails.identity_guardrail import (
    DEFAULT_MCP_PERMISSION,
    TOOL_PERMISSION_MAP,
    IdentityGuardrailMiddleware,
)


def _make_request(name: str, *, call_id: str = "call_1", state=None):
    """Build a ToolCallRequest-compatible mock.

    Real ``ToolCallRequest`` is a frozen dataclass, but the middleware
    only reads ``.tool_call`` and ``.state`` so a SimpleNamespace is
    cheaper and more readable than instantiating the real thing.
    """
    return SimpleNamespace(
        tool_call={"name": name, "args": {}, "id": call_id},
        state=state,
        runtime=None,
        tool=None,
    )


class _Identity:
    """Minimal duck-typed identity for test fixtures."""

    def __init__(self, *, user_id=42, permissions=frozenset()):
        self.user_id = user_id
        self.permissions = frozenset(permissions)


def test_identity_with_thread_write_allows_bash():
    mw = IdentityGuardrailMiddleware()
    state = {"identity": _Identity(permissions={"thread:write"})}
    req = _make_request("bash", state=state)
    handler = MagicMock(return_value="ok")

    assert mw.wrap_tool_call(req, handler) == "ok"
    handler.assert_called_once()


def test_identity_without_thread_write_denies_bash():
    mw = IdentityGuardrailMiddleware()
    state = {"identity": _Identity(permissions={"thread:read"})}
    req = _make_request("bash", state=state)
    handler = MagicMock()

    result = mw.wrap_tool_call(req, handler)
    handler.assert_not_called()
    assert result.status == "error"
    assert "Permission denied" in result.content
    assert "thread:write" in result.content
    assert result.additional_kwargs["audit_action"] == "authz.tool.denied"


def test_unknown_tool_denied_by_default():
    """Whitelist policy: tools outside the map and registry are denied."""
    mw = IdentityGuardrailMiddleware()
    state = {"identity": _Identity(permissions={"thread:write", "admin:all"})}
    req = _make_request("super_dangerous_tool", state=state)
    handler = MagicMock()

    result = mw.wrap_tool_call(req, handler)
    handler.assert_not_called()
    assert result.status == "error"
    assert result.additional_kwargs["audit_code"] == "authz.tool.unknown"


def test_flag_off_state_without_identity_falls_through():
    """Pre-M5 deployments keep working: no identity → no gate."""
    mw = IdentityGuardrailMiddleware()
    state = {}  # no identity key — legacy mode
    req = _make_request("bash", state=state)
    handler = MagicMock(return_value="ok")

    assert mw.wrap_tool_call(req, handler) == "ok"
    handler.assert_called_once()


def test_mcp_tool_with_declared_permission_honored():
    """MCP tools that declare their own ``required_permission`` win."""
    custom_tool = SimpleNamespace(required_permission="knowledge:read")
    registry = {"search_knowledge": custom_tool}
    mw = IdentityGuardrailMiddleware(tool_registry=registry)

    # Caller has the declared perm → allowed
    state_ok = {"identity": _Identity(permissions={"knowledge:read"})}
    req_ok = _make_request("search_knowledge", state=state_ok)
    handler_ok = MagicMock(return_value="ok")
    assert mw.wrap_tool_call(req_ok, handler_ok) == "ok"

    # Caller lacks the declared perm → denied
    state_bad = {"identity": _Identity(permissions={"thread:write"})}
    req_bad = _make_request("search_knowledge", state=state_bad)
    handler_bad = MagicMock()
    result = mw.wrap_tool_call(req_bad, handler_bad)
    handler_bad.assert_not_called()
    assert result.status == "error"
    assert "knowledge:read" in result.content


def test_mcp_tool_without_declaration_uses_default_mcp_permission():
    """MCP tool in registry but no declared permission → DEFAULT_MCP_PERMISSION."""
    plain_mcp_tool = SimpleNamespace()  # no required_permission attribute
    registry = {"some_mcp_tool": plain_mcp_tool}
    mw = IdentityGuardrailMiddleware(tool_registry=registry)

    # Caller with skill:invoke passes
    state_ok = {"identity": _Identity(permissions={DEFAULT_MCP_PERMISSION})}
    req_ok = _make_request("some_mcp_tool", state=state_ok)
    handler_ok = MagicMock(return_value="ok")
    assert mw.wrap_tool_call(req_ok, handler_ok) == "ok"

    # Caller without skill:invoke denied
    state_bad = {"identity": _Identity(permissions={"thread:write"})}
    req_bad = _make_request("some_mcp_tool", state=state_bad)
    handler_bad = MagicMock()
    result = mw.wrap_tool_call(req_bad, handler_bad)
    assert result.status == "error"
    assert DEFAULT_MCP_PERMISSION in result.content


def test_internal_tools_bypass_permission_check():
    """``write_todos`` is internal plumbing and must never be blocked."""
    mw = IdentityGuardrailMiddleware()
    # Even an identity with zero permissions should not be denied
    state = {"identity": _Identity(permissions=set())}
    req = _make_request("write_todos", state=state)
    handler = MagicMock(return_value="ok")

    assert mw.wrap_tool_call(req, handler) == "ok"


@pytest.mark.parametrize("tool,required", sorted(TOOL_PERMISSION_MAP.items()))
def test_permission_map_gate(tool, required):
    """Every tool in the map denies callers lacking its required tag."""
    mw = IdentityGuardrailMiddleware()
    state = {"identity": _Identity(permissions=set())}  # no permissions
    req = _make_request(tool, state=state)
    handler = MagicMock()

    result = mw.wrap_tool_call(req, handler)
    handler.assert_not_called()
    assert result.status == "error"
    assert required in result.content


def test_dict_style_identity_supported():
    """Identity carried as a dict (e.g. when serialized through config) is honored."""
    mw = IdentityGuardrailMiddleware()
    state = {"identity": {"user_id": 7, "permissions": frozenset({"thread:write"})}}
    req = _make_request("bash", state=state)
    handler = MagicMock(return_value="ok")
    assert mw.wrap_tool_call(req, handler) == "ok"


async def _arun(mw, req, handler):
    return await mw.awrap_tool_call(req, handler)


def test_async_path_denies_same_way():
    """Async entry point enforces identically."""
    import asyncio

    mw = IdentityGuardrailMiddleware()
    state = {"identity": _Identity(permissions={"thread:read"})}  # no write
    req = _make_request("bash", state=state)

    async def handler(_req):
        raise AssertionError("handler should not run")

    result = asyncio.run(_arun(mw, req, handler))
    assert result.status == "error"
    assert "thread:write" in result.content


def test_state_missing_falls_through():
    """If ToolCallRequest.state is None we cannot evaluate — fall through."""
    mw = IdentityGuardrailMiddleware()
    req = _make_request("bash", state=None)
    handler = MagicMock(return_value="ok")
    assert mw.wrap_tool_call(req, handler) == "ok"


def test_registered_when_signing_key_set(monkeypatch):
    monkeypatch.setenv("DEERFLOW_INTERNAL_SIGNING_KEY", "dummy-key")
    from deerflow.agents.middlewares.tool_error_handling_middleware import build_lead_runtime_middlewares

    chain = build_lead_runtime_middlewares(lazy_init=True)
    assert any(isinstance(mw, IdentityGuardrailMiddleware) for mw in chain)


def test_not_registered_when_no_signing_key(monkeypatch):
    monkeypatch.delenv("DEERFLOW_INTERNAL_SIGNING_KEY", raising=False)
    from deerflow.agents.middlewares.tool_error_handling_middleware import build_lead_runtime_middlewares

    chain = build_lead_runtime_middlewares(lazy_init=True)
    assert all(not isinstance(mw, IdentityGuardrailMiddleware) for mw in chain)
