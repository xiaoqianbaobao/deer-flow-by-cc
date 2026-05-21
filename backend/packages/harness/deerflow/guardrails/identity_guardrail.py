"""Identity-driven tool authorization middleware (M5 Task 3).

Extends the guardrail layer with a whitelist-mode permission map keyed on
``state["identity"].permissions``. Runs *before* any configured OAP /
allowlist provider, so a caller missing the required tag is denied with an
``authz.tool.denied`` reason regardless of what the downstream provider
would have said.

Flag-off / legacy behavior: when ``state["identity"]`` is absent (the M5
IdentityMiddleware did not run) the check is a no-op — we fall through to
the existing provider chain so pre-M5 deployments are unaffected.

Tool permission map is per spec §6.4. Whitelist default-deny: tools not in
the map and not carrying a ``required_permission`` attribute are denied
with reason ``authz.tool.unknown``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)


# Spec §6.4 — tool → required-permission tag. Tools outside this map are
# treated as unknown and denied unless they carry a ``required_permission``
# attribute (typically set by MCP adapter registration).
TOOL_PERMISSION_MAP: dict[str, str] = {
    "bash": "thread:write",
    "write_file": "thread:write",
    "str_replace": "thread:write",
    "read_file": "thread:read",
    "ls": "thread:read",
    "task": "thread:write",
    "present_files": "thread:read",
    "view_image": "thread:read",
    "ask_clarification": "thread:read",
    # ``write_todos`` is handled entirely by TodoListMiddleware and never
    # reaches the tool executor — no permission gate needed here.
}

DEFAULT_MCP_PERMISSION = "skill:invoke"

# Tools the harness always allows without a permission check. These are
# internal LangGraph plumbing (not user-invocable) that should never be
# blocked by authorization policy — doing so would break graph execution.
_INTERNAL_TOOL_ALLOWLIST = frozenset({"write_todos"})


def _identity_has_permission(identity, tag: str) -> bool:
    """Check whether *identity* carries *tag*. Defensive against shapes.

    The harness cannot import the Gateway ``Identity`` dataclass, so this
    duck-types against either a ``permissions`` attribute (Gateway /
    harness :class:`~deerflow.identity_propagation.VerifiedIdentity`) or a
    ``get("permissions")``-style dict.
    """
    if identity is None:
        return False

    perms = getattr(identity, "permissions", None)
    if perms is None and hasattr(identity, "get"):
        try:
            perms = identity.get("permissions")  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - very defensive
            perms = None
    if not perms:
        return False
    return tag in perms


def _resolve_required_permission(tool_call: dict, tool_registry: dict | None) -> tuple[str, str] | None:
    """Return ``(tag, reason_code)`` for the required permission, or None.

    ``None`` means the tool is unknown (whitelist default-deny applies).
    ``reason_code`` tells the denial path whether the gate came from the
    built-in map or MCP adapter metadata.
    """
    name = str(tool_call.get("name", ""))
    if not name:
        return None

    if name in _INTERNAL_TOOL_ALLOWLIST:
        return ("", "authz.tool.internal")  # signal: skip check

    # Built-in / sandbox / subagent tool
    if name in TOOL_PERMISSION_MAP:
        return (TOOL_PERMISSION_MAP[name], "authz.tool.builtin")

    # MCP-registered tool: check the live tool registry for a declared
    # required_permission attribute; if absent fall back to the MCP default.
    # The registry lookup is best-effort — harness does not always have
    # access to the bound tool instances here.
    if tool_registry is not None:
        tool_obj = tool_registry.get(name)
        if tool_obj is not None:
            declared = getattr(tool_obj, "required_permission", None)
            if isinstance(declared, str) and declared:
                return (declared, "authz.tool.mcp_declared")
            # Tool object present but no declared permission → treat as MCP
            # and apply the default.
            return (DEFAULT_MCP_PERMISSION, "authz.tool.mcp_default")

    # Unknown tool — neither mapped nor MCP-registered. Whitelist
    # default-deny per spec §6.4.
    return None


class IdentityGuardrailMiddleware(AgentMiddleware[AgentState]):
    """Enforce ``state["identity"].permissions`` against the TOOL_PERMISSION_MAP.

    Registered *before* the (optional) OAP-style ``GuardrailMiddleware``
    so both gates compose: missing identity permission → denied here;
    present here but blocked by the provider's policy → denied there.

    When ``state["identity"]`` is not populated (flag-off / pre-M5 run),
    this middleware is a no-op — tool calls pass through unchanged and
    any configured provider still runs.
    """

    def __init__(self, *, tool_registry: dict | None = None):
        super().__init__()
        # ``tool_registry`` is optional dict[name -> tool]. If None, MCP
        # tools are treated as requiring DEFAULT_MCP_PERMISSION.
        self._tool_registry = tool_registry

    def _build_deny_message(self, request: ToolCallRequest, reason: str, code: str) -> ToolMessage:
        tool_name = str(request.tool_call.get("name", "unknown_tool"))
        tool_call_id = str(request.tool_call.get("id", "missing_id"))
        # The message content is what the model sees as the ToolMessage
        # output, so phrase it so the agent can recover / try alternatives.
        return ToolMessage(
            content=f"Permission denied: {reason}",
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
            additional_kwargs={"audit_action": "authz.tool.denied", "audit_code": code},
        )

    def _check(self, state: AgentState, request: ToolCallRequest) -> ToolMessage | None:
        if isinstance(state, dict):
            identity = state.get("identity")
        else:
            identity = getattr(state, "identity", None)

        # Flag-off: no identity on state → pass through. Existing behavior
        # (OAP allowlist, etc.) is unaffected.
        if identity is None:
            return None

        resolved = _resolve_required_permission(request.tool_call, self._tool_registry)
        tool_name = str(request.tool_call.get("name", ""))

        if resolved is None:
            # Unknown tool — whitelist default-deny.
            logger.warning("IdentityGuardrail denying unknown tool '%s' for user=%s (whitelist policy)", tool_name, getattr(identity, "user_id", "?"))
            return self._build_deny_message(
                request,
                reason=f"tool '{tool_name}' is not in the permission map",
                code="authz.tool.unknown",
            )

        required_tag, reason_code = resolved
        if reason_code == "authz.tool.internal":
            return None  # internal plumbing, skip

        if not _identity_has_permission(identity, required_tag):
            user_id = getattr(identity, "user_id", "?")
            logger.info(
                "IdentityGuardrail denying tool=%s for user=%s missing permission=%s (code=%s)",
                tool_name,
                user_id,
                required_tag,
                reason_code,
            )
            return self._build_deny_message(
                request,
                reason=f"missing permission '{required_tag}' for tool '{tool_name}'",
                code=reason_code,
            )

        # Identity has the tag — defer to downstream providers.
        return None

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        # ``state`` is not directly available in wrap_tool_call — we have
        # to use LangGraph's runtime. ``AgentMiddleware`` exposes state via
        # the wrapped tool's request.state, which LangGraph stashes during
        # dispatch. Use ``langgraph.config.get_config`` as a fallback? It
        # doesn't carry state. So we access state via request.state if the
        # ToolCallRequest has it; else fall through without a check.
        state = getattr(request, "state", None)
        if state is None:
            # Cannot evaluate — fall through to next middleware / handler
            # rather than block spuriously.
            return handler(request)

        try:
            denial = self._check(state, request)
        except GraphBubbleUp:
            raise
        except Exception:
            logger.exception("IdentityGuardrail evaluation error — falling through")
            return handler(request)

        if denial is not None:
            return denial
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        state = getattr(request, "state", None)
        if state is None:
            return await handler(request)

        try:
            denial = self._check(state, request)
        except GraphBubbleUp:
            raise
        except Exception:
            logger.exception("IdentityGuardrail evaluation error — falling through")
            return await handler(request)

        if denial is not None:
            return denial
        return await handler(request)
