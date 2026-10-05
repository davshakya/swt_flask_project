# MySQL recovery follow-up: October 1, 2026

The new log proves the earlier setup retry is running, but connections still reset. The supplied cPanel report records no CPU, memory, entry-process, or process-limit faults. It records 10 I/O faults in the 20:30–21:30 interval. I/O throttling is present; the report does not establish that it caused the MySQL resets, and it does not expose MySQL service health or database-specific connection limits.

## Changes

- After a transient reset, a `localhost:3306` TCP connection can retry using an existing local MySQL Unix socket. Only actual socket files are eligible, and discovery does not redirect remote hosts, alternate ports, Windows connections, or connections with a configured TLS CA. A working socket is cached per process only after successful connection and session setup. If the cached socket disappears, recovery returns to the original TCP configuration. Credentials and database name remain the configured values.
- `MYSQL_UNIX_SOCKET` optionally pins the hosting provider's socket explicitly. An explicit setting is preserved during retries. PyMySQL supports this transport directly: [connection documentation](https://pymysql.readthedocs.io/en/latest/modules/connections.html).
- Read retries cannot reconnect after a transaction has performed a write or locking statement. The earlier reconnect behavior could lose pending writes and then commit only later work. Plain reads can still recover; failed write transactions propagate to their caller.
- Failed pooled reconnects cannot release the same pool lease twice.
- Automatic `OPTIMIZE TABLE` is now disabled by default. This matches the existing cPanel deployment guidance and prevents implicit table-rebuild I/O when the setting is omitted. An explicit `MYSQL_OPTIMIZE_ENABLED=true` still enables it; set it to `false` on this I/O-limited account. Retention deletion remains enabled.
- Startup logs now identify the recovery policy revision, independently of the older root entrypoint marker.

These changes improve application recovery and remove avoidable I/O. They do not prove or repair a MySQL service outage. A host investigation is still needed if both TCP and socket connections reset.

Validation: 47 targeted tests passed, covering transport discovery/recovery, transaction safety, pool behavior, cPanel connection settings, production preflight, retention SQL, and command priorities. Git whitespace validation passed. Linux socket behavior was simulated in tests; a live Unix-socket connection on the production host was not available in this workspace.

## Deploy

The workspace release archive `deploy/mysql-recovery-2026-10-01.zip` contains only these runtime files and this note. It contains no credentials or production environment file.

Upload these files under the configured cPanel application root, preserving paths:

```text
flask_app/server.py
flask_app/mysql_transport.py
```

Keep production credentials and the existing secret key. Set `MYSQL_OPTIMIZE_ENABLED=false` if production currently overrides it to true. Restart the Python application once after uploading both files. This local task did not deploy to or restart the hosted application.

## Verify

1. Startup should include `MySQL recovery policy: local-socket-fallback-v1; transaction-safe-read-retry-v1`.
2. On a recoverable TCP reset, a successful transport switch logs `MySQL local connection recovered using Unix socket ...`. If no accessible socket exists, the bounded retry uses TCP. Ask the host for the correct path if an explicit socket is needed.
3. Check dashboard updates, device command polling, and telemetry processing. Compare I/O faults and database resets over a similar traffic interval; a retry warning alone does not establish failure of the request.
4. If resets persist, give the host the reset timestamps and request MySQL service/restart logs, aborted connection counters, database-user connection limits, and any database governor restrictions. The account resource report alone cannot answer these questions.
