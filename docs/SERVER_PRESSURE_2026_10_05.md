# Server pressure reduction, October 5, 2026

Telemetry authentication, acceptance and live-state writes remain on the request
path. Command delivery and acknowledgements are unchanged. Configuration/event
maintenance and analytics are coalesced; no feature is disabled by this patch.

## Changes

- Configuration reads no longer create tables. Serialized startup migrations
  own schema setup. Startup also checks capacity-table availability when that
  feature is enabled, including deployments whose base schema marker is current.
- Configuration updates compare stored values and skip unchanged writes.
- Routine telemetry invalidates live snapshot caches, including their capacity
  and legacy keys, without discarding analytics before its TTL expires.
- Workers reuse fresh persisted analytics. A filesystem lease prevents parallel
  builds for the same window. Contending requests wait at most one second before
  using the existing fallback response. History is not arbitrarily truncated.
- Telemetry background thread creation is bounded per process. Latest pending
  payloads are retained, devices receive turns, and database failures retain work
  for retry. Cross-process leases coordinate both exclusion and a 30-second
  cooldown. Pump, sensor and fault-state changes bypass that cooldown. An
  unavailable lease directory falls back to process-local coordination.
- Dashboard summary persistence has one retry owner, with three attempts rather
  than nested three-by-four retries.
- With latest-state storage enabled, routine legacy history is sampled at up to
  30-second intervals. Motor, sensor, fault, firmware and runtime-counter changes,
  and level movements of at least 0.25 percentage points, are stored immediately.
  Latest state is written for every accepted report in the same transaction.
  Without latest-state storage, every legacy report is retained.
- Browser local-sync requests defer optional postprocessing just like device
  telemetry and Android local-sync requests.

## Deploy

Upload all three runtime files together into the cPanel application root:

```text
flask_app/server.py
flask_app/background_lease.py
flask_app/history_sampling.py
```

Keep existing credentials, device keys, secret key and source-mode settings.
Restart the Python application once. The schema revision is
`2026-10-05-server-pressure-v1`.

The configuration and background improvements apply without new feature flags.
To additionally sample routine history while keeping live status fresh, merge
these settings into the existing application environment before that restart:

```dotenv
FEATURE_CAPACITY_SCHEMA=true
FEATURE_LATEST_STATE_WRITES=true
FEATURE_LEGACY_TANK_DATA_WRITES=true
LEGACY_HISTORY_MIN_INTERVAL_SECONDS=30
BACKGROUND_DB_MAX_WORKERS=1
ANALYTICS_CACHE_TTL_SECONDS=300
DASHBOARD_SUMMARY_RECONCILIATION_ENABLED=false
MYSQL_OPTIMIZE_ENABLED=false
```

Do not enable narrow-history, archival or ingestion rollout flags merely to use
this patch. Setting `LEGACY_HISTORY_MIN_INTERVAL_SECONDS=0` restores every-report
history while retaining latest-state storage. Retention still requires the
existing admin/cron maintenance; the configured row cap is not enforced by each
telemetry request.

## Verify on hosting

Check a device's tank level and last-seen timestamp, deliver and acknowledge a
pump command, change saved thresholds, verify fault and recovery events, and open
analytics. Compare MySQL connection errors, latency and account resource faults
over equivalent traffic intervals. Optional analytics may lag by its configured
TTL; live telemetry and commands do not use that TTL.

The local checks include real MySQL configuration/live-state integration and
cross-process lease contention. No hosted deployment or load test was performed.
This patch cannot prevent a hosting supervisor from sending SIGTERM.

Local validation: 152 focused tests passed, covering MySQL integration, command queues, fault/activity events, analytics fallback, sampling, leases, and transaction recovery. A separate older threshold static suite has pre-existing expectations for removed activity-template variables and an obsolete schema marker; those unrelated assertions were not rewritten in this change. The full repository suite is not certified.
