# Flask deployment over FTPS

Run from the Flask project directory:

```powershell
python scripts/deploy_cpanel.py --dry-run
python scripts/deploy_cpanel.py --insecure-ftps --watch-seconds 45
```

The password is prompted without echoing, or read from `SWT_FTP_PASSWORD`.
The certificate bypass is necessary for the currently configured FTP host;
omit it when the hosting provider fixes its certificate. Transfers use TLS.

The default deploys changed and untracked permitted files from Git. Use `--all`
to upload all permitted project files. Deleted files are not removed remotely.
Production environment files, logs, databases and runtime data are excluded.
This deployer does not install dependencies. When requirements change, install
them through cPanel's Python application controls before deploying dependent code.

The script checks Python syntax and the existing site's health, downloads backups of files it changes, stages
and checksums uploads, then renames them into place. Multiple files are updated
sequentially; this is not a transactional or rolling deployment. It writes
`tmp/restart.txt` to restart only the Flask Passenger application. HTTPS requests
trigger the restart and must return the new deployment ID from `/health`.
The homepage and pricing page must also respond successfully.

If upload or site verification fails, the script attempts to restore applied
files from its backups and requests another application restart. It reports
rollback failures explicitly. A later log-download failure does not roll back
an already verified deployment.

If the host drops the connection during automatic rollback, restore using the
saved deployment directory once FTP access returns:

```powershell
python scripts/deploy_cpanel.py --insecure-ftps --rollback data/deployments/<deployment-id>
```

Rollback checks that remote files still match the failed deployment before
overwriting them. It refuses to overwrite later changes. It can restore files
even when the current website is down, then requests a Flask restart and checks
health. Review `rollback-report.json` for the restoration and verification result.

Each run saves a report, backups, fresh stderr entries, booking status and process
diagnostics under `data/deployments/<deployment-id>/`. The process observer runs
for 30 seconds after startup and is enabled only for the first ten minutes after
the deployment. It records process names, IDs, parent IDs and memory usage, but
does not record command arguments or environment values. Linux process visibility
depends on the hosting provider's `/proc` restrictions.

For another log snapshot without deploying or restarting:

```powershell
python scripts/collect_logs_ftps.py --insecure-ftps
```

To watch appended stderr entries:

```powershell
python scripts/watch_stderr_ftps.py --user logviewer@salewell.co.in --insecure-ftps --tail-bytes 65536
```

Neither log collection nor deployment automatically retries failed notification
channels. Interrupted email delivery needs review before resend because SMTP
may already have accepted the message.

## cPanel runtime profile

The Passenger entrypoint enables `SWT_CPANEL_RUNTIME=true`. This supplies smaller
defaults while respecting explicit values in the production environment:

| Setting | cPanel default |
| --- | --- |
| `FEATURE_DB_CONNECTION_POOL` | `true` (2 idle connections, 1 overflow per process) |
| `MYSQL_CONNECT_TIMEOUT_SECONDS` | 5 |
| `MYSQL_READ_TIMEOUT_SECONDS` | 8 |
| `MYSQL_WRITE_TIMEOUT_SECONDS` | 8 |
| `MYSQL_LOCK_WAIT_TIMEOUT_SECONDS` | 3 |
| `MYSQL_WEB_RETRY_ATTEMPTS` | 2 (maximum 3 in web requests) |
| `SLOW_REQUEST_SECONDS` | 3 |
| `RAG_TFIDF_ENABLED` | `false` |

Database socket timeouts bound individual waits; they are not an overall request
deadline. Background database jobs retain their original retry budgets. Slow and
failed responses log route rules, elapsed time and process IDs without query
strings, request bodies or credentials. Explicitly disable the pool with
`FEATURE_DB_CONNECTION_POOL=false` if required by host connection limits.

The deployment script stores Git metadata in its manifest so production workers
do not launch Git subprocesses. Without metadata, cPanel uses the existing date
version fallback. Chatbot retrieval defaults to the existing lexical search on
cPanel, reducing worker memory use. Set `RAG_TFIDF_ENABLED=true` for TF-IDF ranking;
its numerical libraries load lazily on the first search. This may change ranking
relative to lexical search. The homepage visitor counter remains atomic in MySQL.
