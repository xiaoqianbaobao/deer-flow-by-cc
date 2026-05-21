"""Single-writer coordination for the migration run (spec §10.2).

Two independent locks protect the run:

* **File lock** — ``{home}/_system/migration.lock``. Guards against two
  concurrent invocations on the same host. Uses ``fcntl.flock`` with
  ``LOCK_EX | LOCK_NB`` so a second runner fails fast with a clear error
  rather than blocking indefinitely.

* **PG advisory lock** — ``pg_advisory_lock(hashtext('deerflow_migration'))``.
  Guards against multiple K8s replicas racing on the same database. The
  advisory lock lives for the lifetime of the session, so we hold the PG
  session open for the entire run and release it on exit.

Both locks are exposed as async context managers so the CLI can combine
them into one ``async with ...:`` block.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

__all__ = [
    "LockAcquireError",
    "file_lock",
    "pg_advisory_lock",
]

# Well-known string used by ``pg_advisory_lock`` + ``hashtext`` so the
# key is stable across runs. Must match the value referenced in the
# spec (§13 bootstrap lock uses the same family of keys).
_ADVISORY_KEY_NAME = "deerflow_migration"


class LockAcquireError(RuntimeError):
    """Raised when another runner is already holding one of the locks."""


@contextlib.contextmanager
def file_lock(path: Path):
    """Acquire an exclusive ``fcntl.flock`` on ``path``.

    The file is created if missing. On release the file is left on disk
    (truncated to zero bytes) so subsequent runs can reuse it; removing
    the file would race with a concurrent attempt to open it.

    Raises :class:`LockAcquireError` if another process currently holds
    the lock.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise LockAcquireError(f"migration lock {path!s} is held by another process") from exc
            raise
        # Leave a tiny breadcrumb so operators can see who holds it.
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"pid={os.getpid()}\n".encode())
        except OSError:
            pass
        try:
            yield path
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                logger.debug("flock unlock failed", exc_info=True)
    finally:
        os.close(fd)


@contextlib.asynccontextmanager
async def pg_advisory_lock(engine: AsyncEngine | None, *, key_name: str = _ADVISORY_KEY_NAME) -> AsyncIterator[None]:
    """Hold ``pg_advisory_lock(hashtext(key_name))`` for the ``with`` block.

    When ``engine`` is ``None`` the context manager is a no-op — useful
    for tests and for the ``--no-db`` dry-run path where the caller has
    already committed to skipping the DB check.

    On a successful acquire the lock is released in a ``finally`` block
    via ``pg_advisory_unlock`` so crashing the process still drops the
    lock as soon as the PG session closes.
    """

    if engine is None:
        yield
        return

    async with engine.connect() as conn:
        # ``pg_try_advisory_lock`` returns bool; we want an immediate
        # failure rather than hanging if another replica is migrating.
        result = await conn.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:name))"),
            {"name": key_name},
        )
        acquired = bool(result.scalar())
        if not acquired:
            raise LockAcquireError(f"pg advisory lock {key_name!r} is held by another replica")
        try:
            yield
        finally:
            try:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:name))"),
                    {"name": key_name},
                )
            except Exception:
                logger.exception("pg advisory unlock failed")
