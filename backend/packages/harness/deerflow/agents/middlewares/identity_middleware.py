"""LangGraph-side identity propagation middleware (M5).

Reads the HMAC-signed ``X-Deerflow-*`` headers that the Gateway injected
into ``config.configurable.headers`` (see
:func:`app.gateway.identity.propagation.sign_identity_headers`), verifies
them against ``DEERFLOW_INTERNAL_SIGNING_KEY``, and writes the resulting
:class:`~deerflow.identity_propagation.VerifiedIdentity` into
``state["identity"]`` so downstream middlewares (guardrail, subagent
executor) can enforce tool-level permissions.

This is registered as **middleware position 0** on the lead agent so every
downstream stage sees the identity already populated.

Failure modes:

* **Headers absent**: legacy / flag-off mode. ``state["identity"]`` stays
  absent (we do not overwrite with ``None``); behavior is identical to
  pre-M5 runs. No error.
* **Tampered signature / stale timestamp**: raise so the run fails loud.
  We do *not* silently degrade to anonymous — a bad signature is either a
  key mismatch (operator error) or an attack, neither of which should
  proceed as if nothing happened.
"""

from __future__ import annotations

import logging
from typing import Any, NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config
from langgraph.runtime import Runtime

from deerflow.identity_propagation import (
    MissingHeaderError,
    VerifiedIdentity,
    verify_headers,
)

logger = logging.getLogger(__name__)


class IdentityMiddlewareState(AgentState):
    """State schema extension — the ``identity`` slot is opaque ``Any`` so
    the harness does not take a dependency on the Gateway dataclass.
    """

    identity: NotRequired[Any]


class IdentityMiddleware(AgentMiddleware[IdentityMiddlewareState]):
    """Verify propagated identity headers and populate ``state["identity"]``.

    Parameters
    ----------
    signing_key:
        HMAC key shared with the Gateway. The same value that
        :func:`app.gateway.identity.propagation.sign_identity_headers` was
        called with.
    skew_sec:
        Allowed clock skew window (seconds). Defaults to 300 per spec §5.4.
    """

    state_schema = IdentityMiddlewareState

    def __init__(self, *, signing_key: bytes | str, skew_sec: int = 300):
        super().__init__()
        self._signing_key = signing_key
        self._skew_sec = skew_sec

    def _read_headers(self) -> dict[str, str]:
        """Pull the header dict from the active LangGraph config.

        The Gateway stuffs signed headers under
        ``configurable["headers"]`` before invoking the agent. The key
        is stable across LangGraph SDK versions.

        Returns an empty dict when called outside a runnable context
        (e.g. in unit tests that construct the middleware directly) so
        the middleware degrades cleanly.
        """
        try:
            cfg = get_config()
        except Exception:  # pragma: no cover - exercised via unit tests
            return {}
        configurable = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}
        raw_headers = configurable.get("headers") or {}
        if not isinstance(raw_headers, dict):
            logger.warning("IdentityMiddleware: configurable['headers'] is not a dict, ignoring")
            return {}
        return raw_headers

    def _verify(self, headers: dict[str, str]) -> VerifiedIdentity | None:
        """Verify headers. Return ``None`` if no identity headers were sent.

        Raises :class:`deerflow.identity_propagation.InvalidSignatureError`
        or :class:`~deerflow.identity_propagation.StaleTimestampError` on
        attack/misconfiguration — the agent run fails loud, which is the
        correct behavior per spec §5.4.
        """
        try:
            return verify_headers(headers, key=self._signing_key, skew_sec=self._skew_sec)
        except MissingHeaderError:
            return None

    @override
    def before_agent(self, state: IdentityMiddlewareState, runtime: Runtime) -> dict | None:
        # If identity is already populated (e.g. subagent inheritance has
        # set it directly), don't overwrite — trust parent.
        if isinstance(state, dict) and state.get("identity") is not None:
            return None

        headers = self._read_headers()
        if not headers:
            return None

        verified = self._verify(headers)
        if verified is None:
            # Headers present but incomplete is treated as flag-off for
            # backward compat. Only a full-but-tampered header set fails
            # loud (verify_headers raises).
            return None

        logger.debug(
            "IdentityMiddleware populated identity: user_id=%s tenant_id=%s workspace_id=%s perms=%d",
            verified.user_id,
            verified.tenant_id,
            verified.workspace_id,
            len(verified.permissions),
        )
        return {"identity": verified}
