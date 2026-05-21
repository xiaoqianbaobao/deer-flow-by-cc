"""Tests for ThreadDataMiddleware tenant-aware routing (M4 Task 5).

Validates that:
1. When ``state["identity"]`` is absent or incomplete, the middleware falls
   back to the legacy non-stratified path layout (regression guard for
   flag-off mode, preserved behaviour pre-M5).
2. When identity is present with positive tenant_id + workspace_id, the
   middleware emits tenant-stratified paths under
   ``{base_dir}/tenants/{tid}/workspaces/{wid}/threads/{thread_id}/``.
3. Identity may be either a dict or an attribute-bearing object
   (dataclass / ``SimpleNamespace``) — the middleware must handle both
   without importing the concrete ``app.gateway.identity.auth.Identity``
   type (harness boundary preserved).

Sandbox virtual prefix (``/mnt/user-data/...``) is invariant across both
modes; this middleware only controls the host-side layout.
"""

from dataclasses import dataclass
from types import SimpleNamespace

from langgraph.runtime import Runtime

from deerflow.agents.middlewares.thread_data_middleware import ThreadDataMiddleware


def _as_posix(path: str) -> str:
    return path.replace("\\", "/")


@dataclass
class _FakeIdentity:
    """Harness-local stand-in for the app-side ``Identity`` dataclass.

    We cannot import the real type without violating the harness boundary.
    The middleware reads attributes defensively, so the field names must
    match the app-side contract (``tenant_id`` / ``workspace_id``).
    """

    tenant_id: int | None
    workspace_id: int | None


class TestThreadDataMiddlewareTenantPaths:
    def test_no_identity_uses_legacy_path(self, tmp_path):
        """Regression guard: flag-off (no identity) preserves legacy layout."""
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        result = middleware.before_agent(state={}, runtime=Runtime(context={"thread_id": "t-1"}))

        paths = result["thread_data"]
        assert _as_posix(paths["workspace_path"]).endswith("threads/t-1/user-data/workspace")
        assert "tenants/" not in _as_posix(paths["workspace_path"])

    def test_identity_dict_routes_to_tenant_path(self, tmp_path):
        """Dict-shaped identity with tenant+workspace yields stratified path."""
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        state = {"identity": {"tenant_id": 5, "workspace_id": 7}}
        result = middleware.before_agent(state=state, runtime=Runtime(context={"thread_id": "t-1"}))

        paths = result["thread_data"]
        assert _as_posix(paths["workspace_path"]).endswith("tenants/5/workspaces/7/threads/t-1/user-data/workspace")
        assert _as_posix(paths["uploads_path"]).endswith("tenants/5/workspaces/7/threads/t-1/user-data/uploads")
        assert _as_posix(paths["outputs_path"]).endswith("tenants/5/workspaces/7/threads/t-1/user-data/outputs")

    def test_identity_object_routes_to_tenant_path(self, tmp_path):
        """Dataclass/SimpleNamespace identity is also accepted."""
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        for identity in (_FakeIdentity(tenant_id=5, workspace_id=7), SimpleNamespace(tenant_id=5, workspace_id=7)):
            state = {"identity": identity}
            result = middleware.before_agent(state=state, runtime=Runtime(context={"thread_id": "t-1"}))

            paths = result["thread_data"]
            assert _as_posix(paths["workspace_path"]).endswith("tenants/5/workspaces/7/threads/t-1/user-data/workspace")

    def test_identity_missing_workspace_falls_back_to_legacy(self, tmp_path):
        """Both ids are required; missing workspace_id → legacy path."""
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        state = {"identity": {"tenant_id": 5, "workspace_id": None}}
        result = middleware.before_agent(state=state, runtime=Runtime(context={"thread_id": "t-1"}))

        paths = result["thread_data"]
        assert _as_posix(paths["workspace_path"]).endswith("threads/t-1/user-data/workspace")
        assert "tenants/" not in _as_posix(paths["workspace_path"])

    def test_identity_missing_tenant_falls_back_to_legacy(self, tmp_path):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        state = {"identity": {"tenant_id": None, "workspace_id": 7}}
        result = middleware.before_agent(state=state, runtime=Runtime(context={"thread_id": "t-1"}))

        paths = result["thread_data"]
        assert _as_posix(paths["workspace_path"]).endswith("threads/t-1/user-data/workspace")
        assert "tenants/" not in _as_posix(paths["workspace_path"])

    def test_identity_none_falls_back_to_legacy(self, tmp_path):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        state = {"identity": None}
        result = middleware.before_agent(state=state, runtime=Runtime(context={"thread_id": "t-1"}))

        paths = result["thread_data"]
        assert _as_posix(paths["workspace_path"]).endswith("threads/t-1/user-data/workspace")

    def test_lazy_init_does_not_create_directories(self, tmp_path):
        """Default lazy_init=True must not touch the filesystem."""
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        state = {"identity": {"tenant_id": 5, "workspace_id": 7}}
        middleware.before_agent(state=state, runtime=Runtime(context={"thread_id": "t-1"}))

        tenant_dir = tmp_path / "tenants" / "5" / "workspaces" / "7" / "threads" / "t-1"
        assert not tenant_dir.exists()

    def test_eager_init_creates_tenant_aware_directories(self, tmp_path):
        """lazy_init=False must materialise the tenant-stratified layout."""
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=False)

        state = {"identity": {"tenant_id": 5, "workspace_id": 7}}
        middleware.before_agent(state=state, runtime=Runtime(context={"thread_id": "t-1"}))

        base = tmp_path / "tenants" / "5" / "workspaces" / "7" / "threads" / "t-1"
        assert (base / "user-data" / "workspace").is_dir()
        assert (base / "user-data" / "uploads").is_dir()
        assert (base / "user-data" / "outputs").is_dir()
        assert (base / "acp-workspace").is_dir()
        # Legacy path must NOT be created when identity is present.
        assert not (tmp_path / "threads" / "t-1").exists()

    def test_eager_init_without_identity_creates_legacy_directories(self, tmp_path):
        """Flag-off regression: eager init still targets legacy dirs."""
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=False)

        middleware.before_agent(state={}, runtime=Runtime(context={"thread_id": "t-1"}))

        base = tmp_path / "threads" / "t-1"
        assert (base / "user-data" / "workspace").is_dir()
        # Tenant tree must NOT be created when identity is absent.
        assert not (tmp_path / "tenants").exists()
