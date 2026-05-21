"""Tests for app.gateway.identity.storage.cli (M4 task 8).

Verify the tenant-directory bootstrap helper creates the expected tree with
``0700`` permissions, ensures global dirs exist, is idempotent, and rejects
bad input.
"""

from __future__ import annotations

import pytest

from app.gateway.identity.storage import cli


def _mode(path) -> int:
    return path.stat().st_mode & 0o777


# ---------------------------------------------------------------------------
# Tenant-only tree
# ---------------------------------------------------------------------------


def test_creates_tenant_only_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    result = cli.run(tenant_id=5)

    assert result.home == tmp_path.resolve()

    custom = tmp_path / "tenants" / "5" / "custom"
    shared = tmp_path / "tenants" / "5" / "shared"
    tenant = tmp_path / "tenants" / "5"

    for p in (tenant, custom, shared):
        assert p.is_dir(), f"{p} should exist"
        assert _mode(p) == 0o700, f"{p} mode should be 0700 but is {oct(_mode(p))}"

    # No workspace directory when workspace_id not supplied.
    assert not (tmp_path / "tenants" / "5" / "workspaces").exists()


# ---------------------------------------------------------------------------
# Full tenant+workspace tree
# ---------------------------------------------------------------------------


def test_creates_full_tenant_workspace_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    cli.run(tenant_id=5, workspace_id=7)

    ws = tmp_path / "tenants" / "5" / "workspaces" / "7"
    user = ws / "user"
    threads = ws / "threads"

    for p in (ws, user, threads):
        assert p.is_dir(), f"{p} should exist"
        assert _mode(p) == 0o700, f"{p} mode should be 0700 but is {oct(_mode(p))}"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_second_run_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    first = cli.run(tenant_id=5, workspace_id=7)
    # At least the tenant + workspace dirs should have been freshly created.
    assert len(first.created) > 0

    # Drift the mode on a tenant dir — the helper should re-heal it.
    custom = tmp_path / "tenants" / "5" / "custom"
    custom.chmod(0o755)
    assert _mode(custom) == 0o755

    second = cli.run(tenant_id=5, workspace_id=7)
    # Second run should be a no-op for creation.
    assert second.created == []
    # Mode should have been re-asserted.
    assert _mode(custom) == 0o700
    for _, mode in second.preserved:
        # Every preserved tenant-scope dir must be 0700; skills/public is the
        # only 0755 entry (handled via its own mode constant).
        assert mode in (0o700, 0o755)


# ---------------------------------------------------------------------------
# Global dirs
# ---------------------------------------------------------------------------


def test_creates_global_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    cli.run(tenant_id=5)

    public = tmp_path / "skills" / "public"
    system = tmp_path / "_system"

    assert public.is_dir()
    assert _mode(public) == 0o755, f"skills/public should be 0755, got {oct(_mode(public))}"

    assert system.is_dir()
    assert _mode(system) == 0o700, f"_system should be 0700, got {oct(_mode(system))}"


# ---------------------------------------------------------------------------
# Bad input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_rejects_non_positive_tenant_id_via_run(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    with pytest.raises(ValueError):
        cli.run(tenant_id=bad)


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_rejects_non_positive_tenant_id_via_main(tmp_path, monkeypatch, capsys, bad):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    # argparse exits with SystemExit(2) on type-validation errors.
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--tenant-id", bad])
    assert excinfo.value.code == 2


def test_rejects_non_positive_workspace_id_via_main(tmp_path, monkeypatch):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--tenant-id", "1", "--workspace-id", "0"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Env var honoured
# ---------------------------------------------------------------------------


def test_uses_deer_flow_home_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    result = cli.run(tenant_id=3, workspace_id=4)
    assert result.home == tmp_path.resolve()
    assert (tmp_path / "tenants" / "3" / "custom").is_dir()
    assert (tmp_path / "tenants" / "3" / "workspaces" / "4" / "user").is_dir()


def test_explicit_home_overrides_env(tmp_path, monkeypatch):
    # Point env at one dir, pass explicit home pointing at another.
    other = tmp_path / "other"
    target = tmp_path / "target"
    monkeypatch.setenv("DEER_FLOW_HOME", str(other))
    result = cli.run(tenant_id=1, home=target)
    assert result.home == target.resolve()
    assert (target / "tenants" / "1" / "custom").is_dir()
    # Env-pointed home must not have been touched.
    assert not other.exists() or not (other / "tenants").exists()
    # And the env var must be restored after the call.
    import os

    assert os.environ["DEER_FLOW_HOME"] == str(other)


# ---------------------------------------------------------------------------
# main() smoke — happy path returns 0 and prints summary
# ---------------------------------------------------------------------------


def test_main_prints_summary_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    rc = cli.main(["--tenant-id", "9", "--workspace-id", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "home:" in out
    assert str(tmp_path) in out
    # Should list at least tenant + workspace roots.
    assert "tenants/9" in out
    assert "workspaces/2" in out
