"""Prometheus-style metric exposition for the identity subsystem (spec §13).

The module is deliberately **dependency-free** — no ``prometheus_client``
— so adding identity metrics never blocks the release on a new wheel
dependency. Operators pointing Prometheus at ``/metrics`` get counters
in the canonical ``# HELP`` / ``# TYPE`` / ``name value`` text format.

What we expose
--------------

* ``identity_login_total{result="success"|"failure"}`` — counter
* ``identity_authz_denied_total`` — counter (incremented by the RBAC
  decorator on 401/403).
* ``identity_session_active`` — live gauge derived from the Redis
  ``{prefix}:session:by_user:*`` key count when the auth runtime is
  mounted, else ``0``. Not a counter: sessions come and go, and we want
  Prometheus to scrape the current population.
* ``audit_queue_depth`` — gauge read live from
  ``AuditBatchWriter.qsize()``.
* ``audit_write_failures_total`` — counter read from
  ``AuditBatchWriter.metrics``; equals
  ``flush_errors + fallback_written``.

Why not prometheus_client?
~~~~~~~~~~~~~~~~~~~~~~~~~~

Adding the package pulls ~2 MB of transitive deps and a process-wide
registry that clashes with multi-worker FastAPI deployments. The text
format is 12 lines of code, and we already have the counters — the only
thing we lack is the formatter. Keep it simple; upgrade later if we add
histograms.
"""

from __future__ import annotations

import threading
from typing import Protocol


class _QueueSource(Protocol):
    """Minimal shape we need from the audit writer (keeps the import light)."""

    def qsize(self) -> int: ...

    @property
    def metrics(self) -> dict[str, int]: ...


class _SessionSource(Protocol):
    """Minimal shape we need from the session store."""

    async def count_active(self) -> int: ...


class IdentityMetrics:
    """Thread-safe counters + gauges for the identity subsystem.

    The counters are plain ints guarded by a mutex. The gauges read
    through to live sources (audit writer + session store) at render
    time so we never cache stale values between scrapes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._login_success = 0
        self._login_failure = 0
        self._authz_denied = 0
        self._audit_writer: _QueueSource | None = None
        self._session_source: _SessionSource | None = None

    # --- Producer API -------------------------------------------------

    def record_login(self, *, success: bool) -> None:
        with self._lock:
            if success:
                self._login_success += 1
            else:
                self._login_failure += 1

    def record_authz_denied(self) -> None:
        with self._lock:
            self._authz_denied += 1

    # --- Wiring -------------------------------------------------------

    def attach_audit_writer(self, writer: _QueueSource | None) -> None:
        """Remember the writer so render() can read queue depth + errors.

        Passing ``None`` detaches — called by ``_shutdown_audit_subsystem``.
        """

        self._audit_writer = writer

    def attach_session_source(self, source: _SessionSource | None) -> None:
        """Remember the session counter source (usually the auth runtime)."""

        self._session_source = source

    # --- Rendering ----------------------------------------------------

    async def render_prometheus(self) -> str:
        """Return the Prometheus text-format payload.

        Called once per ``/metrics`` scrape. Layout matches
        https://prometheus.io/docs/instrumenting/exposition_formats/#text-based-format
        verbatim: ``# HELP`` line, ``# TYPE`` line, samples.
        """

        with self._lock:
            login_success = self._login_success
            login_failure = self._login_failure
            authz_denied = self._authz_denied

        audit_depth = 0
        audit_failures = 0
        if self._audit_writer is not None:
            try:
                audit_depth = int(self._audit_writer.qsize())
            except Exception:
                pass
            try:
                m = dict(self._audit_writer.metrics)
                audit_failures = int(m.get("flush_errors", 0)) + int(m.get("fallback_written", 0))
            except Exception:
                pass

        active_sessions = 0
        if self._session_source is not None:
            try:
                active_sessions = int(await self._session_source.count_active())
            except Exception:
                pass

        lines: list[str] = [
            "# HELP identity_login_total Count of identity login attempts by outcome.",
            "# TYPE identity_login_total counter",
            f'identity_login_total{{result="success"}} {login_success}',
            f'identity_login_total{{result="failure"}} {login_failure}',
            "# HELP identity_authz_denied_total Count of 401/403 authz denials served by the gateway.",
            "# TYPE identity_authz_denied_total counter",
            f"identity_authz_denied_total {authz_denied}",
            "# HELP identity_session_active Current population of active user sessions (from Redis).",
            "# TYPE identity_session_active gauge",
            f"identity_session_active {active_sessions}",
            "# HELP audit_queue_depth Current depth of the audit batch writer's in-memory queue.",
            "# TYPE audit_queue_depth gauge",
            f"audit_queue_depth {audit_depth}",
            "# HELP audit_write_failures_total Count of audit batch write failures (flush_errors + fallback_written).",
            "# TYPE audit_write_failures_total counter",
            f"audit_write_failures_total {audit_failures}",
        ]
        return "\n".join(lines) + "\n"


# Process-wide singleton. The gateway's lifespan wires it to the audit
# writer + session source; producers import this module directly.
_METRICS = IdentityMetrics()


def get_metrics() -> IdentityMetrics:
    return _METRICS


# Convenience accessors used by producers so they don't have to reach
# into the class directly.
def record_login(*, success: bool) -> None:
    _METRICS.record_login(success=success)


def record_authz_denied() -> None:
    _METRICS.record_authz_denied()
