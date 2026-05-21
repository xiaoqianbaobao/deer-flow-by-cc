"""Internal audit endpoint tests (M5 Task 6).

The endpoint is a stub — M6 replaces the queue with a real writer. But
the HMAC contract is load-bearing (it authenticates the LangGraph
runtime calling back into Gateway), so we pin it down with tests now.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.identity.routers.internal import (
    drain_audit_queue_for_testing,
    router,
    sign_internal_payload,
)

KEY = "audit-test-signing-key"


def _settings(*, enabled=True, key=KEY):
    from types import SimpleNamespace

    return SimpleNamespace(enabled=enabled, internal_signing_key=key)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    drain_audit_queue_for_testing()  # start clean
    with patch("app.gateway.identity.routers.internal.get_identity_settings", return_value=_settings()):
        with TestClient(app) as c:
            yield c
    drain_audit_queue_for_testing()


def _post_audit(client, payload: dict, *, ts_override: int | None = None, key_override: str | None = None):
    body = json.dumps(payload).encode("utf-8")
    sig, ts = sign_internal_payload(body, ts=ts_override, key=key_override or KEY)
    return client.post(
        "/internal/audit",
        content=body,
        headers={
            "X-Deerflow-Internal-Sig": sig,
            "X-Deerflow-Internal-Ts": ts,
            "Content-Type": "application/json",
        },
    )


def test_valid_signature_queues_event(client):
    payload = {
        "action": "authz.tool.denied",
        "tenant_id": 7,
        "user_id": 42,
        "workspace_id": 3,
        "thread_id": "thr_xyz",
        "resource": "bash",
        "outcome": "deny",
        "extra": {"missing_permission": "thread:write"},
    }
    resp = _post_audit(client, payload)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "queued"}

    queued = drain_audit_queue_for_testing()
    assert len(queued) == 1
    assert queued[0]["action"] == "authz.tool.denied"
    assert queued[0]["user_id"] == 42


def test_tampered_body_rejected(client):
    payload = {"action": "test.tampered"}
    body = json.dumps(payload).encode("utf-8")
    sig, ts = sign_internal_payload(body, key=KEY)

    # Mutate body after signing → HMAC should no longer match
    mutated = body + b" "
    resp = client.post(
        "/internal/audit",
        content=mutated,
        headers={
            "X-Deerflow-Internal-Sig": sig,
            "X-Deerflow-Internal-Ts": ts,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_wrong_key_rejected(client):
    payload = {"action": "test.wrongkey"}
    resp = _post_audit(client, payload, key_override="some-other-key")
    assert resp.status_code == 401


def test_stale_timestamp_rejected(client):
    payload = {"action": "test.stale"}
    resp = _post_audit(client, payload, ts_override=int(time.time()) - 9_999)
    assert resp.status_code == 401


def test_missing_headers_rejected(client):
    body = json.dumps({"action": "test.noheaders"}).encode("utf-8")
    resp = client.post("/internal/audit", content=body, headers={"Content-Type": "application/json"})
    # FastAPI returns 422 when required headers are missing
    assert resp.status_code == 422


def test_invalid_body_rejected(client):
    """Signature valid, but body isn't a valid AuditEventPayload."""
    body = b'{"not_action": "x"}'
    sig, ts = sign_internal_payload(body, key=KEY)
    resp = client.post(
        "/internal/audit",
        content=body,
        headers={
            "X-Deerflow-Internal-Sig": sig,
            "X-Deerflow-Internal-Ts": ts,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


def test_signing_key_not_configured_returns_503():
    app = FastAPI()
    app.include_router(router)
    drain_audit_queue_for_testing()

    with patch("app.gateway.identity.routers.internal.get_identity_settings", return_value=_settings(key=None)):
        with TestClient(app) as client:
            payload = {"action": "test.noconfig"}
            body = json.dumps(payload).encode("utf-8")
            sig, ts = sign_internal_payload(body, key="whatever")
            resp = client.post(
                "/internal/audit",
                content=body,
                headers={
                    "X-Deerflow-Internal-Sig": sig,
                    "X-Deerflow-Internal-Ts": ts,
                    "Content-Type": "application/json",
                },
            )
            assert resp.status_code == 503


def test_multiple_events_queue_in_order(client):
    for i in range(3):
        payload = {"action": f"test.seq.{i}", "user_id": i}
        resp = _post_audit(client, payload)
        assert resp.status_code == 200

    queued = drain_audit_queue_for_testing()
    assert [e["action"] for e in queued] == ["test.seq.0", "test.seq.1", "test.seq.2"]
    assert [e["user_id"] for e in queued] == [0, 1, 2]
