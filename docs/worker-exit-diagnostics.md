# Diagnose worker termination on cPanel

Flask log lines now include `pid`, `ppid`, and a generated request ID. Requests
also return `X-Request-ID` so a failing response can be matched to the logs.
Slow requests (2 seconds by default) and HTTP 5xx responses log method, route
template, status, and duration. Slow database operations (1 second by default)
and failed SQL attempts log operation type, duration, exception class, and
MySQL error code. SQL parameters, raw SQL, request bodies, and query strings
are omitted from these diagnostic messages. Retry duration can include recovery
work. A successful connection/session retry emits a recovery message.

Optional Flask `device.env` settings:

```dotenv
SWT_LOG_SLOW_REQUEST_SECONDS=2
SWT_LOG_SLOW_QUERY_SECONDS=1
```

Logging cannot reliably identify the sender of SIGTERM or execute cleanup after
SIGKILL. Use the collector below to gather independent process evidence.

Publish `scripts/diagnose_worker_exits.py` and
`scripts/collect_host_diagnostics.py` through Git. In cPanel Terminal, change to
the deployed Flask project directory and run:

```bash
python scripts/diagnose_worker_exits.py --seconds 45 --pid 1053988
```

Use the Python executable from your application's virtual environment. To
observe all worker terminations, omit `--pid`. If the log is elsewhere, supply
`--log /absolute/path/to/stderr.log`. The PID option highlights matching log
entries; all visible account processes are sampled to preserve ancestry.

The report is written after each sample to `data/worker-exits.json`. Download
it using cPanel File Manager. It includes executable paths, process names,
parent IDs, start ticks, memory, threads, process arrivals/disappearances, and
matching Passenger startup/termination log entries. It does not record command
arguments, process environment variables, configuration secrets, or unrelated
log lines. No application restart or database connection is performed.

An old, exited PID cannot be identified retroactively. Run the collector while
new terminations occur. Historical log entries and reused PIDs require care;
an observed process with the same PID is not proof of a historical identity.
Processes that disappear may have exited normally or become inaccessible.
The reporting PID is not proof of who sent SIGTERM. Ask the hosting provider
to trace signal delivery and inspect worker recycling/resource limits if the
sender or termination reason remains unknown. Restricted `/proc` access and
processes shorter than the sampling interval can limit results.
