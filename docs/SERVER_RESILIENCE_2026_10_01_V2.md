# Server resilience release v2

## Evidence from the latest log

The recovery policy deployed successfully at 21:02 IST. Local Unix socket `/var/lib/mysql/mysql.sock` subsequently connected successfully several times. Event writes encountered lock-wait error 1205 and spent about 22 seconds retrying before failing. Seven worker processes later received signal 15; this is an externally requested termination, not a Python traceback. The excerpt cannot identify who requested those terminations.

## Repairs

- Prefer an accessible local MySQL Unix socket immediately for eligible `localhost:3306` connections, rather than waiting for each new worker's TCP connection to reset. Remote hosts, alternate ports, Windows and configured TLS-CA connections retain their original transport. Retry a transient socket reset on the same socket; fall back to TCP if an automatically selected socket is unavailable. An explicit socket setting remains authoritative.
- Commit event writes in bounded chunks (32 rows by default, configurable with `DEVICE_EVENT_WRITE_BATCH_ROWS`, bounded to 1–100). Retry only the failed transaction chunk, up to three attempts. This reduces time holding event locks during large snapshot batches. Batches can partially commit; deterministic event keys allow later synchronization to upsert the same events safely.
- Handle uncaught transient database errors as HTTP 503 with `Retry-After: 5` and `Cache-Control: no-store`. Responses do not expose connection configuration and do not claim that failed writes succeeded. Authentication/HTTP errors and unrelated bugs retain their appropriate error status. Flask's supported error-handler mechanism is used: [Flask documentation](https://flask.palletsprojects.com/en/stable/errorhandling/).
- Supply the pump threshold contract through template globals, preventing a device-detail rendering error when a caller omits the optional context value.
- Identify FTPS log-watcher errors by exception type, including empty-message exceptions. A watcher disconnection is separate from a server request failure.
- Preserve earlier fixes for transaction-safe reconnects, pool lease cleanup, failed session cleanup, bounded command deadlock retries, and disabled-by-default automatic table optimization.

## Deploy

Validation: 84 targeted tests passed, including database-outage responses and subsequent worker usability, bounded event commits and deadlock recovery, socket selection/recovery, transaction safety, command queue behavior, device-detail rendering, cPanel configuration, retention and production preflight. Whitespace and Python syntax validation passed. Unix socket behavior was simulated locally; production socket availability is evidenced by the supplied log.

Upload the following runtime files from `deploy/server-resilience-2026-10-01-v2.zip` into the existing cPanel application root, preserving paths:

```text
flask_app/server.py
flask_app/mysql_transport.py
flask_app/database_availability.py
scripts/watch_stderr_ftps.py
```

The watcher change applies to the local workstation too. The archive contains no secrets or production environment file. Keep the production secret key and credentials unchanged. Keep `MYSQL_OPTIMIZE_ENABLED=false`. You may explicitly set `MYSQL_UNIX_SOCKET=/var/lib/mysql/mysql.sock` on this host, whose log confirms that path works. Auto-discovery otherwise selects it without needing an environment change. Restart the application once after uploading the runtime modules.

This release was prepared locally; no hosted files or hosting settings were changed by the agent.

## Verify and hosting boundary

Startup should log `MySQL recovery policy: socket-first-v2; bounded-event-transactions-v2`, followed by `MySQL local transport selected: Unix socket ...` when eligible. Check telemetry, command polling, and the device-detail page. Compare lock-wait failures, resets and cPanel I/O faults over similar traffic intervals.

No application patch can prevent Passenger/the host from sending SIGTERM or guarantee that the database service never fails. If process terminations continue, ask the host to identify their source from Passenger/LiteSpeed/service logs. The resource report records I/O faults but no CPU/memory/process faults; it does not expose database governor limits or MySQL service restarts.
