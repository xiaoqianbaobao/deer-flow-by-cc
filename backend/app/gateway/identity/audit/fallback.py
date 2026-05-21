"""On-disk JSONL fallback for critical audit events (spec §9.3).

When Postgres is unreachable, ``AuditBatchWriter`` routes critical events
through :func:`write` into ``{deer_flow_home}/_audit/fallback.jsonl``. A
follow-up backfill (``backfill``) streams that file into PG once the
connection recovers and deletes the file.

Design notes:
- Append-only JSONL, one event per line.
- ``asyncio.Lock`` serialises writes from the writer task.
- File moves happen outside the lock to minimise hold time.
- Callers only invoke these functions from the writer task, so there are
  no cross-task race windows beyond the lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.gateway.identity.audit.events import AuditEvent

logger = logging.getLogger(__name__)

_FALLBACK_DIRNAME = "_audit"
_FALLBACK_FILENAME = "fallback.jsonl"


def fallback_path(deer_flow_home: str | os.PathLike) -> Path:
    """Return the full JSONL path, creating the parent dir on first call."""
    p = Path(deer_flow_home) / _FALLBACK_DIRNAME / _FALLBACK_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _event_to_json(event: AuditEvent) -> str:
    d = asdict(event)
    # datetime → ISO for JSONL portability.
    d["created_at"] = event.created_at.isoformat()
    return json.dumps(d, separators=(",", ":"))


def _json_to_event(line: str) -> AuditEvent:
    d = json.loads(line)
    d["created_at"] = datetime.fromisoformat(d["created_at"])
    return AuditEvent(**d)


class FallbackLog:
    """JSONL writer/reader scoped to one deer_flow_home."""

    def __init__(self, deer_flow_home: str | os.PathLike) -> None:
        self._path = fallback_path(deer_flow_home)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def write(self, event: AuditEvent) -> None:
        line = _event_to_json(event) + "\n"
        async with self._lock:
            # Open per-write so we tolerate external rotation. Small cost
            # is acceptable — this only runs on PG outages.
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    async def write_many(self, events: Iterable[AuditEvent]) -> None:
        lines = [_event_to_json(e) + "\n" for e in events]
        if not lines:
            return
        async with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.writelines(lines)

    async def drain(self) -> list[AuditEvent]:
        """Atomically rotate the file and return all queued events.

        If the file does not exist, returns ``[]``. After drain, the file
        is removed; callers are expected to re-enqueue on success and
        call :meth:`write_many` on failure to restore the backlog.
        """

        async with self._lock:
            if not self._path.exists():
                return []
            # Rename first so concurrent writers start a fresh file.
            rotated = self._path.with_suffix(".jsonl.rotating")
            self._path.rename(rotated)

        try:
            with rotated.open("r", encoding="utf-8") as fh:
                events = [_json_to_event(line) for line in fh if line.strip()]
        except Exception:
            logger.exception("fallback drain failed; restoring file")
            # Restore the file so we don't lose records.
            async with self._lock:
                if not self._path.exists():
                    rotated.rename(self._path)
            raise
        else:
            try:
                rotated.unlink()
            except FileNotFoundError:
                pass
            return events
