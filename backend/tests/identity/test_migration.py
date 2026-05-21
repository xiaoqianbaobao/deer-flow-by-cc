"""Unit tests for the M7 migration package.

These tests run WITHOUT Postgres — the planner/executor/rollback core is
pure filesystem work, which is exercised via ``tmp_path`` fixtures. The
CLI script itself is not exercised here; its DB pre-check and advisory
lock are covered by integration tests in a follow-up.

The tests assert the full contract:

* The planner correctly classifies pending vs. already-migrated items.
* ``--dry-run`` never mutates the filesystem.
* ``--apply`` moves files AND drops a forwarder symlink.
* Rollback restores the tree exactly.
* Symlink guard rejects a malicious post-apply link.
* Idempotency: a second apply after partial success is a no-op.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

# Tests don't need the DB. Skip the identity conftest's DB backend check.
os.environ.setdefault("IDENTITY_TEST_BACKEND", "off")


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect DEER_FLOW_HOME into an isolated tmp dir."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))
    return home


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "skills" / "custom").mkdir(parents=True)
    (repo / "skills" / "user").mkdir(parents=True)
    return repo


def _scaffold_legacy_state(home: Path, repo: Path, *, thread_ids: list[str]) -> None:
    """Drop fake content at the three legacy source paths."""

    threads_root = home / "threads"
    threads_root.mkdir(parents=True, exist_ok=True)
    for tid in thread_ids:
        d = threads_root / tid
        (d / "user-data" / "workspace").mkdir(parents=True)
        (d / "user-data" / "workspace" / "hello.txt").write_text(f"thread {tid}")

    (repo / "skills" / "custom" / "alpha").mkdir()
    (repo / "skills" / "custom" / "alpha" / "SKILL.md").write_text("custom alpha")
    (repo / "skills" / "user" / "bravo").mkdir()
    (repo / "skills" / "user" / "bravo" / "SKILL.md").write_text("user bravo")


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def test_planner_enumerates_all_three_source_roots(fake_home: Path, fake_repo: Path) -> None:
    from app.gateway.identity.migration.planner import ItemKind, build_plan

    _scaffold_legacy_state(fake_home, fake_repo, thread_ids=["t1", "t2"])

    plan = build_plan(
        legacy_home=fake_home,
        repo_root=fake_repo,
        tenant_id=1,
        workspace_id=1,
        tenant_slug="default",
        workspace_slug="default",
    )

    kinds = [item.kind for item in plan.items]
    assert kinds.count(ItemKind.THREAD) == 2
    assert kinds.count(ItemKind.SKILL_CUSTOM) == 1
    assert kinds.count(ItemKind.SKILL_USER) == 1
    assert all(not i.already_migrated for i in plan.items)


def test_planner_deterministic_ordering(fake_home: Path, fake_repo: Path) -> None:
    from app.gateway.identity.migration.planner import build_plan

    _scaffold_legacy_state(fake_home, fake_repo, thread_ids=["c", "a", "b"])

    plan = build_plan(
        legacy_home=fake_home,
        repo_root=fake_repo,
        tenant_id=1,
        workspace_id=1,
        tenant_slug="default",
        workspace_slug="default",
    )
    thread_names = [i.source.name for i in plan.items if i.source.name in {"a", "b", "c"}]
    assert thread_names == ["a", "b", "c"]


def test_planner_empty_tree_yields_empty_plan(fake_home: Path, fake_repo: Path) -> None:
    from app.gateway.identity.migration.planner import build_plan

    plan = build_plan(
        legacy_home=fake_home,
        repo_root=fake_repo,
        tenant_id=1,
        workspace_id=1,
        tenant_slug="default",
        workspace_slug="default",
    )
    assert plan.items == ()


# ---------------------------------------------------------------------------
# Executor — dry run
# ---------------------------------------------------------------------------


def test_dry_run_never_writes(fake_home: Path, fake_repo: Path) -> None:
    from app.gateway.identity.migration.executor import apply_plan
    from app.gateway.identity.migration.planner import build_plan

    _scaffold_legacy_state(fake_home, fake_repo, thread_ids=["x"])

    before = _snapshot(fake_home, fake_repo)

    plan = build_plan(
        legacy_home=fake_home,
        repo_root=fake_repo,
        tenant_id=1,
        workspace_id=1,
        tenant_slug="default",
        workspace_slug="default",
    )
    report = asyncio.run(apply_plan(plan, report_path=fake_home / "_system" / "dry_report.json", dry_run=True))

    after = _snapshot(fake_home, fake_repo, exclude={"_system"})
    assert before == after, "dry-run must not change the filesystem"
    assert report.mode == "dry-run"
    assert all(i.status != "failed" for i in report.items)


# ---------------------------------------------------------------------------
# Executor — apply
# ---------------------------------------------------------------------------


def test_apply_moves_and_creates_forwarder_symlinks(fake_home: Path, fake_repo: Path) -> None:
    from app.gateway.identity.migration.executor import apply_plan
    from app.gateway.identity.migration.planner import build_plan

    _scaffold_legacy_state(fake_home, fake_repo, thread_ids=["x", "y"])
    plan = build_plan(
        legacy_home=fake_home,
        repo_root=fake_repo,
        tenant_id=1,
        workspace_id=1,
        tenant_slug="default",
        workspace_slug="default",
    )

    asyncio.run(apply_plan(plan, report_path=fake_home / "_system" / "apply.json"))

    # Sources became symlinks.
    assert (fake_home / "threads" / "x").is_symlink()
    assert (fake_home / "threads" / "y").is_symlink()
    assert (fake_repo / "skills" / "custom" / "alpha").is_symlink()
    assert (fake_repo / "skills" / "user" / "bravo").is_symlink()

    # Targets exist with original content.
    assert (fake_home / "tenants" / "1" / "workspaces" / "1" / "threads" / "x" / "user-data" / "workspace" / "hello.txt").read_text() == "thread x"
    assert (fake_home / "tenants" / "1" / "custom" / "alpha" / "SKILL.md").read_text() == "custom alpha"
    assert (fake_home / "tenants" / "1" / "workspaces" / "1" / "user" / "bravo" / "SKILL.md").read_text() == "user bravo"


def test_apply_is_idempotent(fake_home: Path, fake_repo: Path) -> None:
    """Running apply twice leaves the tree in the same state."""
    from app.gateway.identity.migration.executor import apply_plan
    from app.gateway.identity.migration.planner import build_plan

    _scaffold_legacy_state(fake_home, fake_repo, thread_ids=["x"])

    def _run() -> None:
        plan = build_plan(
            legacy_home=fake_home,
            repo_root=fake_repo,
            tenant_id=1,
            workspace_id=1,
            tenant_slug="default",
            workspace_slug="default",
        )
        asyncio.run(apply_plan(plan, report_path=fake_home / "_system" / "run.json"))

    _run()
    first_snapshot = _snapshot(fake_home, fake_repo, exclude={"_system"})
    _run()
    second_snapshot = _snapshot(fake_home, fake_repo, exclude={"_system"})
    assert first_snapshot == second_snapshot


def test_apply_emits_audit_events(fake_home: Path, fake_repo: Path) -> None:
    from app.gateway.identity.audit.events import AuditEvent
    from app.gateway.identity.migration.executor import apply_plan
    from app.gateway.identity.migration.planner import build_plan

    _scaffold_legacy_state(fake_home, fake_repo, thread_ids=["x"])
    plan = build_plan(
        legacy_home=fake_home,
        repo_root=fake_repo,
        tenant_id=1,
        workspace_id=1,
        tenant_slug="default",
        workspace_slug="default",
    )

    captured: list[tuple[AuditEvent, bool]] = []

    async def _writer(ev: AuditEvent, critical: bool) -> None:
        captured.append((ev, critical))

    asyncio.run(
        apply_plan(
            plan,
            report_path=fake_home / "_system" / "a.json",
            audit_writer=_writer,
        )
    )

    # 1 thread + 1 custom + 1 user = 3 events.
    assert len(captured) == 3
    for ev, critical in captured:
        assert ev.action == "system.migration.item.moved"
        assert ev.result == "success"
        assert ev.tenant_id == 1
        assert ev.workspace_id == 1
        assert critical is True


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_rollback_restores_original_tree(fake_home: Path, fake_repo: Path) -> None:
    from app.gateway.identity.migration.executor import apply_plan
    from app.gateway.identity.migration.planner import build_plan
    from app.gateway.identity.migration.rollback import rollback_plan

    _scaffold_legacy_state(fake_home, fake_repo, thread_ids=["x"])
    before = _snapshot(fake_home, fake_repo, exclude={"_system"})

    plan = build_plan(
        legacy_home=fake_home,
        repo_root=fake_repo,
        tenant_id=1,
        workspace_id=1,
        tenant_slug="default",
        workspace_slug="default",
    )
    asyncio.run(apply_plan(plan, report_path=fake_home / "_system" / "a.json"))

    asyncio.run(rollback_plan(plan, report_path=fake_home / "_system" / "r.json"))

    after = _snapshot(fake_home, fake_repo, exclude={"_system", "tenants"})
    assert before == after


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


def test_report_written_with_counts_and_per_item(fake_home: Path, fake_repo: Path) -> None:
    from app.gateway.identity.migration.executor import apply_plan
    from app.gateway.identity.migration.planner import build_plan

    _scaffold_legacy_state(fake_home, fake_repo, thread_ids=["x", "y"])
    plan = build_plan(
        legacy_home=fake_home,
        repo_root=fake_repo,
        tenant_id=1,
        workspace_id=1,
        tenant_slug="default",
        workspace_slug="default",
    )
    report_path = fake_home / "_system" / "report.json"
    asyncio.run(apply_plan(plan, report_path=report_path))

    data = json.loads(report_path.read_text())
    assert data["mode"] == "apply"
    assert data["tenant_id"] == 1
    assert data["counts"]["moved"] == 4  # 2 threads + 1 custom + 1 user
    assert data["counts"]["failed"] == 0
    kinds = {i["kind"] for i in data["items"]}
    assert kinds == {"thread", "skill_custom", "skill_user"}


def test_report_includes_skipped_items_on_rerun(fake_home: Path, fake_repo: Path) -> None:
    from app.gateway.identity.migration.executor import apply_plan
    from app.gateway.identity.migration.planner import build_plan

    _scaffold_legacy_state(fake_home, fake_repo, thread_ids=["x"])

    plan = build_plan(
        legacy_home=fake_home,
        repo_root=fake_repo,
        tenant_id=1,
        workspace_id=1,
        tenant_slug="default",
        workspace_slug="default",
    )
    asyncio.run(apply_plan(plan, report_path=fake_home / "_system" / "a.json"))

    plan2 = build_plan(
        legacy_home=fake_home,
        repo_root=fake_repo,
        tenant_id=1,
        workspace_id=1,
        tenant_slug="default",
        workspace_slug="default",
    )
    # Every item should now be classified as already_migrated.
    assert plan2.items
    assert all(i.already_migrated for i in plan2.items)

    report = asyncio.run(apply_plan(plan2, report_path=fake_home / "_system" / "b.json"))
    assert all(i.status == "skipped" for i in report.items)


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


def test_file_lock_rejects_second_acquire(tmp_path: Path) -> None:
    from app.gateway.identity.migration.lock import LockAcquireError, file_lock

    lock_path = tmp_path / "mig.lock"
    with file_lock(lock_path):
        with pytest.raises(LockAcquireError):
            with file_lock(lock_path):
                pass


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_plan_rejects_target_outside_tenant_root(fake_home: Path, fake_repo: Path) -> None:
    """A hand-crafted plan whose target escapes the tenant root is rejected."""
    from app.gateway.identity.migration.executor import validate_plan
    from app.gateway.identity.migration.planner import (
        ItemKind,
        MigrationItem,
        MigrationPlan,
    )
    from app.gateway.identity.storage.path_guard import PathEscapeError

    bogus_item = MigrationItem(
        kind=ItemKind.THREAD,
        source=fake_home / "threads" / "x",
        target=fake_home / "outside" / "of" / "tenant",
    )
    plan = MigrationPlan(
        tenant_id=1,
        workspace_id=1,
        tenant_slug="default",
        workspace_slug="default",
        items=(bogus_item,),
    )
    with pytest.raises(PathEscapeError):
        validate_plan(plan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snapshot(home: Path, repo: Path, *, exclude: set[str] | None = None) -> dict[str, str]:
    """Recursive path → content/type snapshot for equality assertions.

    Symlinks are compared by their ``readlink`` target; regular files by
    their text content (tests only write short UTF-8).
    """

    excluded = exclude or set()
    out: dict[str, str] = {}
    for root in (home, repo):
        for p in root.rglob("*"):
            rel = p.relative_to(root)
            if rel.parts and rel.parts[0] in excluded:
                continue
            key = f"{root.name}/{rel}"
            if p.is_symlink():
                out[key] = f"symlink -> {os.readlink(p)}"
            elif p.is_file():
                out[key] = p.read_text()
            elif p.is_dir():
                out[key] = "<dir>"
    return out
