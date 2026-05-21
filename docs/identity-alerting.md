# Identity Alerting Runbook

Sample Prometheus alert rules for the DeerFlow identity subsystem (spec §13).
Scraped from `GET /metrics` on the gateway. Every rule in this file is a
**starting point**: tune the `for:` window and thresholds to your production
traffic before enabling.

## Available metrics

| Metric | Type | Source | Meaning |
|---|---|---|---|
| `identity_login_total{result="success"\|"failure"}` | counter | `AuditMiddleware` | login attempts by outcome |
| `identity_authz_denied_total` | counter | `AuditMiddleware` + RBAC | 401/403 denials |
| `identity_session_active` | gauge | `SessionStore.count_active` | non-revoked sessions in Redis |
| `audit_queue_depth` | gauge | `AuditBatchWriter.qsize()` | in-memory batch-queue backlog |
| `audit_write_failures_total` | counter | `AuditBatchWriter.metrics` | flush_errors + fallback_written |

The endpoint is **not authenticated** — scrape it over a Prometheus-whitelisted
network path (NetworkPolicy, VPC firewall rule, etc.). When `ENABLE_IDENTITY=false`
the route is absent (404).

## Recommended alerts

```yaml
groups:
  - name: deerflow-identity
    interval: 30s
    rules:
      # -- Login failure spike ---------------------------------------------
      # Triggers when the failure rate exceeds 30/min sustained over 5
      # minutes. Excludes maintenance-window load tests by requiring the
      # *ratio* is also > 50%.
      - alert: DeerFlowLoginFailureSpike
        expr: |
          increase(identity_login_total{result="failure"}[5m]) > 30
          and
          increase(identity_login_total{result="failure"}[5m])
          / clamp_min(increase(identity_login_total[5m]), 1) > 0.5
        for: 5m
        labels:
          severity: page
          component: identity
        annotations:
          summary: "Identity login-failure rate exceeds threshold"
          description: |
            More than 30 failed logins in 5 minutes and failures are over
            50% of total attempts. Likely: credential stuffing, IdP
            outage, or misconfigured OIDC callback URL.
          runbook: docs/identity-release-checklist.md#login-failure

      # -- Persistent authz denials ----------------------------------------
      # 401/403 denials are expected during normal operation. We alert
      # only if the rate stays elevated for 15 minutes, which usually
      # points at a missing permission grant after a deploy.
      - alert: DeerFlowAuthzDeniedElevated
        expr: rate(identity_authz_denied_total[15m]) > 2
        for: 15m
        labels:
          severity: warn
          component: identity
        annotations:
          summary: "Sustained 401/403 rate above baseline"
          description: |
            401/403 responses over 2 req/s for 15m. Check the most recent
            deploy for a new `@requires(...)` tag that users weren't
            granted, or a broken session cookie rollout.

      # -- Audit queue backup ----------------------------------------------
      # The batch writer drains every second at batch_size=500. If depth
      # stays above 5000 (half of queue_max=10000) for > 2 minutes, PG is
      # slow or the writer flush loop is stuck.
      - alert: DeerFlowAuditQueueBackup
        expr: audit_queue_depth > 5000
        for: 2m
        labels:
          severity: page
          component: audit
        annotations:
          summary: "Audit batch-writer queue is backing up"
          description: |
            Queue depth above 5000 for 2+ minutes. Postgres is probably
            slow or unreachable. Critical events are still being written
            via the JSONL fallback, but non-critical events start being
            dropped once the queue hits 10000.
          runbook: docs/identity-release-checklist.md#audit-backlog

      # -- Audit write failures --------------------------------------------
      # Any growth in flush_errors + fallback_written means we are
      # actively hitting Postgres failures. Page on 5-minute delta, not
      # the absolute counter — the counter only grows.
      - alert: DeerFlowAuditWriteFailures
        expr: increase(audit_write_failures_total[5m]) > 10
        for: 5m
        labels:
          severity: page
          component: audit
        annotations:
          summary: "Audit batch writer is hitting Postgres failures"
          description: |
            More than 10 audit-write failures in 5 minutes. Critical
            events are being routed to the JSONL fallback; the backfill
            will pick them up once PG recovers, but the window of "maybe
            lost" grows with every dropped non-critical event.

      # -- Session count anomaly -------------------------------------------
      # Informational. Useful for capacity planning; not paged.
      - record: deerflow:identity_session_active:avg_5m
        expr: avg_over_time(identity_session_active[5m])
```

## Grafana starter dashboard

A minimal dashboard (json to be hand-imported) should have four panels:

1. **Logins / 5m** — `sum(increase(identity_login_total[5m])) by (result)`.
2. **Authz denials / min** — `rate(identity_authz_denied_total[1m])`.
3. **Active sessions** — `identity_session_active`.
4. **Audit queue depth** — `audit_queue_depth` and
   `increase(audit_write_failures_total[5m])` on a dual-axis.

## Tuning notes

* The alert thresholds above assume < 100 req/s to `/api/auth/oidc/*/callback`
  during steady state. If you run a large fleet (> 500 logins/s), raise the
  `DeerFlowLoginFailureSpike` threshold proportionally.
* `identity_session_active` is computed via Redis SCAN on every scrape.
  Prometheus default scrape interval is 15s; if that becomes expensive
  (> 50k sessions) move the scrape to a 60s interval on that job.
* All counters are process-local. In a multi-replica deploy, sum the
  series across replicas with `sum by(result) (...)`. Gauges
  (`identity_session_active`, `audit_queue_depth`) are per-replica and
  must NOT be summed — use `max by()` or look at each replica
  individually.
