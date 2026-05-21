"""Unit tests for the Prometheus metrics exporter (M7 C.2).

The tests exercise the ``IdentityMetrics`` class directly without a live
Redis or Postgres: the audit writer and session source are stubbed with
protocol-conformant objects. This keeps the tests fast and lets them
run under ``IDENTITY_TEST_BACKEND=off``.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("IDENTITY_TEST_BACKEND", "off")


class _FakeWriter:
    """Stand-in for ``AuditBatchWriter`` — only ``qsize`` + ``metrics`` matter."""

    def __init__(self, q: int = 0, metrics: dict[str, int] | None = None) -> None:
        self._q = q
        self._m = metrics or {}

    def qsize(self) -> int:
        return self._q

    @property
    def metrics(self) -> dict[str, int]:
        return self._m


class _FakeSessions:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count_active(self) -> int:
        return self._count


@pytest.mark.asyncio
async def test_render_prometheus_includes_all_five_metrics() -> None:
    from app.gateway.identity.metrics import IdentityMetrics

    m = IdentityMetrics()
    body = await m.render_prometheus()

    for name in (
        "identity_login_total",
        "identity_authz_denied_total",
        "identity_session_active",
        "audit_queue_depth",
        "audit_write_failures_total",
    ):
        assert name in body, f"{name} missing from Prometheus payload"


@pytest.mark.asyncio
async def test_login_counters_separate_success_and_failure() -> None:
    from app.gateway.identity.metrics import IdentityMetrics

    m = IdentityMetrics()
    m.record_login(success=True)
    m.record_login(success=True)
    m.record_login(success=False)

    body = await m.render_prometheus()
    assert 'identity_login_total{result="success"} 2' in body
    assert 'identity_login_total{result="failure"} 1' in body


@pytest.mark.asyncio
async def test_authz_denied_counter_monotonic() -> None:
    from app.gateway.identity.metrics import IdentityMetrics

    m = IdentityMetrics()
    for _ in range(5):
        m.record_authz_denied()

    body = await m.render_prometheus()
    assert "identity_authz_denied_total 5" in body


@pytest.mark.asyncio
async def test_audit_depth_and_failures_read_live_from_writer() -> None:
    from app.gateway.identity.metrics import IdentityMetrics

    m = IdentityMetrics()
    writer = _FakeWriter(q=42, metrics={"flush_errors": 3, "fallback_written": 7})
    m.attach_audit_writer(writer)

    body = await m.render_prometheus()
    assert "audit_queue_depth 42" in body
    assert "audit_write_failures_total 10" in body  # 3 + 7


@pytest.mark.asyncio
async def test_writer_detach_resets_to_zero() -> None:
    from app.gateway.identity.metrics import IdentityMetrics

    m = IdentityMetrics()
    m.attach_audit_writer(_FakeWriter(q=99, metrics={"flush_errors": 99}))

    body_before = await m.render_prometheus()
    assert "audit_queue_depth 99" in body_before

    m.attach_audit_writer(None)
    body_after = await m.render_prometheus()
    assert "audit_queue_depth 0" in body_after
    assert "audit_write_failures_total 0" in body_after


@pytest.mark.asyncio
async def test_session_source_attachment() -> None:
    from app.gateway.identity.metrics import IdentityMetrics

    m = IdentityMetrics()
    m.attach_session_source(_FakeSessions(count=17))

    body = await m.render_prometheus()
    assert "identity_session_active 17" in body


@pytest.mark.asyncio
async def test_writer_errors_never_break_rendering() -> None:
    """A broken writer must never crash ``/metrics``."""

    from app.gateway.identity.metrics import IdentityMetrics

    class _Broken:
        def qsize(self):
            raise RuntimeError("boom")

        @property
        def metrics(self):
            raise RuntimeError("boom")

    m = IdentityMetrics()
    m.attach_audit_writer(_Broken())

    body = await m.render_prometheus()
    assert "audit_queue_depth 0" in body
    assert "audit_write_failures_total 0" in body


@pytest.mark.asyncio
async def test_session_counter_errors_never_break_rendering() -> None:
    from app.gateway.identity.metrics import IdentityMetrics

    class _Broken:
        async def count_active(self) -> int:
            raise RuntimeError("redis down")

    m = IdentityMetrics()
    m.attach_session_source(_Broken())

    body = await m.render_prometheus()
    assert "identity_session_active 0" in body


@pytest.mark.asyncio
async def test_output_matches_prometheus_text_format() -> None:
    """Every metric has a HELP + TYPE line before its samples."""

    from app.gateway.identity.metrics import IdentityMetrics

    m = IdentityMetrics()
    body = await m.render_prometheus()
    lines = body.splitlines()

    for metric in (
        "identity_login_total",
        "identity_authz_denied_total",
        "identity_session_active",
        "audit_queue_depth",
        "audit_write_failures_total",
    ):
        help_idx = next(i for i, line in enumerate(lines) if line.startswith(f"# HELP {metric} "))
        # The TYPE line must immediately follow HELP.
        assert lines[help_idx + 1].startswith(f"# TYPE {metric} "), f"{metric}: TYPE line must follow HELP"
