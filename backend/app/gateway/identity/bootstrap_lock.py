"""Advisory-lock wrapper for :func:`bootstrap` (spec §13 risk item).

When several gateway replicas come up simultaneously (K8s rolling deploy,
autoscaler burst), each one races to seed roles / permissions / the
default tenant. The seed inserts are idempotent by design but Postgres
can still emit ``UniqueViolation`` on a narrow window; that turns into a
startup failure.

This module wraps :func:`bootstrap` with
``pg_try_advisory_lock(hashtext('deerflow_bootstrap'))`` so only the
first replica runs the seed; the rest wait (up to a bounded timeout),
then observe the already-committed state and return instantly.

Behaviour contract
------------------

* The lock is taken on a **dedicated connection** that lives for the
  duration of the seed call, then released in a ``finally`` block.
* We use the blocking ``pg_advisory_lock`` (not ``pg_try_advisory_lock``)
  because we WANT a late replica to wait for the leader to finish; a
  non-blocking try + polling would make the caller eat a race condition
  on the next ``select`` anyway.
* Any exception inside ``bootstrap`` propagates after the lock is
  released, so a failing leader does not wedge the cluster — the next
  replica inherits the lock and retries.
* If the advisory-lock acquire itself fails (unusual; typically a
  connection-pool exhaustion), we log and fall through to ``bootstrap``
  without the lock. That preserves the pre-M7 behaviour, so the worst
  case is the pre-M7 race, not a startup deadlock.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.gateway.identity.bootstrap import bootstrap as _bootstrap

logger = logging.getLogger(__name__)

__all__ = ["BOOTSTRAP_LOCK_KEY", "bootstrap_with_advisory_lock"]

# Canonical key name. Hashed to a ``bigint`` via PG ``hashtext`` so the
# actual int is deterministic across replicas and language versions.
# Namespaced distinctly from ``deerflow_migration`` (used by the M7
# migration CLI) so the two subsystems never contend.
BOOTSTRAP_LOCK_KEY: str = "deerflow_bootstrap"


async def bootstrap_with_advisory_lock(
    engine: AsyncEngine,
    session: AsyncSession,
    *,
    bootstrap_admin_email: str | None,
    key_name: str = BOOTSTRAP_LOCK_KEY,
) -> None:
    """Run :func:`bootstrap` under a Postgres advisory lock.

    ``session`` is the *seed* session; it gets the bootstrap writes.
    ``engine`` is used to open a **second, independent connection** on
    which we hold the advisory lock — the lock must live for the whole
    seed transaction, so piggy-backing on the seed session would mean
    the lock is released on ``session.commit()`` (which happens INSIDE
    ``_bootstrap``) and then a late replica would see an unlocked state
    while the leader is still settling.
    """

    async with engine.connect() as lock_conn:
        try:
            # ``pg_advisory_lock`` is blocking — perfect for the "wait
            # for the leader to finish seeding, then move on" flow.
            await lock_conn.execute(
                text("SELECT pg_advisory_lock(hashtext(:name))"),
                {"name": key_name},
            )
        except Exception:
            logger.exception("advisory lock acquire failed; running bootstrap without exclusive lock")
            await _bootstrap(session, bootstrap_admin_email=bootstrap_admin_email)
            return

        try:
            await _bootstrap(session, bootstrap_admin_email=bootstrap_admin_email)
        finally:
            try:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:name))"),
                    {"name": key_name},
                )
            except Exception:
                logger.exception("advisory unlock failed")
