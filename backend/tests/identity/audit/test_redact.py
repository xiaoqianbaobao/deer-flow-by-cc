"""Redaction rules for audit metadata."""

from __future__ import annotations

from app.gateway.identity.audit.redact import redact_metadata


def test_none_returns_empty_dict():
    assert redact_metadata("user.login.success", None) == {}


def test_sensitive_keys_masked_at_top_level():
    raw = {
        "password": "hunter2",
        "api_token": "dft_abcd",
        "client_secret": "sssh",
        "access_key": "AKIA",
        "authorization": "Bearer eyJ",
        "benign": "ok",
    }
    redacted = redact_metadata("user.login.failure", raw)
    assert redacted["password"] == "***"
    assert redacted["api_token"] == "***"
    assert redacted["client_secret"] == "***"
    assert redacted["access_key"] == "***"
    assert redacted["authorization"] == "***"
    assert redacted["benign"] == "ok"


def test_sensitive_keys_nested():
    raw = {"outer": {"nested": {"password": "x", "fine": 1}}}
    redacted = redact_metadata("action", raw)
    assert redacted["outer"]["nested"]["password"] == "***"
    assert redacted["outer"]["nested"]["fine"] == 1


def test_http_body_dropped():
    raw = {"http.body": "raw json", "path": "/api/foo", "body": "copy", "request_body": "x"}
    redacted = redact_metadata("authz.api.denied", raw)
    assert "http.body" not in redacted
    assert "body" not in redacted
    assert "request_body" not in redacted
    assert redacted["path"] == "/api/foo"


def test_command_truncation():
    long_cmd = "echo " + "a" * 600
    raw = {"command": long_cmd}
    redacted = redact_metadata("tool.called", raw)
    # Exactly 500 chars of the original + ellipsis.
    assert redacted["command"].endswith("…")
    assert len(redacted["command"]) == 501


def test_short_command_kept():
    raw = {"cmd": "ls /tmp"}
    redacted = redact_metadata("tool.called", raw)
    assert redacted["cmd"] == "ls /tmp"


def test_write_file_drops_content_keeps_path_size():
    raw = {
        "tool": "write_file",
        "args": {"path": "/mnt/user-data/outputs/x.txt", "content": "big blob of text"},
    }
    redacted = redact_metadata("tool.called", raw)
    assert redacted["args"] == {"path": "/mnt/user-data/outputs/x.txt", "size": len("big blob of text")}


def test_write_file_preserves_explicit_size():
    raw = {
        "tool": "write_file",
        "args": {"path": "/x", "size": 12345, "content": "whatever"},
    }
    redacted = redact_metadata("tool.called", raw)
    assert redacted["args"]["size"] == 12345
    assert "content" not in redacted["args"]


def test_list_of_dicts_scrubbed():
    raw = {"items": [{"token": "x", "name": "a"}, {"secret": "y"}]}
    redacted = redact_metadata("action", raw)
    assert redacted["items"][0] == {"token": "***", "name": "a"}
    assert redacted["items"][1] == {"secret": "***"}
