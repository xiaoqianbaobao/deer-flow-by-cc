"""Shared path resolution for thread virtual paths (e.g. mnt/user-data/outputs/...)."""

from pathlib import Path

from fastapi import HTTPException

from deerflow.config.paths import get_paths


def resolve_thread_virtual_path(
    thread_id: str,
    virtual_path: str,
    *,
    tenant_id: int | None = None,
    workspace_id: int | None = None,
) -> Path:
    """Resolve a virtual path to the actual filesystem path under thread user-data.

    Args:
        thread_id: The thread ID.
        virtual_path: The virtual path as seen inside the sandbox
                      (e.g., /mnt/user-data/outputs/file.txt).
        tenant_id: Optional tenant ID (M4 storage isolation). When both
            ``tenant_id`` and ``workspace_id`` are positive ints, the resolver
            returns the tenant-stratified host path under
            ``tenants/{tid}/workspaces/{wid}/threads/.../user-data``. Legacy
            callers omit both and keep the existing
            ``threads/{thread_id}/user-data`` behaviour.
        workspace_id: Optional workspace ID (pair with ``tenant_id``).

    Returns:
        The resolved filesystem path.

    Raises:
        HTTPException: If the path is invalid or outside allowed directories.
    """
    try:
        return get_paths().resolve_virtual_path(
            thread_id,
            virtual_path,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
    except ValueError as e:
        status = 403 if "traversal" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e))
