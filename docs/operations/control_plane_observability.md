# Control-plane observability runbook

The local control plane exposes Prometheus text at `GET /metrics` from both the stdlib
server used by Compose and the FastAPI adapter. The endpoint intentionally has no user
identity requirement so a scraper can read it. It must remain on a trusted internal
network or behind a proxy allowlist; it is not a public product API.

## Primary signals

- `pilot107_metrics_scrape_error`: any durable queue or Worker metrics source could not be
  read. Treat `1` as an observability failure; the response never includes the source error
  or file content.
- `pilot107_outbox_messages{topic,state}`: current queue depth. A `dead_letter` state needs
  operator review before replay or data repair.
- `pilot107_outbox_due_pending`: messages ready for a Worker now.
- `pilot107_outbox_expired_running`: work whose lease expired and has not yet been reclaimed.
- `pilot107_outbox_attempts` and `pilot107_outbox_reclaims`: current retained-message delivery
  attempts and fencing reclaims.
- `pilot107_worker_*_total`: per-worker durable counters for reconcile, submission, Evidence
  collection, diagnosis, Agent execution and remediation.
- `pilot107_worker_active`: `1` while a Worker is expected to tick. Graceful shutdown writes
  `0`; a crash leaves `1`, allowing stale-tick alerting without normal scale-down noise.
- `pilot107_worker_last_tick_age_seconds`: Worker freshness. The Compose healthcheck rejects
  age above 60 seconds, telemetry errors, inactive state and schema mismatch.
- `pilot107_api_requests_total` and `pilot107_api_request_duration_seconds_*`: process-local
  request count/status/duration. Object IDs are replaced with route placeholders.

Alert rules are in `config/observability/pilot107-alerts.yml`. They cover metric-source
failure, stale active Workers, expired leases, dead letters and sustained API 5xx ratio.

## Triage order

1. Check `pilot107_metrics_scrape_error` and API/Worker container health.
2. For a stale active Worker, inspect the latest health payload and container exit/restart
   history. Do not mark it stopped unless the deployment intentionally removed that Worker.
3. For expired work, allow a healthy Worker to reclaim it and verify fencing token increases.
4. For a dead letter, correlate topic and aggregate with Run/remediation events. Never edit
   attempts, fencing tokens or terminal events merely to clear an alert.
5. Preserve `X-Request-ID`, Run ID, Job ID and remediation Session ID when collecting evidence.

Health and cumulative Worker files are atomically published with mode `0600`. Error messages,
outbox `last_error`, Run events and remediation audit events pass through conservative secret
redaction before persistence. Fencing tokens and LLM token-count fields are explicitly retained
because they are audit counters, not credentials.

## Current limits

- API counters reset with the API process; a Prometheus-compatible collector is responsible for
  long-term retention.
- LLM provider latency/token and open SSE connection gauges are not yet separate metrics.
- `X-Request-ID` is returned and domain IDs exist in their event streams, but a single persisted
  request-to-Run/Job/Session trace record is still pending.
- PostgreSQL `outbox_metrics()` has backend parity code and the shared contract. The latest local
  rerun could not start its disposable image because the registry returned EOF; do not present
  that skipped live cell as passed.
