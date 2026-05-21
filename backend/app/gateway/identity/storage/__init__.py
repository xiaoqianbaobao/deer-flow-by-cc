"""Identity storage utilities (M4).

This package contains pure path-construction helpers and path-guard utilities
used by the tenant/workspace-aware storage layout.

Only stdlib-level dependencies are permitted in this package — in particular,
nothing here should import the identity settings object or DB machinery, to
keep the layer usable from early-startup code and from the harness bridge.
"""

from app.gateway.identity.storage.config_layers import (
    SENSITIVE_GLOBAL_ONLY,
    SensitiveFieldViolation,
    load_layered_config,
    merge_config,
)
from app.gateway.identity.storage.path_guard import (
    PathEscapeError,
    assert_symlink_parent_safe,
    assert_within_tenant_root,
    safe_join,
)
from app.gateway.identity.storage.paths import (
    audit_archive_path,
    audit_fallback_path,
    deerflow_home,
    migration_lock_path,
    migration_report_path,
    skills_public_root,
    skills_tenant_custom_root,
    skills_workspace_user_root,
    tenant_root,
    tenant_shared_root,
    thread_path,
    user_memory_path,
    workspace_root,
)

__all__ = [
    "PathEscapeError",
    "SENSITIVE_GLOBAL_ONLY",
    "SensitiveFieldViolation",
    "assert_symlink_parent_safe",
    "assert_within_tenant_root",
    "audit_archive_path",
    "audit_fallback_path",
    "deerflow_home",
    "load_layered_config",
    "merge_config",
    "migration_lock_path",
    "migration_report_path",
    "safe_join",
    "skills_public_root",
    "skills_tenant_custom_root",
    "skills_workspace_user_root",
    "tenant_root",
    "tenant_shared_root",
    "thread_path",
    "user_memory_path",
    "workspace_root",
]
