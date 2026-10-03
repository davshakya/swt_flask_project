# Server log review — 2026-10-03

Read-only FTPS snapshot of the last 2 MiB of production `stderr.log`, ending
2026-10-03 10:50:35 IST. The snapshot starts mid-file and does not represent
the complete server history. It contains no Python tracebacks.

## Findings

The October 3 portion contains 419 entries mentioning a lost MySQL connection
or connection reset, six entries mentioning lock wait timeouts, and one
telemetry postprocessing failure. Counts represent log entries, including
retry messages, rather than independent incidents. Devices continued reporting
reachable/live status between failures.

The dominant failure is MySQL error 2006 (`server has gone away` / broken pipe),
with earlier error 2003 connection resets and lock wait timeouts also present.
Application logs cannot establish whether the database was restarted, killed
connections, or exhausted hosting resources. MySQL and cPanel resource logs
are needed to distinguish those causes.

The telemetry retention-cap warnings describe configured policy and are not
evidence of a failing retention operation.

## Local fixes

- The Passenger compatibility entrypoint replaced the backend retry helper
  with linear delays without jitter. Restore exponential delays with bounded
  jitter so competing workers do not retry conflicting transactions together.
- Mark a connection broken when query execution reports a recoverable
  disconnect. Discard it when released to the pool even if the caller catches
  the exception. A successful reconnect clears the broken flag.
- Preserve the existing prohibition on replaying writes after disconnects:
  a lost response does not prove the database failed to apply the write.

Validation: 31 tests passed across connection setup, transport selection,
transaction recovery, and production retry backoff. Tests simulate database
failures; they do not validate the hosted MySQL service.

## Remaining production work

These changes have not been deployed. Publishing was rejected by automatic
approval review pending explicit authorization of the GitHub destination and
payload. Log retrieval used the supplied logviewer account only for reading.

After deployment, compare new log entries with this baseline and check cPanel
process/connection limits and MySQL restart/resource logs if disconnects
continue. Keep the existing capacity rollout hold described in the deployment
guide until its recovery gates pass. No production database settings or
capacity feature flags were changed during this review.

## Follow-up: background worker crash prevention

Dashboard summary scheduling previously created one thread per device outside
the telemetry database-work semaphore. Those threads could queue behind the
summary lock and add database concurrency despite `BACKGROUND_DB_MAX_WORKERS=1`.
Summary scheduling now acquires that shared permit before starting a thread,
skips best-effort refreshes while busy, and releases the permit on completion
or launch failure. A skipped device can retry on its next event/telemetry update.
Telemetry schedules its summary after releasing its own permit so a one-worker
configuration still refreshes the dashboard.

Telemetry thread-launch failures now clear queued/running markers and are
logged instead of propagating through a request whose telemetry was already
accepted. Regression tests cover burst limits, permit contention, database
failure cleanup, thread-launch failure, and telemetry-to-summary handoff.

These fixes address application paths that amplify shared-host thread and
database pressure. They do not establish or eliminate hosting-side MySQL
restarts or worker termination. Deployment remains pending.
