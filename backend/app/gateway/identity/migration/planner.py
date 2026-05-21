"""Enumerate legacy sources and build a migration plan (spec §10.2 / §10.3).

The planner is **pure**: it takes three input roots + a tenant/workspace
pair and returns a frozen :class:`MigrationPlan`. It never touches the
filesystem beyond a ``Path.iterdir`` / ``Path.is_dir`` probe on the three
source roots (everything else is lexical).

Sources scanned
---------------

* ``{legacy_home}/threads/{thread_id}``  → tenant workspace threads tree
* ``{repo_root}/skills/custom/*``        → tenant custom skills
* ``{repo_root}/skills/user/*``          → workspace user skills

Each entry in the plan carries absolute source + target + a kind tag. The
plan is serialisable (dataclasses + enums) so the CLI can pretty-print it
or emit the JSON form without further reflection.

Idempotency hook
~~~~~~~~~~~~~~~~

The planner skips any source whose target already exists **unless** the
source is a symlink whose realpath equals the target — that case means
the item has already been migrated and the old path is now a forwarder.
A partially-applied run is therefore safe to re-plan and re-execute: the
second pass will only enumerate leftovers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from app.gateway.identity.storage.paths import (
    skills_tenant_custom_root,
    skills_workspace_user_root,
    thread_path,
)

__all__ = [
    "ItemKind",
    "MigrationItem",
    "MigrationPlan",
    "build_plan",
]


class ItemKind(StrEnum):
    """The three migratable categories (spec §10.2)."""

    THREAD = "thread"
    SKILL_CUSTOM = "skill_custom"
    SKILL_USER = "skill_user"


@dataclass(frozen=True, slots=True)
class MigrationItem:
    """One entry of the plan: a single dir to move (or already-moved).

    ``already_migrated`` distinguishes the "old path is now a symlink to
    the new home" state from a fresh directory that still needs to move.
    Skipped items are still emitted in the plan so reports are complete.
    """

    kind: ItemKind
    source: Path
    target: Path
    already_migrated: bool = False


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """Frozen, ordered list of items + the tenant/workspace we are targeting.

    Consumers:

    * ``executor.apply_plan`` — iterates items in order, moves each.
    * ``rollback.reverse_plan`` — iterates in reverse, restores each.
    * ``report.write_report`` — dumps this structure to JSON.
    """

    tenant_id: int
    workspace_id: int
    tenant_slug: str
    workspace_slug: str
    items: tuple[MigrationItem, ...] = field(default_factory=tuple)

    @property
    def pending(self) -> tuple[MigrationItem, ...]:
        return tuple(i for i in self.items if not i.already_migrated)

    @property
    def skipped(self) -> tuple[MigrationItem, ...]:
        return tuple(i for i in self.items if i.already_migrated)


def _iter_direct_children(root: Path) -> list[Path]:
    """Return a sorted list of immediate children of ``root``.

    Absent or non-directory → empty list. Sorting makes the plan
    deterministic, which matters for idempotency assertions and for the
    human-readable plan printer.
    """

    if not root.exists() or not root.is_dir():
        return []
    return sorted((c for c in root.iterdir()), key=lambda p: p.name)


def _already_migrated(source: Path, target: Path) -> bool:
    """Return True when ``source`` is a symlink resolving to ``target``.

    After a successful apply the planner drops a ``source → target``
    symlink; re-running the planner must recognise that as "done" rather
    than attempting to move a link over an existing directory.
    """

    if not source.is_symlink():
        return False
    try:
        return source.resolve() == target.resolve()
    except (OSError, RuntimeError):
        return False


def build_plan(
    *,
    legacy_home: Path,
    repo_root: Path,
    tenant_id: int,
    workspace_id: int,
    tenant_slug: str,
    workspace_slug: str,
) -> MigrationPlan:
    """Discover migratable items under the three source roots.

    ``legacy_home`` is the pre-M4 single-tenant ``$DEER_FLOW_HOME``
    (typically ``backend/.deer-flow``). ``repo_root`` is the project root
    used to resolve ``skills/custom`` and ``skills/user``.

    The function does not create any directories; it only reads names.
    Targets are computed via the M4 helpers in ``storage/paths.py`` so
    the two paths always agree on layout.
    """

    items: list[MigrationItem] = []

    # --- threads ---
    threads_root = legacy_home / "threads"
    for src in _iter_direct_children(threads_root):
        if not src.is_dir() and not src.is_symlink():
            continue
        thread_id = src.name
        tgt = thread_path(tenant_id, workspace_id, thread_id)
        items.append(
            MigrationItem(
                kind=ItemKind.THREAD,
                source=src,
                target=tgt,
                already_migrated=_already_migrated(src, tgt),
            )
        )

    # --- skills/custom ---
    custom_root = repo_root / "skills" / "custom"
    custom_target_root = skills_tenant_custom_root(tenant_id)
    for src in _iter_direct_children(custom_root):
        if not src.is_dir() and not src.is_symlink():
            continue
        tgt = custom_target_root / src.name
        items.append(
            MigrationItem(
                kind=ItemKind.SKILL_CUSTOM,
                source=src,
                target=tgt,
                already_migrated=_already_migrated(src, tgt),
            )
        )

    # --- skills/user ---
    user_root = repo_root / "skills" / "user"
    user_target_root = skills_workspace_user_root(tenant_id, workspace_id)
    for src in _iter_direct_children(user_root):
        if not src.is_dir() and not src.is_symlink():
            continue
        tgt = user_target_root / src.name
        items.append(
            MigrationItem(
                kind=ItemKind.SKILL_USER,
                source=src,
                target=tgt,
                already_migrated=_already_migrated(src, tgt),
            )
        )

    return MigrationPlan(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        tenant_slug=tenant_slug,
        workspace_slug=workspace_slug,
        items=tuple(items),
    )
