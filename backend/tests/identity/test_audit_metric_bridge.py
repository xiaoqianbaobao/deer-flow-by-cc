"""Verify that audit-middleware actions bump the identity metric counters.

The AuditMiddleware ``dispatch`` calls ``_emit_identity_metric(action)``
after every successful enqueue. We test that helper directly so the
assertion doesn't depend on spinning up a FastAPI app.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("IDENTITY_TEST_BACKEND", "off")


@pytest.fixture
def fresh_metrics(monkeypatch: pytest.MonkeyPatch):
    """Swap the process-wide singleton so tests don't cross-pollute."""

    from app.gateway.identity.metrics import IdentityMetrics

    m = IdentityMetrics()
    monkeypatch.setattr("app.gateway.identity.metrics._METRICS", m)
    return m


@pytest.mark.asyncio
async def test_login_success_action_bumps_counter(fresh_metrics) -> None:
    from app.gateway.identity.audit.middleware import _emit_identity_metric

    _emit_identity_metric("user.login.success")
    body = await fresh_metrics.render_prometheus()
    assert 'identity_login_total{result="success"} 1' in body
    assert 'identity_login_total{result="failure"} 0' in body


@pytest.mark.asyncio
async def test_login_failure_action_bumps_counter(fresh_metrics) -> None:
    from app.gateway.identity.audit.middleware import _emit_identity_metric

    _emit_identity_metric("user.login.failure")
    body = await fresh_metrics.render_prometheus()
    assert 'identity_login_total{result="failure"} 1' in body


@pytest.mark.asyncio
async def test_authz_denied_action_bumps_counter(fresh_metrics) -> None:
    from app.gateway.identity.audit.middleware import _emit_identity_metric

    _emit_identity_metric("authz.api.denied")
    _emit_identity_metric("authz.tool.denied")
    body = await fresh_metrics.render_prometheus()
    assert "identity_authz_denied_total 2" in body


@pytest.mark.asyncio
async def test_unrelated_action_is_noop(fresh_metrics) -> None:
    from app.gateway.identity.audit.middleware import _emit_identity_metric

    _emit_identity_metric("http.get")
    _emit_identity_metric("thread.created")
    body = await fresh_metrics.render_prometheus()
    assert 'identity_login_total{result="success"} 0' in body
    assert 'identity_login_total{result="failure"} 0' in body
    assert "identity_authz_denied_total 0" in body
