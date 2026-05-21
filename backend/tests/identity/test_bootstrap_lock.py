"""Unit tests for the bootstrap advisory-lock wrapper (spec §13 / M7 C.1).

These tests do not require Postgres — the lock machinery is exercised
via a fake async engine that records the SQL statements it was asked to
run. That's enough to assert:

* ``pg_advisory_lock`` is called before :func:`bootstrap`.
* ``pg_advisory_unlock`` is called after, even on exception paths.
* When the lock acquire itself fails, ``bootstrap`` still runs (the
  wrapper degrades to the pre-M7 behaviour rather than deadlocking the
  cluster).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

# Run without the DB backend — we substitute a fake engine below.
os.environ.setdefault("IDENTITY_TEST_BACKEND", "off")


class _FakeConn:
    """Minimal async context manager with ``execute`` capture."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._fail_on = fail_on

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt, params=None):
        # SQLAlchemy ``text()`` renders to str(stmt) with the bound SQL.
        sql = str(stmt)
        self.calls.append((sql, dict(params or {})))
        if self._fail_on is not None and self._fail_on in sql:
            raise RuntimeError(f"forced failure on {self._fail_on}")


class _FakeEngine:
    """Stand-in for an ``AsyncEngine`` exposing only ``.connect()``."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self):  # noqa: D401 — mirrors sa API shape
        return self._conn


@pytest.mark.asyncio
async def test_bootstrap_under_advisory_lock_takes_and_releases_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.identity import bootstrap_lock as module

    bootstrap_mock = AsyncMock()
    monkeypatch.setattr(module, "_bootstrap", bootstrap_mock)

    conn = _FakeConn()
    engine = _FakeEngine(conn)
    session = object()  # opaque — bootstrap mock doesn't touch it

    await module.bootstrap_with_advisory_lock(
        engine,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        bootstrap_admin_email=None,
    )

    sqls = [sql for sql, _ in conn.calls]
    assert any("pg_advisory_lock" in s for s in sqls), sqls
    assert any("pg_advisory_unlock" in s for s in sqls), sqls
    # Ordering: lock precedes unlock.
    lock_idx = next(i for i, s in enumerate(sqls) if "pg_advisory_lock" in s)
    unlock_idx = next(i for i, s in enumerate(sqls) if "pg_advisory_unlock" in s)
    assert lock_idx < unlock_idx

    bootstrap_mock.assert_awaited_once_with(session, bootstrap_admin_email=None)


@pytest.mark.asyncio
async def test_unlock_runs_even_when_bootstrap_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.identity import bootstrap_lock as module

    bootstrap_mock = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(module, "_bootstrap", bootstrap_mock)

    conn = _FakeConn()
    engine = _FakeEngine(conn)

    with pytest.raises(RuntimeError, match="boom"):
        await module.bootstrap_with_advisory_lock(
            engine,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            bootstrap_admin_email=None,
        )

    sqls = [sql for sql, _ in conn.calls]
    assert any("pg_advisory_unlock" in s for s in sqls), "unlock must run on bootstrap failure"


@pytest.mark.asyncio
async def test_lock_acquire_failure_falls_through_to_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.identity import bootstrap_lock as module

    bootstrap_mock = AsyncMock()
    monkeypatch.setattr(module, "_bootstrap", bootstrap_mock)

    conn = _FakeConn(fail_on="pg_advisory_lock")
    engine = _FakeEngine(conn)
    session = object()

    # No exception: the wrapper degrades to the pre-M7 path.
    await module.bootstrap_with_advisory_lock(
        engine,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        bootstrap_admin_email="admin@example.com",
    )

    bootstrap_mock.assert_awaited_once_with(session, bootstrap_admin_email="admin@example.com")


@pytest.mark.asyncio
async def test_uses_correct_lock_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.identity import bootstrap_lock as module

    monkeypatch.setattr(module, "_bootstrap", AsyncMock())

    conn = _FakeConn()
    engine = _FakeEngine(conn)

    await module.bootstrap_with_advisory_lock(
        engine,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        bootstrap_admin_email=None,
    )

    lock_calls = [params for sql, params in conn.calls if "pg_advisory_lock" in sql]
    assert lock_calls, "advisory lock must be attempted"
    assert lock_calls[0]["name"] == "deerflow_bootstrap"
