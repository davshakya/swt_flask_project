# SaleWell Smart Tank Flask Backend

Last refreshed: `2026-05-27`

This repository contains the Flask backend for the SaleWell Smart Tank system. It receives telemetry from tank controllers, stores operational state in MySQL/MariaDB, serves the web dashboard and PWA, exposes mobile-friendly APIs, queues control commands for devices, and provides monitoring, alerting, and support tooling.

This backend is one part of the wider SaleWell IoT Solutions stack. The shared `device.env` file is designed so the firmware, Flask backend, and companion clients can use the same device identity and endpoint settings.

## Workspace Context

Within the wider workspace:

- [`../swt_firmware_project/README.md`](../swt_firmware_project/README.md) documents the ESP8266 controller that posts to `/status` and polls `/device/command`
- [`../swt_android_app_project/README.md`](../swt_android_app_project/README.md) documents the Android client that consumes `/api/mobile/*` and local firmware pages
- [`Flask_deployment_README.md`](Flask_deployment_README.md) covers cPanel / Passenger deployment for this backend
- [`../swt_test_cases_project/README.md`](../swt_test_cases_project/README.md) covers the separated API/UI/ML test suites and virtual-device tooling

## What This Project Includes

- Device telemetry ingestion through `POST /status`
- Device command delivery through `/device/command` and `/device/command/ack`
- Admin and customer login flows with separate scopes
- Browser dashboard, customer dashboard, and per-device detail pages
- PWA manifest, service worker, install prompt, mobile-friendly public pages, and customer-facing homepage
- Pricing/comparison page for Starter Wi-Fi, Home Control, Home Cloud Pro, RWA Standard, Commercial AI Pro, Dealer / Installer Kit, and Enterprise Modular plans
- Sales/demo enquiry form with backup logging, support email, customer confirmation email, and optional WhatsApp webhook delivery
- Monitoring endpoints for health, alerts, audit events, relay state, and DB summary
- Admin service controls for source tank monitoring, buzzer, LED, cloud-feed mode, and customer AI access
- Firmware artifact upload/download flow for device-scoped master/slave OTA-style updates
- Admin-managed Android APK releases with customer download and in-app update manifest
- Optional HTTP relay and notification integration support
- Optional ML-based tank level forecasting through `/ml/predict`
- Android update manifest at `/static/version.json`
- MySQL/MariaDB schema initialization for local and hosted deployment

## Customer and Sales Features

- public marketing homepage with product explanations, plan guidance, Android download link, and enquiry entry points
- guided product chatbot content for pricing, pump control, overflow, leakage, installation type, app access, and service coverage questions
- customer dashboard for tank level, pump state, alert status, events, analytics, and local sync
- admin dashboard for device registration, customer mapping, customer password reset, service flags, firmware artifacts, Android releases, reboot commands, and data-source mode
- operational monitoring for last-seen status, stale telemetry, alerts, audit log, command delivery, and database summary
- optional integrations for SMTP email, WhatsApp webhook, Slack/Telegram-style alert hooks, HTTP relay, and MQTT telemetry/command channels

## Repository Layout

| Path | Purpose |
| --- | --- |
| `server.py` | Root entrypoint and WSGI compatibility wrapper |
| `flask_app/server.py` | Main Flask application, routes, DB init, auth, telemetry, command queue, relay logic |
| `flask_app/__init__.py` | Package export for `app` |
| `flask_app/templates/` | Login, dashboard, admin, and device-detail UI templates |
| `flask_app/static/` | PWA assets, frontend JS, and fallback `version.json` for Android update checks |
| `flask_app/.env.example` | Example backend environment file |
| `device.env.example` | Example shared device identity/settings file |
| `requirements.txt` | Single dependency file for the whole project, including ML support |
| `gunicorn.conf.py` | Root wrapper that loads `flask_app/gunicorn.conf.py` |
| `Procfile` | Procfile for gunicorn-based platforms |
| `Flask_deployment_README.md` | cPanel / Passenger deployment guide |
| `scripts/` | Operational and data utility scripts |
| `docs/` | Production rollout and support guidance |
| `data/` | Default local upload/artifact storage location |
| `tests/` | Small backend-only pytest checks for startup/env parsing and optional simulator imports |

## Runtime Flow

1. A device sends telemetry to `POST /status`.
2. Flask authenticates the device using the `X-Device-Id` and `X-Device-Key` headers. The legacy `device_id` and `device_key` JSON fields remain accepted for compatibility, but new clients should use headers.
3. Telemetry is stored in MySQL, device registration is refreshed, and operational alerts can be updated.
4. The dashboard and mobile APIs read snapshot, history, analytics, alerts, and audit data from the database.
5. Browser or mobile control actions queue commands in `device_command_queue`; optional integrations can relay selected payloads when configured.
6. Devices poll `GET /device/command`, execute the command, then confirm delivery with `POST /device/command/ack`.

## Local Setup

### 1. Copy the example config files

PowerShell:

```powershell
Copy-Item flask_app\.env.example flask_app\.env
Copy-Item device.env.example device.env
```

### 2. Edit the copied files before first run

At minimum, update these values:

- `APP_SECRET_KEY`
- `DB_BACKEND=mysql`
- `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, and `MYSQL_DATABASE` or `DATABASE_URL`
- `LOGIN_PASSWORD`
- `SWT_DEVICE_ID`
- `SWT_DEVICE_API_KEY`
- `SESSION_COOKIE_SECURE=false` for local plain HTTP development

For local-only testing, also consider clearing these so telemetry is not relayed to a shared cloud target:

- `RELAY_STATUS_URLS=`
- `RELAY_COMMAND_URLS=`

### 3. Create a virtual environment and install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 4. Run the server

```powershell
python run_local.py
```

By default, the app starts on `http://localhost:8000`.

Useful first URLs:

- Admin login: `http://localhost:8000/login/admin`
- Customer login: `http://localhost:8000/login/customer`
- Health check: `http://localhost:8000/health`

## Configuration Loading

The app automatically reads configuration from:

1. `./.env`
2. `./flask_app/.env`
3. `./device.env`

Values already present in the real process environment are preserved. Among the dotenv-style files, later files override earlier ones. In practice:

- system environment variables win
- `device.env` can override values loaded from `.env` files
- the sibling test repo can layer per-device virtual-device env files on top of the shared settings for its emulator tooling

To avoid confusion, keep backend-only settings in `flask_app/.env`, shared device credentials in `device.env`, and virtual-device overrides in the sibling `swt_test_cases_project` repo.

## Important Environment Variables

### Security and sessions

- `APP_SECRET_KEY`: Flask session signing key. Set this explicitly in production.
- `LOGIN_USERNAME`: Admin username. Defaults to `admin`.
- `LOGIN_PASSWORD`: Admin password. Change this immediately.
- `RESET_ADMIN_PASSWORD_ON_BOOT`: Forces the stored admin password to be reset from `LOGIN_PASSWORD` on startup.
- `SESSION_COOKIE_SECURE`: Must be `false` for local HTTP, should be `true` for HTTPS deployments.
- `SESSION_COOKIE_SAMESITE`: Defaults to `Lax`.
- `SESSION_LIFETIME_HOURS`: Default admin/customer browser session lifetime.

### Device authentication

- `SWT_DEVICE_ID` and `SWT_DEVICE_API_KEY`: Simplest single-device/shared-device setup. Use a unique random API key with at least 32 characters.
- `SWT_DEVICE_KEYS` or `DEVICE_KEYS`: Comma-separated registry for multi-device auth.
- `DEVICE_KEYS` format: `device-a:key-a,device-b:key-b,prefix*:shared-key`
- `DEVICE_AUTH_REQUIRED`: When `1`, every device request must include valid device credentials from config or the database. Keep this enabled in production.
- `AUTO_REGISTER_DEVICE_KEYS`: When `1`, a new device with an allowed ID prefix and a strong API key is registered on its first authenticated `/status` call. Keep this `0` in production unless you intentionally want open first-contact activation.
- `AUTO_REGISTER_DEVICE_ID_PREFIXES`: Comma-separated allowed ID prefixes for auto-registration, for example `swt-`.
- `AUTO_REGISTER_DEVICE_KEY_MIN_LENGTH`: Minimum API key length for auto-registration. Use `32` or higher.
- Admins can register device credentials from `/admin/customers` without restarting Flask; the key is stored as a hash in the database.
- `SWT_DEVICE_SOURCE_MODE`: Active backend source for snapshot/history/analytics/command reads. Use `real` for MCU traffic or `virtual` when testing with the sibling repo's virtual-device runner.
- `RESET_DEVICE_SOURCE_MODE_ON_BOOT`: When `true`, Flask resets the stored source mode to `SWT_DEVICE_SOURCE_MODE` during startup. Defaults to `true` on Render and `false` locally.
- `SEED_VIRTUAL_DEVICE_ENVS`: When `true`, Flask registers devices from `SWT_FLASK_TEST_REPO/tests/virtual_device*.env` when that sibling repo is available, then falls back to local `tests/virtual_device*.env`. Defaults to `false` on Render and `true` locally.
- `PURGE_VIRTUAL_DEVICE_ENVS_ON_BOOT`: When `true`, Flask removes repo-configured virtual-device records from the database during startup. Defaults to `true` on Render and `false` locally.

To keep the key synchronized with firmware and Android local builds, run this from the workspace root:

```powershell
python scripts\sync_device_identity.py --generate-if-placeholder
```

### Storage and retention

- `DB_BACKEND`: Must be `mysql`.
- `DATABASE_URL`: Optional MySQL/MariaDB connection URL.
- `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`: MySQL connection settings when `DATABASE_URL` is blank.
- `DATA_RETENTION_DAYS`: How long telemetry is retained.
- `TELEMETRY_HISTORY_ENABLED`: If `false`, only the latest snapshot is effectively kept.
- `MAX_TELEMETRY_ROWS_PER_DEVICE`: Hard cap per device for telemetry history.
- `DEVICE_COMMAND_RETENTION_DAYS`: Retention for delivered commands.
- `OPS_ALERT_RETENTION_DAYS`: Retention for operational alerts.
- `OPS_AUDIT_RETENTION_DAYS`: Retention for audit records.
- `DB_MAINTENANCE_ENABLED`: Enables cleanup/maintenance passes.
- `DB_TARGET_SIZE_MB`: Soft database size target used by maintenance logic.
- `DB_MAINTENANCE_MIN_INTERVAL_SECONDS`: Minimum spacing between maintenance runs.
- `DB_WAL_AUTOCHECKPOINT_PAGES`: Legacy setting ignored by MySQL-only deployments.
- `TEMP_DB_SIZE_GUARD_ENABLED`: Temporary safety flag that skips post-retention maintenance when nothing was pruned and the database is still under the configured size target.
- `TEMP_HARD_DB_CAP_ENABLED`: Temporary hard-cap flag that trims the oldest telemetry rows when the database stays above the configured size target.
- `TEMP_HARD_DB_CAP_BATCH_ROWS`: Number of oldest telemetry rows to remove per hard-cap batch while preserving the newest row for each device.
- `TEMP_HARD_DB_CAP_MAX_BATCHES`: Maximum hard-cap cleanup batches to run in one pass before giving up and logging that the DB is still over target.
- `FIRMWARE_ARTIFACT_DIR`: Optional directory for admin-uploaded OTA firmware binaries. Defaults under local `data/`.
- `FIRMWARE_ARTIFACT_MAX_MB`: Maximum size accepted for an admin OTA firmware upload. Defaults to `4`.
- `ANDROID_RELEASE_DIR`: Optional directory for admin-uploaded Android APK releases. Defaults under local `data/`.
- `ANDROID_RELEASE_MAX_MB`: Maximum size accepted for an admin Android APK upload. Defaults to `128`.

### Relay and notifications

- `RELAY_STATUS_URLS`: Comma-separated telemetry relay targets. Bare origins are treated as `/status`, and same-host request loops are skipped.
- `RELAY_COMMAND_URLS`: Comma-separated remote command relay targets. Bare origins are treated as `/device/command`, and same-host request loops are skipped.
- `RELAY_VERIFY_TLS`: TLS verification for relay HTTP calls.
- `ALERT_WEBHOOK_URL`, `SLACK_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `WHATSAPP_WEBHOOK_URL`: Optional alert integrations.

### Analytics and forecasting

- `TANK_CAPACITY_LITERS`: Default tank capacity used in summaries.
- `DATA_STALE_AFTER_SECONDS`: Threshold for stale telemetry.
- `LEVEL_FORECAST_MODEL_PATH`: Optional custom path to the forecast artifact.
- `MOBILE_TOKEN_MAX_AGE_HOURS`: Lifetime for mobile API tokens.

Check [`flask_app/.env.example`](flask_app/.env.example) for the currently wired backend defaults.

## Main Routes and APIs

### Browser UI

| Route | Purpose | Auth |
| --- | --- | --- |
| `/` | Redirects to the correct dashboard for the logged-in user | Session |
| `/login/admin` | Admin login page | Public |
| `/login/customer` | Customer login page | Public |
| `/admin/customers` | Customer account management and device/customer mapping | Admin |
| `/admin/customers/<device_id>/services` | Update service flags and cloud-feed mode for one device | Admin |
| `/admin/customers/<device_id>/firmware` | Upload master or slave firmware artifacts for one device; rejects binaries whose embedded role marker does not match the chosen target | Admin |
| `/admin/releases/android` | Upload a customer Android APK release | Admin |
| `/admin/customers/<device_id>/reboot` | Queue a reboot command for one device | Admin |
| `/customer/dashboard` | Customer dashboard | Customer |
| `/devices/<device_id>` | Device detail page | Logged-in user |
| `/static/version.json` | Android update manifest for the latest uploaded APK | Public |
| `/downloads/android/latest.apk` | Download the latest uploaded Android APK | Public |

### Device-facing endpoints

| Route | Method | Purpose |
| --- | --- | --- |
| `/status` | `GET` | Basic API status/version response and active `device_source_mode` |
| `/status` | `POST` | Receive and store device telemetry; devices may send `X-Device-Source: real|virtual` |
| `/device/command` | `GET` | Device polls for queued or relayed commands for the active source mode |
| `/device/command/ack` | `POST` | Device acknowledges command delivery for the active source mode |

### Monitoring and data endpoints

| Route | Purpose | Auth |
| --- | --- | --- |
| `/health` | Lightweight health response | Public |
| `/last` | Latest dashboard snapshot | Session |
| `/history` | Time-window telemetry history | Session |
| `/analytics` | Aggregated usage analytics | Session |
| `/system/status` | Structured device/system state | Session |
| `/monitoring/summary` | Monitoring summary | Session |
| `/monitoring/alerts` | Alert list | Session |
| `/monitoring/audit` | Audit trail | Session |
| `/events` | Recent event feed | Session |
| `/relay/health` | Relay queue and relay connectivity summary | Admin |
| `/admin/db-summary` | DB size/retention/row summary | Admin |
| `/admin/device-source-mode` | Get or set the active backend `device_source_mode` | Admin |
| `/ml/predict` | Forecast payload for a device | Session |

### Mobile API

The mobile API uses signed tokens, not browser sessions.

| Route | Method | Purpose |
| --- | --- | --- |
| `/api/mobile/auth/login` | `POST` | Exchange admin/customer credentials for a token |
| `/api/mobile/bootstrap` | `GET` | Initial dashboard/mobile payload |
| `/api/mobile/analytics` | `GET` | Analytics payload |
| `/api/mobile/last` | `GET` | Latest snapshot |
| `/api/mobile/device/status` | `GET` | Snapshot + system + monitoring status |
| `/api/mobile/device/services` | `GET`, `POST` | Read or update device service settings |
| `/api/mobile/device/firmware` | `GET` | Latest device-specific firmware for `role=master` or `role=slave` |
| `/api/mobile/device/firmware/<artifact_id>/download` | `GET` | Authenticated firmware artifact download for the scoped device and role |
| `/api/mobile/motor/on` | `POST` | Queue motor `ON` |
| `/api/mobile/motor/off` | `POST` | Queue motor `OFF` |
| `/api/mobile/sensor/calibrate` | `POST` | Queue calibration |
| `/api/mobile/sensor/configure` | `POST` | Queue tank config update |
| `/api/mobile/account/password` | `POST` | Self-service password change |

## Database and Persistence

The backend auto-creates and maintains its MySQL/MariaDB database schema on startup. Use `DB_BACKEND=mysql` with `DATABASE_URL` or `MYSQL_*` values. SQLite is no longer supported by the Flask app.

Important tables include:

- `tank_data`: Telemetry history and the latest device snapshot fields
- `device_command_queue`: Commands waiting for or already delivered to devices
- `relay_queue`: Deferred outbound relay payloads
- `ops_alerts`: Operational alerts
- `ops_audit_log`: Audit trail for admin and account actions
- `customer_accounts`: Customer login records keyed by `device_id`
- `registered_devices`: Known devices seen by the backend
- `device_service_configs`: Per-device service/cloud-feed controls used by admin, dashboard, and mobile flows
- `firmware_artifacts`: Uploaded firmware binaries and metadata for device-scoped master/slave updates
- `android_app_releases`: Uploaded Android APK metadata for website downloads and update checks
- `app_settings`: Persisted app secret and dashboard password settings

Important production notes:

- Set `DB_BACKEND=mysql` and provide `DATABASE_URL` or `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, and `MYSQL_DATABASE`.
- Set `APP_SECRET_KEY` explicitly in production even though the app can persist a generated value.
- If sessions or stored passwords appear to reset after redeploy, check both `APP_SECRET_KEY` and DB persistence first.

## Optional ML Forecasting

The `/ml/predict` endpoint depends on:

- the normal `requirements.txt` install
- a trained artifact, default path: `artifacts/level_forecast_model.pkl`

Train a model from exported or migrated telemetry only after preparing a compatible training dataset. The legacy SQLite training helper is no longer part of the normal MySQL runtime path.

If the artifact is missing, ML dependencies are unavailable, or the selected device has too little telemetry,
`/ml/predict` returns a normal JSON payload with `available=false`, a `reason_code`, and remediation text.
Core dashboard and telemetry features do not depend on ML being ready.

## Testing

This repo keeps a small backend-only pytest layer for local startup/env parsing checks and optional simulator-import coverage.

Install the local unit-test tooling:

```powershell
python -m pip install -r requirements-dev.txt
```

Run the unit-style suite from the project root:

```powershell
pytest
```

Useful focused runs:

```powershell
pytest tests/test_startup_env_parsing.py
pytest tests/test_external_simulator.py
```

Flask integration, API, Playwright UI, ML script, and virtual-device tests live in the sibling repository `../swt_test_cases_project`.

If you use the sibling test repo's virtual-device runner, point Flask at it with:

```powershell
$env:SWT_FLASK_TEST_REPO = "..\\swt_test_cases_project"
```

## Utility Scripts

### Legacy SQLite Migration Helpers

Some scripts remain for one-time migration from older SQLite pilot data, but the Flask app itself now runs MySQL/MariaDB only. Do not use those helpers as the production database path.

### Virtual Device Emulator

The virtual-device runner and its generated env tooling live in the sibling repository `../swt_test_cases_project`.

Use that repo for:

- `scripts/run_virtual_devices.py`
- `scripts/generate_virtual_device_envs.py`
- `tests/virtual_devices/*.env`
- moved ML and virtual-device pytest coverage

## Deployment Guidance

This repository already includes:

- [`Procfile`](Procfile)
- gunicorn config in [`flask_app/gunicorn.conf.py`](flask_app/gunicorn.conf.py)

Typical gunicorn wiring:

- install command: `pip install -r requirements.txt`
- start command: `gunicorn server:app --config flask_app/gunicorn.conf.py`
- health check: `/health`

Before deploying:

- replace all placeholder secrets
- set `DB_BACKEND=mysql`
- configure `DATABASE_URL` or `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, and `MYSQL_DATABASE`
- keep `SESSION_COOKIE_SECURE=true`
- keep firmware and Android release artifact directories on persistent storage
- decide whether relay URLs should be enabled in that environment
- `requirements.txt` already includes the ML dependency set used by `/ml/predict`

## Operational Docs

For rollout and support work, see:

- [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md)
- [`docs/INSTALLER_SUPPORT_RUNBOOK.md`](docs/INSTALLER_SUPPORT_RUNBOOK.md)
- [`Flask_deployment_README.md`](Flask_deployment_README.md)

## Troubleshooting

- `403 invalid device credentials`: Check that the device is sending the correct `device_id` and API key, and that Flask is reading the same values from `device.env` or `DEVICE_KEYS`.
- Login works but session does not stick locally: Set `SESSION_COOKIE_SECURE=false` when serving over plain `http://`.
- Graphs or analytics look empty: Confirm `TELEMETRY_HISTORY_ENABLED=true` and make sure the database has at least a few telemetry rows.
- State disappears after restart or redeploy: Check MySQL persistence and set a stable `APP_SECRET_KEY`.
- `/ml/predict` fails with missing artifact: Make sure `requirements.txt` is installed and train a model with `scripts/train_level_forecast_model.py`.

## Recommended First-Run Checklist

1. Copy `flask_app/.env.example` and `device.env.example`.
2. Replace every `change-me` value.
3. Set `SESSION_COOKIE_SECURE=false` for local HTTP.
4. Decide whether relay URLs should be blank for local testing.
5. Start the server and sign in at `/login/admin`.
6. Confirm `/health` and `/admin/db-summary` look correct.
7. Send one test telemetry payload from a device and verify it appears in `/last`.

## Related Workspace Projects

- Workspace overview: [`../README.md`](../README.md)
- Firmware project: [`../swt_firmware_project/README.md`](../swt_firmware_project/README.md)
- Android project: [`../swt_android_app_project/README.md`](../swt_android_app_project/README.md)
- Test harness: [`../swt_test_cases_project/README.md`](../swt_test_cases_project/README.md)
