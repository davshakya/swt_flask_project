# Performance on the current shared hosting plan

Use `config/cpanel-tiny.env.example` as a starting profile, merging values into
the existing cPanel Python application environment. Do not replace credentials,
device keys, or schema/ingestion feature flags. Restart the application once after
deploying the changed files and environment values.

This profile enables the existing database pool: one retained connection and one
overflow connection per Python worker. With 20 workers, that means up to 20
retained sessions and 40 simultaneous sessions, in addition to other applications
and maintenance jobs. Confirm the account's database connection allowance before
enabling it. More Python workers multiply memory and database usage; the server's
48 CPUs are not the account's CPU entitlement. Keep LSAPI_CHILDREN unchanged until
the host confirms the effective worker manager and limits.

Connections are checked before reuse and recycled after 60 seconds. This reduces
connection/setup churn and rejects stale sessions; it cannot prevent a MariaDB
restart or a disconnect during an active transaction. If the provider's idle
timeout is below 60 seconds, lower the recycle setting (minimum supported: 30).
Disable FEATURE_DB_CONNECTION_POOL to return to connections opened per operation.
The pool uses separate sockets after a worker fork and drops expired connections
when returned. Snapshot cache memory is bounded to 128 entries per worker; its
two-second freshness window is unchanged. Existing command and telemetry cache
invalidation remains active.

Before and after rollout, compare the same device/dashboard traffic over at least
15 minutes: response latency, HTTP 5xx responses, MySQL 2006/2013 errors, worker
restarts, and cPanel Resource Usage memory/process/entry-process faults. Optional
FEATURE_REQUEST_TIMING=true and FEATURE_CAPACITY_METRICS=true expose the existing
process-level metrics in the protected capacity diagnostics; these values do not
represent all workers combined. Do not load-test the shared server without the
provider's agreement. If faults increase, disable pooling and restore the prior
environment values.

The actual account quotas and a before/after workload measurement are needed to
choose the best worker count or claim a throughput improvement. Avoid enabling
new schema or ingestion features as part of this configuration change.
