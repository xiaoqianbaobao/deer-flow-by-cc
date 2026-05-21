"""Shared helpers for reading identity context from middleware state.

The harness must not import ``app.gateway.identity.auth.Identity`` (boundary
enforced by ``tests/test_harness_boundary.py``). Any middleware that needs to
pull ``tenant_id`` / ``workspace_id`` out of an opaque identity object should
use :func:`extract_tenant_ids` here so the duck-typing logic stays in one
place.

Identity may appear in state as:

* ``None`` — flag-off / legacy mode (no identity subsystem).
* A dict — e.g. forwarded through ``runtime.context`` as a plain mapping.
* A dataclass / ``SimpleNamespace`` / custom class — the production
  ``Identity`` dataclass lives in ``app.gateway.identity.auth`` and exposes
  ``tenant_id`` / ``workspace_id`` as attributes.
"""

from __future__ import annotations

from typing import Any


def extract_tenant_ids(identity: Any) -> tuple[int | None, int | None]:
    """Best-effort extraction of ``(tenant_id, workspace_id)`` from *identity*.

    Returns ``(None, None)`` whenever the values cannot be safely read.
    The caller is expected to treat that as "fall back to the legacy
    non-stratified layout" — every tenant-aware :class:`~deerflow.config.paths.Paths`
    helper already does so when either id is missing.
    """
    if identity is None:
        return (None, None)

    def _get(key: str) -> Any:
        # Attribute lookup first (covers dataclass, SimpleNamespace, custom classes).
        value = getattr(identity, key, None)
        if value is not None:
            return value
        # Dict-style fallback. ``Mapping`` would be cleaner but importing it
        # here is unnecessary — the ``.get`` duck-type is stable and fast.
        if hasattr(identity, "get"):
            try:
                return identity.get(key)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover — extremely defensive
                return None
        return None

    return (_get("tenant_id"), _get("workspace_id"))
