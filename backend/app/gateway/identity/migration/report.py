"""Structured JSON report for a migration run (spec §10.2 "report").

Written to ``{home}/_system/migration_report_{ts}.json`` via the helper
in :func:`write_report`. The shape is stable enough to be consumed by
operators / dashboards and by the M7 admin UI in the future.

Sync semantics
~~~~~~~~~~~~~~

The executor calls :func:`write_report` after every batch of 50 moves so
a crash mid-run still leaves a partial report on disk. We ``fsync`` both
the file and its parent directory: just fsync'ing the file doesn't
guarantee the new inode is durable on POSIX; the parent dir fsync makes
the rename visible after reboot.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.gateway.identity.migration.planner import ItemKind, MigrationPlan

__all__ = [
    "MigrationReport",
    "ItemResult",
    "write_report",
    "now_ts",
]


def now_ts() -> str:
    """Return a filesystem-safe ISO-8601-ish timestamp.

    Colons and ``+`` are replaced with ``-`` so the report filename is
    portable across POSIX and Windows.
    """

    return datetime.now(UTC).isoformat(timespec="seconds").replace(":", "-").replace("+", "Z")


@dataclass(slots=True)
class ItemResult:
    """Outcome of one :class:`MigrationItem` after execution."""

    kind: str
    source: str
    target: str
    status: str  # "moved" | "skipped" | "failed"
    error: str | None = None


@dataclass(slots=True)
class MigrationReport:
    """Serialisable summary of a run.

    ``started_at`` / ``ended_at`` are ISO-8601 UTC. ``mode`` is
    ``"dry-run"`` or ``"apply"``. ``items`` carries one :class:`ItemResult`
    per planned entry.
    """

    mode: str
    tenant_id: int
    workspace_id: int
    tenant_slug: str
    workspace_slug: str
    started_at: str
    ended_at: str | None = None
    items: list[ItemResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def start(cls, *, mode: str, plan: MigrationPlan) -> MigrationReport:
        return cls(
            mode=mode,
            tenant_id=plan.tenant_id,
            workspace_id=plan.workspace_id,
            tenant_slug=plan.tenant_slug,
            workspace_slug=plan.workspace_slug,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def finish(self) -> None:
        self.ended_at = datetime.now(UTC).isoformat(timespec="seconds")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "tenant_slug": self.tenant_slug,
            "workspace_slug": self.workspace_slug,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "counts": self._counts(),
            "errors": list(self.errors),
            "items": [
                {
                    "kind": i.kind,
                    "source": i.source,
                    "target": i.target,
                    "status": i.status,
                    **({"error": i.error} if i.error else {}),
                }
                for i in self.items
            ],
        }

    def _counts(self) -> dict[str, int]:
        by_status: dict[str, int] = {"moved": 0, "skipped": 0, "failed": 0}
        by_kind: dict[str, int] = {k.value: 0 for k in ItemKind}
        for item in self.items:
            by_status[item.status] = by_status.get(item.status, 0) + 1
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
        return {**by_status, **{f"kind_{k}": v for k, v in by_kind.items()}}


def write_report(path: Path, report: MigrationReport) -> None:
    """Atomically write ``report`` to ``path``.

    Write to a temp file in the same directory, ``fsync`` the file,
    ``os.replace`` onto the target, then ``fsync`` the directory. This is
    the textbook "crash-safe" write idiom.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = json.dumps(report.to_dict(), indent=2, sort_keys=True).encode("utf-8")

    # tempfile in the same dir so the replace is atomic on all POSIX FSes.
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        # Clean up the temp file if the replace failed mid-way.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    # Fsync the directory to persist the rename.
    dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
