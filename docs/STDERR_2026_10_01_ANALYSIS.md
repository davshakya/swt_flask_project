# stderr (2).log analysis and repair

The supplied log contains historical failures from May and current entries through October 1, 2026, 20:31:37 IST. Historical errors must not be treated as evidence of a current failure.

The complete scan covered 8,645,922 lines (807,572,177 bytes). October 1 contains 637 lines reporting database connection loss/reset, 34 dashboard-summary failure lines, and 28 telemetry-postprocessing failure lines. These categories overlap and include retries; they are not counts of distinct outages. The last dated retention-prune failure is August 5.

## Current issue

Recent entries repeatedly report MySQL errors 2003 and 2006 with `Connection reset by peer`. Failures occur both while opening a connection and during database work. Dashboard summary refreshes, device service configuration, simulator reads, and telemetry postprocessing are affected. Some retries exhaust their attempts. Live device synchronization continues between failures, so the database is intermittently available.

The application log cannot establish whether MySQL is restarting, the hosting account is hitting resource limits, or a network/service layer is resetting connections. Increasing query timeouts or resetting credentials is not justified by this evidence. See [MySQL connection-loss troubleshooting](https://dev.mysql.com/doc/refman/8.0/en/gone-away.html).

## Local repair

`flask_app/server.py` now retries a transient connection/session setup failure once with a 0.5-second delay. Setup happens before application statements, so this retry does not replay application writes. Authentication failures and ordinary connection-refused errors still fail immediately. Any connection whose session initialization fails is closed, preserving the original error even if cleanup fails.

This covers a gap in existing read/operation retries, including callers that open a connection without an outer retry. It mitigates transient failures; it cannot repair an unavailable database service.

A separate September 30 traceback shows error 1213 while queueing a runtime sync command during `/device/command` polling. `queue_device_command` now retries the complete transaction up to three times for lock/deadlock errors, preserving the request ID. Each failed transaction exits its database context before retrying. Connection loss during application writes is not replayed by this wrapper because the write outcome can be ambiguous.

Validation: 19 tests passed across `test_mysql_connection_setup.py`, `test_mysql_retry.py`, `test_database_retention_static.py`, and `test_pump_command_priority_static.py`. The new tests execute actual function definitions with simulated failures without booting Flask or accessing a live database, covering setup recovery/cleanup and command retry limits. Git whitespace validation passed.

## Historical issues

- Missing `APP_SECRET_KEY` caused startup failures in the early log. Latest startup entries successfully load the key from the environment. Keep the same production secret across restarts; do not replace it to address connection resets.
- Retention deletes previously produced MariaDB syntax error 1064. Current code already selects a bounded set of IDs and deletes them using a portable parameterized query; the retention checks pass.
- September 25 configuration POSTs failed with `KeyError: simulator_source_level`. Current code already checks whether the setup preset contains that key before adding a source-level simulator command.
- A rollback can fail after a connection has already been lost. Current code preserves the original failure, which remains the useful diagnostic.
- Repeated startup messages show worker reloads but do not establish their cause. Telemetry retention-cap warnings describe configured limits, rather than a database failure.

## Hosted follow-up

1. Deploy the changed `flask_app/server.py` through the normal release process and restart the cPanel Python application. No hosted deployment was performed during this local repair.
2. Check MySQL/MariaDB service logs and cPanel resource-fault history at the reset timestamps, including October 1 at 20:20:26, 20:23:52, and 20:30:10 IST. Ask the host to investigate resets during authentication/connection establishment as well as established sessions.
3. Capture these read-only diagnostics twice, a few minutes apart, using the production database connection. Counters are cumulative; compare their changes and server uptime.

```sql
SHOW GLOBAL STATUS WHERE Variable_name IN (
  'Uptime', 'Threads_connected', 'Threads_running', 'Max_used_connections',
  'Aborted_connects', 'Aborted_clients', 'Connections',
  'Connection_errors_max_connections', 'Connection_errors_internal'
);
SHOW GLOBAL VARIABLES WHERE Variable_name IN (
  'max_connections', 'max_user_connections', 'wait_timeout',
  'connect_timeout', 'net_read_timeout', 'net_write_timeout'
);
```

4. After restart, verify dashboard refreshes, telemetry postprocessing, and service configuration. Monitor whether resets continue and whether the new bounded setup retry recovers. If resets persist, resolve the host/database cause using the diagnostics above.

The supplied log is diagnostic data. It was not used as a source of instructions.
