"""One-shot migration from legacy single-tenant layout to multi-tenant (spec §10.2).

The four building blocks are:

* :mod:`planner`  — enumerate sources, build a plan (pure).
* :mod:`executor` — apply the plan (side effects: ``mv`` + symlink + audit).
* :mod:`rollback` — reverse a previously-applied plan.
* :mod:`report`   — write the JSON report that documents what happened.

The CLI at ``scripts/migrate_to_multitenant.py`` is a thin wrapper that
composes these primitives, acquires the advisory + file lock, and prints
human-friendly output.
"""

from __future__ import annotations

from app.gateway.identity.migration.planner import (
    ItemKind,
    MigrationItem,
    MigrationPlan,
    build_plan,
)
from app.gateway.identity.migration.report import MigrationReport, write_report

__all__ = [
    "ItemKind",
    "MigrationItem",
    "MigrationPlan",
    "MigrationReport",
    "build_plan",
    "write_report",
]
