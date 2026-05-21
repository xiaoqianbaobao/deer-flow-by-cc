"""Sensitive-field scrubber + per-action summariser (spec §9.4).

Redaction runs **before** an event is enqueued. The writer never sees raw
values. Rules:

- Any dict key matching ``/password|token|secret|key|authorization/i`` has
  its value replaced with ``'***'``. Applied recursively to nested dicts
  and through lists.
- ``http.body`` / ``body`` / ``request_body`` / ``response_body`` are
  always dropped — we never persist raw payloads.
- ``command`` / ``cmd`` (bash args summary) longer than 500 chars is
  truncated with a ``…`` marker.
- ``write_file`` tool args: keep ``path`` and ``size``, drop ``content``.

The redactor is conservative by design: if in doubt, scrub. Producers can
pre-flatten their metadata if they want structured fields to survive.
"""

from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEY_RE = re.compile(r"(password|token|secret|key|authorization)", re.IGNORECASE)
_DROP_KEYS = frozenset({"http.body", "body", "request_body", "response_body"})
_COMMAND_KEYS = frozenset({"command", "cmd"})
_MAX_COMMAND_LEN = 500
_MASK = "***"


def redact_metadata(action: str, raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a redacted shallow copy of ``raw``.

    ``action`` is carried for tool-specific summarisation (e.g.
    ``tool.called`` with ``write_file`` drops content).

    ``raw is None`` → returns ``{}`` so callers can drop the conditional.
    """

    if raw is None:
        return {}

    # write_file → keep path + size
    if action in {"tool.called", "tool.denied", "tool.failed"} and raw.get("tool") == "write_file":
        kept = {k: v for k, v in raw.items() if k != "args"}
        args = raw.get("args") or {}
        if isinstance(args, dict):
            compact = {}
            if "path" in args:
                compact["path"] = args["path"]
            if "size" in args:
                compact["size"] = args["size"]
            elif isinstance(args.get("content"), str):
                compact["size"] = len(args["content"])
            kept["args"] = compact
        return _scrub(kept)

    return _scrub(raw)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in _DROP_KEYS:
                continue
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k):
                out[k] = _MASK
                continue
            if k in _COMMAND_KEYS and isinstance(v, str):
                out[k] = _truncate_command(v)
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item) for item in value)
    return value


def _truncate_command(cmd: str) -> str:
    if len(cmd) <= _MAX_COMMAND_LEN:
        return cmd
    return cmd[:_MAX_COMMAND_LEN] + "…"
