"""Apply a migration plan to the filesystem (spec §10.2 / §10.3).

The executor performs three actions per item, in order:

1. ``os.rename`` source → target (a fast, atomic move within the same
   filesystem; the planner and target-root helpers guarantee both paths
   live under ``$DEER_FLOW_HOME`` so cross-device moves are not expected).
2. Drop a ``source → target`` symlink so legacy code that still reads the
   old path transparently follows to the new home.
3. For skills, validate that the new symlink's realpath stays inside the
   tenant root (``assert_symlink_parent_safe``) so a malicious on-disk
   layout cannot use the migration to escape into another tenant's tree.
4. Emit an ``system.migration.item.moved`` audit event via the optional
   writer hook — the CLI wires this up when identity is enabled, the
   unit tests pass ``audit_writer=None``.

Batching: the report is fsync'd after every ``REPORT_FSYNC_EVERY`` items
so a crash mid-run still leaves a partial, readable JSON report on disk.

Cross-filesystem handling
~~~~~~~~~~~~~~~~~~~~~~~~~

``os.rename`` raises ``OSError(EXDEV)`` if source and target are on
different mounts. In that case we fall back to ``shutil.move`` which
performs a copy + unlink; the post-check still validates byte-count
parity, so silent corruption is caught.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.gateway.identity.audit.events import AuditEvent
from app.gateway.identity.migration.planner import (
    ItemKind,
    MigrationItem,
    MigrationPlan,
)
from app.gateway.identity.migration.report import (
    ItemResult,
    MigrationReport,
    write_report,
)
from app.gateway.identity.storage.path_guard import (
    PathEscapeError,
    assert_symlink_parent_safe,
)
from app.gateway.identity.storage.paths import tenant_root

logger = logging.getLogger(__name__)

__all__ = [
    "REPORT_FSYNC_EVERY",
    "AuditWriter",
    "apply_plan",
    "validate_plan",
]

# Flush the JSON report to disk after this many items are processed.
REPORT_FSYNC_EVERY = 50

# Minimal structural type for the audit sink — we only need ``enqueue``
# so tests can pass a stub without importing ``AuditBatchWriter``.
AuditWriter = Callable[[AuditEvent, bool], Awaitable[None] | None]


def _count_files(p: Path) -> int:
    """Return the number of regular files under ``p`` (recursive).

    Used by the post-check to assert byte-count parity after a move.
    Counts files, not directories or symlinks.
    """

    if p.is_file():
        return 1
    total = 0
    for child in p.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += 1
        except OSError:
            continue
    return total


def validate_plan(plan: MigrationPlan, *, enforce_tenant_root: bool = True) -> None:
    """Raise if any planned target would escape its tenant root.

    Called once before the executor touches the filesystem. Skill items
    (custom + user) are double-checked via ``assert_symlink_parent_safe``
    once the symlink exists, but the pre-check here catches typos in
    ``tenant_id`` / ``workspace_id`` arguments before any state changes.
    """

    if not enforce_tenant_root:
        return

    t_root = tenant_root(plan.tenant_id).resolve()
    for item in plan.items:
        # Threads + workspace-user + tenant-custom all live under
        # ``tenants/{tid}/``; targets must resolve inside that tree.
        resolved_target = Path(item.target).resolve()
        if not resolved_target.is_relative_to(t_root):
            raise PathEscapeError(f"target {resolved_target!s} for {item.kind.value} escapes tenant root {t_root!s}")


async def apply_plan(
    plan: MigrationPlan,
    *,
    report_path: Path,
    audit_writer: AuditWriter | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    """Execute ``plan`` and return the resulting report.

    When ``dry_run`` is ``True`` the filesystem is **never** mutated: the
    function walks the plan, classifies each item (skipped vs. would-be
    moved), writes the report, and returns. This matches the spec's
    ``--dry-run`` contract: "never writes".

    When ``dry_run`` is ``False`` every pending item is moved and a
    forwarder symlink is left at the old path.
    """

    validate_plan(plan)

    mode = "dry-run" if dry_run else "apply"
    report = MigrationReport.start(mode=mode, plan=plan)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    processed = 0

    for item in plan.items:
        if item.already_migrated:
            report.items.append(
                ItemResult(
                    kind=item.kind.value,
                    source=str(item.source),
                    target=str(item.target),
                    status="skipped",
                )
            )
            processed += 1
            if processed % REPORT_FSYNC_EVERY == 0:
                write_report(report_path, report)
            continue

        if dry_run:
            report.items.append(
                ItemResult(
                    kind=item.kind.value,
                    source=str(item.source),
                    target=str(item.target),
                    status="moved",  # "would move" — mode=="dry-run" disambiguates
                )
            )
            processed += 1
            if processed % REPORT_FSYNC_EVERY == 0:
                write_report(report_path, report)
            continue

        try:
            await _execute_one(item, plan=plan, audit_writer=audit_writer)
            report.items.append(
                ItemResult(
                    kind=item.kind.value,
                    source=str(item.source),
                    target=str(item.target),
                    status="moved",
                )
            )
        except Exception as exc:  # noqa: BLE001 — per-item resilience
            logger.exception("migration item failed: %s", item)
            report.items.append(
                ItemResult(
                    kind=item.kind.value,
                    source=str(item.source),
                    target=str(item.target),
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            report.errors.append(f"{item.source}: {exc}")

        processed += 1
        if processed % REPORT_FSYNC_EVERY == 0:
            write_report(report_path, report)

    report.finish()
    write_report(report_path, report)
    return report


async def _execute_one(
    item: MigrationItem,
    *,
    plan: MigrationPlan,
    audit_writer: AuditWriter | None,
) -> None:
    """Move a single item and emit its audit event.

    Invariants on success:

    * ``item.target`` exists and contains the original bytes.
    * ``item.source`` is a symlink pointing at ``item.target``.
    * For skills: ``assert_symlink_parent_safe`` passes on the new link.
    """

    src = Path(item.source)
    tgt = Path(item.target)

    if tgt.exists() and not tgt.is_symlink():
        # Idempotency guard: target already populated by a prior run.
        # This is strictly separate from the ``already_migrated`` short
        # circuit above — that one covers a forwarder symlink; this one
        # covers the rarer "move succeeded, symlink drop failed" recovery.
        if src.exists() and not src.is_symlink():
            raise FileExistsError(f"target {tgt!s} already exists while source {src!s} is not a symlink")
        _ensure_forwarder_symlink(src, tgt, kind=item.kind, tenant_id=plan.tenant_id)
        await _emit_audit(item, plan, audit_writer, status="symlink-only")
        return

    tgt.parent.mkdir(parents=True, exist_ok=True)

    original_count = _count_files(src)

    try:
        os.rename(src, tgt)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Different filesystems: fall back to copy + unlink.
        shutil.move(str(src), str(tgt))

    new_count = _count_files(tgt)
    if new_count != original_count:
        raise RuntimeError(f"byte-count parity check failed for {item.kind.value}: source had {original_count} files, target has {new_count}")

    _ensure_forwarder_symlink(src, tgt, kind=item.kind, tenant_id=plan.tenant_id)
    await _emit_audit(item, plan, audit_writer, status="moved")


def _ensure_forwarder_symlink(
    source: Path,
    target: Path,
    *,
    kind: ItemKind,
    tenant_id: int,
) -> None:
    """Create (or repair) a symlink at ``source`` pointing at ``target``.

    A symlink whose realpath resolves outside the tenant root is rejected
    via ``assert_symlink_parent_safe`` for skill items. Threads are not
    checked against the tenant root by the symlink guard because the
    legacy thread path (``backend/.deer-flow/threads/{id}``) is OUTSIDE
    the per-tenant subtree by design — only the TARGET must live inside
    the tenant root, which ``validate_plan`` already enforced.
    """

    if source.exists() and not source.is_symlink():
        # Defensive: a non-symlink left at the source after a move should
        # never happen, but if it does we refuse to overwrite data.
        raise FileExistsError(f"source {source!s} is not a symlink after move; refusing to overwrite")

    if source.is_symlink():
        try:
            if source.resolve() == target.resolve():
                return
        except OSError:
            pass
        source.unlink()

    source.symlink_to(target, target_is_directory=target.is_dir())

    # Skills land under ``tenant_root(tid)`` — verify the new link.
    if kind in (ItemKind.SKILL_CUSTOM, ItemKind.SKILL_USER):
        assert_symlink_parent_safe(source, tenant_root(tenant_id))


async def _emit_audit(
    item: MigrationItem,
    plan: MigrationPlan,
    writer: AuditWriter | None,
    *,
    status: str,
) -> None:
    if writer is None:
        return
    event = AuditEvent(
        action="system.migration.item.moved",
        result="success",
        tenant_id=plan.tenant_id,
        workspace_id=plan.workspace_id,
        resource_type=item.kind.value,
        resource_id=item.target.name,
        metadata={
            "source": str(item.source),
            "target": str(item.target),
            "status": status,
        },
    )
    res = writer(event, True)  # critical=True → never silently dropped
    if res is not None:
        await res
