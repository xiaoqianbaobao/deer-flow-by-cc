"""Reverse a previously-applied migration plan (spec §10.2 "rollback").

Symmetric to :mod:`executor`: for each item we

1. Remove the forwarder symlink at ``source`` (if present).
2. ``os.rename`` the target back to ``source`` (falls back to
   ``shutil.move`` on EXDEV).
3. Emit a ``system.migration.item.moved`` audit event with
   ``metadata.status == "rolled-back"`` so the action history remains
   attributable without a dedicated action string.

Items that are not ``already_migrated == True`` (i.e. never moved in the
first place) are skipped. The rollback is therefore idempotent: running
it twice in a row produces the same on-disk state.

Ordering
~~~~~~~~

Reverse iteration matches the invariant that parents migrate before
children — reversing the list guarantees children are restored before
their parent directory is recreated.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
from pathlib import Path

from app.gateway.identity.audit.events import AuditEvent
from app.gateway.identity.migration.executor import AuditWriter
from app.gateway.identity.migration.planner import MigrationItem, MigrationPlan
from app.gateway.identity.migration.report import (
    ItemResult,
    MigrationReport,
    write_report,
)

logger = logging.getLogger(__name__)

__all__ = ["rollback_plan"]


async def rollback_plan(
    plan: MigrationPlan,
    *,
    report_path: Path,
    audit_writer: AuditWriter | None = None,
) -> MigrationReport:
    """Reverse ``plan``. Safe to call on a partially-applied run."""

    report = MigrationReport.start(mode="rollback", plan=plan)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    for item in reversed(plan.items):
        if not _target_exists(item):
            report.items.append(
                ItemResult(
                    kind=item.kind.value,
                    source=str(item.source),
                    target=str(item.target),
                    status="skipped",
                )
            )
            continue

        try:
            await _rollback_one(item, plan=plan, audit_writer=audit_writer)
            report.items.append(
                ItemResult(
                    kind=item.kind.value,
                    source=str(item.source),
                    target=str(item.target),
                    status="moved",
                )
            )
        except Exception as exc:  # noqa: BLE001 — per-item resilience
            logger.exception("rollback item failed: %s", item)
            report.items.append(
                ItemResult(
                    kind=item.kind.value,
                    source=str(item.source),
                    target=str(item.target),
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            report.errors.append(f"{item.target}: {exc}")

    report.finish()
    write_report(report_path, report)
    return report


def _target_exists(item: MigrationItem) -> bool:
    try:
        return Path(item.target).exists()
    except OSError:
        return False


async def _rollback_one(
    item: MigrationItem,
    *,
    plan: MigrationPlan,
    audit_writer: AuditWriter | None,
) -> None:
    src = Path(item.source)
    tgt = Path(item.target)

    if src.is_symlink():
        src.unlink()

    if src.exists():
        raise FileExistsError(f"source {src!s} still exists (not a symlink) — refusing to overwrite pre-existing data")

    try:
        os.rename(tgt, src)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(tgt), str(src))

    if audit_writer is not None:
        event = AuditEvent(
            action="system.migration.item.moved",
            result="success",
            tenant_id=plan.tenant_id,
            workspace_id=plan.workspace_id,
            resource_type=item.kind.value,
            resource_id=item.source.name,
            metadata={
                "source": str(item.source),
                "target": str(item.target),
                "status": "rolled-back",
            },
        )
        res = audit_writer(event, True)
        if res is not None:
            await res
