# Changelog

All notable changes to DeerFlow are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning
follows [SemVer](https://semver.org/).

## [Unreleased — v2.0.0 release candidate]

First release to carry the multi-tenant **identity foundation**. Shipped
as P0 milestones M1 through M7.

### Non-breaking changes (`ENABLE_IDENTITY=false`, default)

* Zero behaviour change from v1.x. The regression guard
  `backend/tests/identity/test_feature_flag_offline.py` confirms the
  subsystem is completely inert.
* Added `ENABLE_IDENTITY`, `DEERFLOW_DATABASE_URL`, `DEERFLOW_REDIS_URL`,
  and related env vars — all optional.

### Breaking changes (`ENABLE_IDENTITY=true`)

* **Schema**: `identity` PostgreSQL schema with 11 tables
  (tenants, users, memberships, workspaces, permissions, roles,
  role_permissions, user_roles, workspace_members, api_tokens,
  audit_logs). Seeded via idempotent bootstrap.
* **Authentication**: OIDC login flow (Okta, Azure AD, Keycloak) plus
  API tokens (`dft_*`, bcrypt at rest). Access token lives in an
  HttpOnly `deerflow_session` cookie; refresh token is server-side in
  Redis.
* **RBAC**: five built-in roles (`platform_admin`, `tenant_owner`,
  `workspace_admin`, `member`, `viewer`). Scope decorator
  `@requires(tag, scope)` gates every non-read-only route. Tenant-scope
  auto-filter is installed on the SQLAlchemy session.
* **Storage isolation**: thread / skill / memory filesystem paths move
  under `$DEER_FLOW_HOME/tenants/{tid}/...`. Legacy flat paths are
  reached through forwarder symlinks left by the migration script.
* **LangGraph identity propagation**: Gateway signs an identity header
  set (HMAC-SHA256) into every run config; LangGraph `IdentityMiddleware`
  verifies on entry. Tool authorization gate `IdentityGuardrailMiddleware`
  whitelists tools by `TOOL_PERMISSION_MAP`; unknown tools default-deny.
* **Audit pipeline**: async batch writer (max 500 rows / 1 s), JSONL
  fallback on PG outage, REVOKE UPDATE/DELETE on `identity.audit_logs`
  from the app role. Query + export API at
  `/api/tenants/{tid}/audit{,export}` and cross-tenant
  `/api/admin/audit`.
* **Migration script** at `scripts/migrate_to_multitenant.py` with
  `--dry-run` / `--apply` / `--rollback` modes. Guards against
  multi-replica runs with a file lock and a PG advisory lock
  (`deerflow_migration`).
* **Bootstrap advisory lock**: K8s-safe rolling restarts — the first
  replica to reach `bootstrap_with_advisory_lock` takes
  `pg_advisory_lock(hashtext('deerflow_bootstrap'))`; the rest wait for
  it to finish.
* **Metrics**: Prometheus text-format `/metrics` endpoint exposing
  `identity_login_total`, `identity_authz_denied_total`,
  `identity_session_active`, `audit_queue_depth`, and
  `audit_write_failures_total`. Sample alert rules in
  `docs/identity-alerting.md`.

### Added

* `app/gateway/identity/` subsystem (M1 – M6).
* `app/gateway/identity/migration/` package + `scripts/migrate_to_multitenant.py` (M7 B).
* `app/gateway/identity/bootstrap_lock.py` (M7 C.1).
* `app/gateway/identity/metrics.py` + `routers/metrics.py` (M7 C.2).
* Makefile targets: `db-upgrade`, `db-downgrade-one`, `identity-bootstrap`,
  `identity-keys`, `identity-dirs`, `identity-test`,
  `identity-migrate-{dry,apply,rollback}`.
* Docs: `docs/UPGRADE_v2.md`, `docs/identity-alerting.md`,
  `docs/identity-release-checklist.md`.

### Changed

* Gateway lifespan now initialises the identity subsystem before the
  LangGraph runtime when the flag is on (order: DB engine → bootstrap
  under advisory lock → AuthRuntime → tenant auto-filter → audit writer
  → LangGraph runtime → IM channels).
* `AuditMiddleware.dispatch` mirrors `user.login.{success,failure}` and
  `authz.*.denied` actions into the Prometheus counters.

### Known gaps (tracked for post-M7)

* **Admin UI (M7 Part A)**: 14 admin pages + Playwright E2E suite are
  still open. Frontend lands in a follow-up PR.
* **End-to-end smoke in CI**: the GitHub Actions workflow that boots
  PG + Redis and plays through a mock OIDC login is sketched in the
  release checklist but not yet committed.
* **IM channel identity threading**: `app/channels/manager.py` still
  calls `Paths.resolve_virtual_path` without identity (TODO marker).

## [v1.x — legacy single-tenant]

No formal changelog kept for v1.x. See `git log` prior to the
`docs/p0-implementation-plans` branch.
