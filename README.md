# Smart Water Tank Flask Backend

This repository contains the Flask backend for the Smart Water Tank system. It receives telemetry from tank controllers, stores operational state in SQLite, serves the web dashboard and PWA, exposes mobile-friendly APIs, queues control commands for devices, and provides monitoring, alerting, and support tooling.

This backend is only one part of the wider Smart Water Tank stack. The shared `device.env` file is designed so the firmware, Flask backend, and companion clients can use the same device identity and endpoint settings.

## What This Project Includes

- Device telemetry ingestion through `POST /status`
- Device command delivery through `/device/command` and `/device/command/ack`
- Admin and customer login flows with separate scopes
- Browser dashboard, customer dashboard, and per-device detail pages
- Monitoring endpoints for health, alerts, audit events, relay state, and DB summary
- Optional HTTP relay and MQTT bridge support
- Optional ML-based tank level forecasting through `/ml/predict`
- Render-ready deployment config with persistent SQLite disk support

## Repository Layout

| Path | Purpose |
| --- | --- |
| `server.py` | Root entrypoint and WSGI compatibility wrapper |
| `flask_app/server.py` | Main Flask application, routes, DB init, auth, telemetry, command queue, relay logic |
| `flask_app/__init__.py` | Package export for `app` |
| `flask_app/templates/` | Login, dashboard, admin, and device-detail UI templates |
| `flask_app/static/` | PWA manifest, service worker, icons, and frontend JS |
| `flask_app/.env.example` | Example backend environment file |
| `device.env.example` | Example shared device identity/settings file |
| `requirements.txt` | Root dependency entrypoint, points to `flask_app/requirements.txt` |
| `requirements-ml.txt` | Optional ML dependencies for forecasting |
| `render.yaml` | Render deployment definition |
| `Procfile` | Procfile for gunicorn-based platforms |
| `scripts/` | Operational and data utility scripts |
| `docs/` | Production rollout and support guidance |
| `data/` | Default local SQLite database location |

## Runtime Flow

1. A device sends telemetry to `POST /status`.
2. Flask authenticates the device using `X-Device-Id` and `X-Device-Key` headers, or `device_id` and `device_key` fields in the JSON body.
3. Telemetry is stored in SQLite, device registration is refreshed, and operational alerts can be updated.
4. The dashboard and mobile APIs read snapshot, history, analytics, alerts, and audit data from the database.
5. Browser or mobile control actions queue commands in `device_command_queue` and optionally publish them to MQTT.
6. Devices poll `GET /device/command`, execute the command, then confirm delivery with `POST /device/command/ack`.

## Local Setup

### 1. Copy the example config files

PowerShell:

```powershell
Copy-Item flask_app\.env.example flask_app\.env
Copy-Item device.env.example device.env
Copy-Item tests\virtual_device.env.example tests\virtual_device.env
```

### 2. Edit the copied files before first run

At minimum, update these values:

- `APP_SECRET_KEY`
- `LOGIN_PASSWORD`
- `SWT_DEVICE_ID`
- `SWT_DEVICE_API_KEY`
- `SWT_OTA_PASSWORD`
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

Install ML extras only if you want forecasting:

```powershell
python -m pip install -r requirements-ml.txt
```

### 4. Run the server

```powershell
python server.py
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
- `tests/virtual_device.env` can override shared values, but only for `scripts/virtual_device.py`

To avoid confusion, keep backend-only settings in `flask_app/.env`, shared device credentials in `device.env`, and emulator-only overrides in `tests/virtual_device.env`.

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

- `SWT_DEVICE_ID` and `SWT_DEVICE_API_KEY`: Simplest single-device/shared-device setup.
- `SWT_DEVICE_KEYS` or `DEVICE_KEYS`: Comma-separated registry for multi-device auth.
- `DEVICE_KEYS` format: `device-a:key-a,device-b:key-b,prefix*:shared-key`
- `DEVICE_URL`: Optional fixed device URL used when building firmware update redirects.
- `SWT_DEVICE_SOURCE_MODE`: Active backend source for snapshot/history/analytics/command reads. Use `real` for MCU traffic or `virtual` when testing with `scripts/virtual_device.py`.

### Storage and retention

- `DB_FILE`: SQLite file path. Defaults to `data/tank.db` locally.
- `DATA_RETENTION_DAYS`: How long telemetry is retained.
- `TELEMETRY_HISTORY_ENABLED`: If `false`, only the latest snapshot is effectively kept.
- `MAX_TELEMETRY_ROWS_PER_DEVICE`: Hard cap per device for telemetry history.
- `DEVICE_COMMAND_RETENTION_DAYS`: Retention for delivered commands.
- `OPS_ALERT_RETENTION_DAYS`: Retention for operational alerts.
- `OPS_AUDIT_RETENTION_DAYS`: Retention for audit records.
- `DB_MAINTENANCE_ENABLED`: Enables cleanup/maintenance passes.
- `DB_TARGET_SIZE_MB`: Soft database size target used by maintenance logic.
- `DB_MAINTENANCE_MIN_INTERVAL_SECONDS`: Minimum spacing between maintenance runs.
- `DB_WAL_AUTOCHECKPOINT_PAGES`: WAL checkpoint tuning.
- `REQUIRE_RENDER_PERSISTENT_DB`: If `true`, startup fails on Render unless `/var/data/tank.db` is active.

### Relay, notifications, and MQTT

- `RELAY_STATUS_URLS`: Comma-separated telemetry relay targets.
- `RELAY_COMMAND_URLS`: Comma-separated remote command relay targets.
- `RELAY_VERIFY_TLS`: TLS verification for relay HTTP calls.
- `ALERT_WEBHOOK_URL`, `SLACK_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `WHATSAPP_WEBHOOK_URL`: Optional alert integrations.
- `MQTT_ENABLED`: Enables MQTT bridge behavior.
- `MQTT_BROKER_HOST`, `MQTT_BROKER_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`
- `MQTT_TOPIC_PREFIX`, `MQTT_KEEPALIVE_SEC`, `MQTT_QOS`, `MQTT_COMMAND_RETAIN`

### Analytics and forecasting

- `TANK_CAPACITY_LITERS`: Default tank capacity used in summaries.
- `DATA_STALE_AFTER_SECONDS`: Threshold for stale telemetry.
- `LEVEL_FORECAST_MODEL_PATH`: Optional custom path to the forecast artifact.
- `MOBILE_TOKEN_MAX_AGE_HOURS`: Lifetime for mobile API tokens.

Check [`flask_app/.env.example`](flask_app/.env.example) and [`render.yaml`](render.yaml) for the currently wired defaults.

## Main Routes and APIs

### Browser UI

| Route | Purpose | Auth |
| --- | --- | --- |
| `/` | Redirects to the correct dashboard for the logged-in user | Session |
| `/login/admin` | Admin login page | Public |
| `/login/customer` | Customer login page | Public |
| `/admin/customers` | Customer account management and device/customer mapping | Admin |
| `/customer/dashboard` | Customer dashboard | Customer |
| `/devices/<device_id>` | Device detail page | Logged-in user |
| `/firmware/update` | Redirects to the latest or scoped device OTA page | Logged-in user |

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
| `/api/mobile/motor/on` | `POST` | Queue motor `ON` |
| `/api/mobile/motor/off` | `POST` | Queue motor `OFF` |
| `/api/mobile/motor/auto` | `POST` | Queue `AUTO` |
| `/api/mobile/sensor/calibrate` | `POST` | Queue calibration |
| `/api/mobile/sensor/configure` | `POST` | Queue tank config update |
| `/api/mobile/account/password` | `POST` | Self-service password change |

## Database and Persistence

The backend auto-creates and maintains its SQLite database on startup. Important tables include:

- `tank_data`: Telemetry history and the latest device snapshot fields
- `device_command_queue`: Commands waiting for or already delivered to devices
- `relay_queue`: Deferred outbound relay payloads
- `ops_alerts`: Operational alerts
- `ops_audit_log`: Audit trail for admin and account actions
- `customer_accounts`: Customer login records keyed by `device_id`
- `registered_devices`: Known devices seen by the backend
- `app_settings`: Persisted app secret and dashboard password settings

SQLite is configured with WAL mode. This works well for a small hosted Flask service, but it still needs persistent storage in production.

Important production notes:

- Do not rely on ephemeral filesystems for production data.
- On Render, use a mounted disk and keep the DB at `/var/data/tank.db`.
- Set `APP_SECRET_KEY` explicitly in production even though the app can persist a generated value.
- If sessions or stored passwords appear to reset after redeploy, check both `APP_SECRET_KEY` and DB persistence first.

## Optional ML Forecasting

The `/ml/predict` endpoint depends on:

- extra dependency install from `requirements-ml.txt`
- a trained artifact, default path: `artifacts/level_forecast_model.pkl`

Train a model from existing telemetry:

```powershell
python scripts/train_level_forecast_model.py --db-path data/tank.db --horizon-hours 1
```

If the artifact is missing, `/ml/predict` returns an error explaining how to train it.

## Utility Scripts

### Train the forecast model

```powershell
python scripts/train_level_forecast_model.py --db-path data/tank.db --horizon-hours 1
```

### Export bootstrap values from an existing DB

This is useful when moving an existing DB-backed deployment to environment-backed bootstrap variables.

```powershell
python scripts/export_render_bootstrap.py data/tank.db
```

It prints:

- `APP_SECRET_KEY`
- `DASHBOARD_PASSWORD_HASH`
- `CUSTOMER_ACCOUNTS_BOOTSTRAP_B64`

### Keep only one device in a database

Useful when cleaning a pilot DB or preparing a single-site handoff.

```powershell
python scripts/cleanup_single_device_db.py --db-path data/tank.db --keep-device-id swt-000-000-000-001
```

By default, the script creates a timestamped backup before deleting rows.

### Run the virtual device emulator

The emulator behaves like the MCU and is useful for Flask-side testing:

- sends telemetry to `/status`
- polls `/device/command`
- acknowledges `/device/command/ack`
- simulates relay/service connect-disconnect periods

Recommended flow:

1. Set `SWT_DEVICE_SOURCE_MODE=virtual` in `device.env`.
2. Adjust `tests/virtual_device.env` if you want emulator-specific settings without changing shared device config.
3. Start Flask.
4. Run:

```powershell
python scripts/virtual_device.py --base-url http://127.0.0.1:8000/ --device-id swt-node-01 --device-key change-me-device-key
```

The emulator automatically loads defaults from `tests/virtual_device.env`, so you can usually run it without passing all CLI flags every time.

Helpful options:

- `--run-seconds 120`
- `--connected-seconds 90 --disconnected-seconds 30`
- `--no-enable-source-tank`

If you want to switch modes without restarting Flask, `GET/POST /admin/device-source-mode` is available for admin sessions. `POST` uses the normal CSRF protection.

## Deployment Guidance

This repository already includes:

- [`render.yaml`](render.yaml)
- [`Procfile`](Procfile)
- gunicorn config in [`flask_app/gunicorn.conf.py`](flask_app/gunicorn.conf.py)

Current Render wiring:

- runtime: Python
- Python version: `3.11.11`
- build command: `pip install -r flask_app/requirements.txt`
- start command: `gunicorn server:app --config flask_app/gunicorn.conf.py`
- health check: `/health`
- persistent disk mount path: `/var/data`

Before deploying:

- replace all placeholder secrets
- set `DB_FILE=/var/data/tank.db`
- keep `SESSION_COOKIE_SECURE=true`
- attach a persistent disk
- decide whether relay URLs should be enabled in that environment
- install `requirements-ml.txt` too if you want `/ml/predict` in production

## Operational Docs

For rollout and support work, see:

- [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md)
- [`docs/INSTALLER_SUPPORT_RUNBOOK.md`](docs/INSTALLER_SUPPORT_RUNBOOK.md)

## Troubleshooting

- `403 invalid device credentials`: Check that the device is sending the correct `device_id` and API key, and that Flask is reading the same values from `device.env` or `DEVICE_KEYS`.
- Login works but session does not stick locally: Set `SESSION_COOKIE_SECURE=false` when serving over plain `http://`.
- Graphs or analytics look empty: Confirm `TELEMETRY_HISTORY_ENABLED=true` and make sure the database has at least a few telemetry rows.
- State disappears after restart or redeploy: Use persistent storage for `DB_FILE` and set a stable `APP_SECRET_KEY`.
- `/ml/predict` fails with missing artifact: Install `requirements-ml.txt` and train a model with `scripts/train_level_forecast_model.py`.

## Recommended First-Run Checklist

1. Copy `flask_app/.env.example` and `device.env.example`.
2. Replace every `change-me` value.
3. Set `SESSION_COOKIE_SECURE=false` for local HTTP.
4. Decide whether relay URLs should be blank for local testing.
5. Start the server and sign in at `/login/admin`.
6. Confirm `/health` and `/admin/db-summary` look correct.
7. Send one test telemetry payload from a device and verify it appears in `/last`.
