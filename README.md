# SaleWell Smart Tank Flask Backend

Last refreshed: `2026-09-21`

## Firmware board and simulator selection

The tank node can use ESP8266 (`swt_slave`) or ESP32 (`swt_esp32_slave`).
Keep uploaded firmware artifacts matched to the installed board and device
identity. Both ESP32 roles now request maximum driver TX power; this takes
effect after flashing, not after a backend restart.
Normal firmware packaging defaults to simulation enabled in test mode and
disabled in production; production rejects enabled simulation. Runtime admin
simulator controls remain separate from these build settings.
See the [firmware guide](../swt_firmware_project/README.md).

## Municipal Sensor and Simulator Status

Admin configuration exposes a separate Municipal Detection Sensor card.
Water-flow and water-pressure are mutually exclusive, and Municipal Water must
be enabled before either is active. Diagnostics replace the generic municipal
simulator with a detector-specific flow/pressure simulator and provide an
admin-only bulk action to disable every simulator for hardware testing.

Fleet and device-detail pages preserve operational labels instead of converting
green states to generic `Online`. Municipal Water shows `Available`, `No Flow`,
`Disabled`, or `Offline` when controller telemetry is stale/offline. The fleet
entry pipeline preserves flow/pressure enabled and detected telemetry fields so
initial HTML and automatic refresh agree.

## Documentation map

Start with [`docs/README.md`](docs/README.md). It separates customer and installer guidance from development, deployment, and production operations. The public bilingual customer guide is [`docs/SaleWell-Smart-Tank-Customer-Installation-Guide-English-Hindi.pdf`](docs/SaleWell-Smart-Tank-Customer-Installation-Guide-English-Hindi.pdf); the detailed internal guide remains [`docs/INSTALLATION_GUIDE_EN_HI.md`](docs/INSTALLATION_GUIDE_EN_HI.md).

The complete product control, client, data, deployment, and test boundaries are
defined in [`../docs/PROJECT_DESIGN_AND_ARCHITECTURE.md`](../docs/PROJECT_DESIGN_AND_ARCHITECTURE.md).

## Developer Setup

On Ubuntu/WSL, prepare the complete workspace from its root:

```bash
cd ~/workspace/all_swt_project
./getting_start.sh
source swt_flask_project/.venv/bin/activate
```

The bootstrap installs both `requirements.txt` and `requirements-dev.txt` in
this project's `.venv`. It does not create or overwrite `device.env`, start
Flask, start Docker, or modify MySQL.

This repository contains the Flask backend for the SaleWell Smart Tank system. It receives telemetry from tank controllers, stores operational state in MySQL/MariaDB, serves the web dashboard and PWA, exposes mobile-friendly APIs, queues control commands for devices, and provides monitoring, alerting, and support tooling. Devices keep their local control loops on the ESP32 master (or a supported legacy ESP8266 master) when Flask or the internet is unavailable; Flask adds remote visibility and command routing on top.

This backend is one part of the wider SaleWell IoT Solutions stack. The shared `device.env` file is designed so the firmware, Flask backend, and companion clients can use the same device identity and endpoint settings.

## Workspace Context

Within the wider workspace:

- [`../swt_firmware_project/README.md`](../swt_firmware_project/README.md) documents the mixed ESP32-master / ESP8266-slave controller pair that posts to `/status` and polls `/device/command`
- [`../swt_android_app_project/README.md`](../swt_android_app_project/README.md) documents the Android client that consumes `/api/mobile/*` and local firmware pages
- [`Flask_deployment_README.md`](Flask_deployment_README.md) covers cPanel / Passenger deployment for this backend
- [`../swt_test_cases_project/README.md`](../swt_test_cases_project/README.md) covers the separated API/UI/ML test suites and virtual-device tooling

## What This Project Includes

Cloud telemetry uploads, Android refreshes, and Flask dashboard polling use `SWT_CLOUD_POLL_INTERVAL_SECONDS=30`. Firmware command checks independently use `SWT_CLOUD_COMMAND_POLL_INTERVAL_SECONDS=10`. Local sensor sampling and master/slave safety timing remain at 5 seconds, so lower cloud load does not delay pump protection.

- Device telemetry ingestion through `POST /status`
- Device command delivery through `/device/command` and `/device/command/ack`
- Admin and customer login flows with separate scopes
- Customer forgot-password and reset-password flow when SMTP is configured
- Browser dashboard, customer dashboard, and per-device detail pages
- PWA manifest, service worker, install prompt, mobile-friendly public pages, and customer-facing homepage
- Pricing/comparison page for Home Basic, Home Control, Home Cloud Pro, RWA Standard, Commercial AI Pro, Dealer / Installer Kit, and Enterprise Modular plans
- Sales/demo enquiry form with backup logging, support email, customer confirmation email, and optional WhatsApp webhook delivery
- Monitoring endpoints for health, alerts, audit events, relay state, and DB summary
- Admin service controls for source tank monitoring, optional municipal automation, optional upper/lower turbidity monitoring, buzzer, LED, cloud-feed mode, and customer AI access
- Device-detail current-status cards and activity events for live node reachability, peer channel, peer freshness, and service state
- Admin delete flow that purges device-scoped data and can keep a deleted-device marker until the device is registered again
- Firmware artifact upload/download flow for independently scoped master, slave, repeater1, and repeater2 OTA updates
- Flask and Android OTA remain valid for hardware-bound firmware. Binding is
  enforced by the target MCU using the MAC compiled during production
  packaging; Flask continues to issue device-scoped, expiring HMAC
  authorization for the selected artifact and role.
- Admin-managed Android APK releases with customer download and in-app update manifest
- APK update availability is determined from the uploaded APK's monotonic numeric `versionCode`, which must never reset. Android and firmware display names use `vYY.M.<monthlyIncrement>` and reset only that final display component when the UTC month/year changes; uploading the same Android numeric code again does not create an update
- A successful firmware upload replaces the previous artifact for the same device and selected role (`master`, `slave`, `repeater1`, or `repeater2`). Repeater artifacts use distinct derived device identities and are never shared between indices. A successful Android upload replaces all previous APK releases. Old database rows and obsolete stored files are removed only after the replacement is registered successfully.
- Optional HTTP relay and notification integration support
- Optional ML-based tank level forecasting through `/ml/predict`
- Android update manifests at `/static/version.json` and `/api/mobile/app/update`
- MySQL/MariaDB schema initialization for local and hosted deployment
- Independent AJAX simulator switches for tank level, municipal water, motorized valve, lower turbidity, and upper turbidity. Simulator state is read back from persisted device telemetry after refresh.
- A persisted Device Setup Type selector with six supported route presets plus Custom/manual configuration. Saving a preset applies compatible service flags and automatic mode as one configuration.
- Automatic preset scenarios for simulator-enabled firmware: reset injected faults and overrides, seed route-appropriate tank levels, and enable only the installed tank, municipal, valve, and turbidity simulators. Production firmware is detected from telemetry and does not receive simulator-only commands.
- The Motorized Valve status reports `ON`/`OFF` separately from its selected path (`Municipal Water` or `Source Tank`). Motion states remain available for diagnostics.
- Master Configuration keeps the optional municipal-water sensor independent from the motorized inlet valve. Sensor-free installations use firmware level-rise detection instead of disabling valve routing.
- Flask queues logical inlet/outlet valve settings but does not own physical GPIO assignments. The firmware mapping currently reserves inlet OPEN/CLOSE on ESP32 `G25/G26`, outlet SOURCE/UPPER on `G27/G21`, and outlet feedback on `G22/G23`; both valve features remain disabled until hardware commissioning. See [`GPIO_POINT_TO_POINT_MAPPING.md`](../swt_firmware_project/docs/GPIO_POINT_TO_POINT_MAPPING.md).
- The public homepage keeps the original rooftop preview image. Its **Dashboard Preview** link opens `static/marketing/water_flow_animation.html`; regenerate that deployed asset with `python ../scripts/sync_water_flow_animation.py` after changing the source animation.
- Public login and pricing pages provide corrected chatbot/footer actions, municipal-package pricing, and a chatbot-assisted demo/device-booking path backed by the maintained customer FAQ and chatbot knowledge base.

Simulator prerequisites, workflows, transitions, and troubleshooting are in [`../docs/SIMULATOR_GUIDE.md`](../docs/SIMULATOR_GUIDE.md).

### Optional Capacity Path

The backend includes a default-off, staged path for scaling beyond the legacy
wide `tank_data` workflow. It provides the combined `POST /api/device/sync`
endpoint, latest-state and narrow-history tables, adaptive sampling, hourly and
daily aggregation, bounded retention, conservative database pooling, request
and replay protection, durable notification jobs, operational health records,
staged device rollout, legacy telemetry archival, and a read-only hosting
migration assessment.

Keep `FEATURE_LEGACY_TANK_DATA_WRITES=true` until the replacement write and read
paths have passed the documented gates. Start with
[`../docs/CPANEL_CAPACITY_DEPLOYMENT_GUIDE.md`](../docs/CPANEL_CAPACITY_DEPLOYMENT_GUIDE.md)
for the rollout procedure and
[`../docs/CAPACITY_FEATURE_FLAGS.md`](../docs/CAPACITY_FEATURE_FLAGS.md) for the
flag dependencies and safe defaults. Capacity scripts live in `scripts/`, and
effective feature state and bounded process metrics are exposed to authenticated
operators through `/system/status`.

## Customer and Sales Features

- public marketing homepage with product explanations, plan guidance, Android download link, and enquiry entry points
- guided product chatbot content for pricing, pump control, overflow, leakage, installation type, app access, and service coverage questions
- customer dashboard for tank level, pump state, alert status, events, analytics, and local sync
- admin dashboard for device registration, customer mapping, customer password reset, service flags, peer channel, firmware artifacts, Android releases, reboot commands, delete/purge, and data-source mode
- operational monitoring for last-seen status, stale telemetry, alerts, audit log, command delivery, and database summary
- optional integrations for SMTP email, WhatsApp webhook, Slack/Telegram-style alert hooks, HTTP relay, and MQTT telemetry/command channels


## Repository Layout

| Path | Purpose |
| --- | --- |
| `server.py` | Root entrypoint and WSGI compatibility wrapper; keep this in sync with `flask_app/server.py` for cPanel / Passenger deployments |
| `flask_app/server.py` | Main Flask application, routes, DB init, auth, telemetry, command queue, relay logic |
| `flask_app/__init__.py` | Package export for `app` |
| `flask_app/templates/` | Login, dashboard, admin, and device-detail UI templates |
| `flask_app/static/` | PWA assets, frontend JS, and fallback `version.json` for Android update checks |
| `device.env.example` | Canonical example for Flask backend settings and shared device identity/settings |
| `requirements.txt` | Single dependency file for the whole project, including ML support |
| `gunicorn.conf.py` | Root wrapper that loads `flask_app/gunicorn.conf.py` |
| `Procfile` | Procfile for gunicorn-based platforms |
| `Flask_deployment_README.md` | cPanel / Passenger deployment guide |
| `scripts/` | Operational and data utility scripts |
| `docs/` | Production rollout and support guidance |
| `tests/` | Backend-local pytest checks for startup/env parsing, mobile local sync, firmware artifacts, dashboard behavior, and optional simulator imports |

## Runtime Flow

1. A device sends telemetry to `POST /status`.
2. Flask authenticates the device using the `X-Device-Id` and `X-Device-Key` headers. The legacy `device_id` and `device_key` JSON fields remain accepted for compatibility, but new clients should use headers.
3. Telemetry is stored in MySQL, device registration is refreshed, and operational alerts can be updated.
4. The dashboard and mobile APIs read snapshot, history, analytics, alerts, and audit data from the database.
5. Browser or mobile control actions queue commands in `device_command_queue`; optional integrations can relay selected payloads when configured.
6. Devices poll `GET /device/command`, execute the command, then confirm delivery with `POST /device/command/ack`.

## MCP Server and RAG

The backend now includes local-document RAG and a stdio MCP server. It indexes
this project's `README.md` and `docs/` first, plus workspace-level `../docs`
and `../README.md` when they exist. Set
`RAG_DOCUMENT_PATHS` to an OS-path-separator-delimited list of other Markdown,
text, reStructuredText, or DOCX files/directories. Relative paths start from
`swt_flask_project`, so `RAG_DOCUMENT_PATHS=README.md:docs` works on Linux.

Anonymous homepage answers use a separate customer-safe allowlist. By default
it includes the chatbot knowledge base, customer FAQ, English/Hindi
feature guides, installation rule book, components/BOM, modular architecture,
and customer BOM/estimation DOCX files. Internal production, Jenkins, pytest,
and maintenance documents are excluded. Override this list only with
visitor-safe paths using `PUBLIC_RAG_DOCUMENT_PATHS`.

The FTPS uploader packages workspace-level customer sources into
`docs/customer_sources/` on the hosted Flask application, so the curated
corpus remains available on cPanel even when only `swt_flask_project` is
uploaded. Runtime configuration files and internal workspace documents remain
excluded.

For HTTP access, set `RAG_ENABLED=true` and a strong `RAG_API_KEY`, then send
the key as `Authorization: Bearer <RAG_API_KEY>` (an authenticated dashboard
session also works):

```bash
curl -X POST http://localhost:8000/api/rag/ask \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"question":"How does overflow protection work?"}'
```

The routes are `POST /api/rag/search`, `POST /api/rag/ask`, and
`POST /api/rag/refresh`. Retrieval uses the existing scikit-learn dependency
and works without a cloud key. With `OPENAI_API_KEY` configured, `/ask`
generates a grounded answer with source citations; without it, the endpoint
returns the strongest matching source passage.

The existing homepage chatbot keeps its deterministic pricing, plan, booking,
and contact flows. Questions that do not match those flows fall back to the
chatbot-specific `POST /chatbot/ask` handler, which searches the same RAG index without exposing
`RAG_API_KEY` to the browser. This public route limits question length and
requests per client; tune `PUBLIC_RAG_RATE_LIMIT`,
`PUBLIC_RAG_RATE_WINDOW_SECONDS`, and `PUBLIC_RAG_MAX_QUESTION_LENGTH` for the
production traffic profile. Only place visitor-safe content in configured RAG
document paths.

Install its optional dependency with `pip install -r requirements-mcp.txt`,
then start the MCP server from `swt_flask_project` with `python mcp_server.py`. A
typical MCP client configuration is:

```json
{
  "mcpServers": {
    "salewell-smart-tank": {
      "command": "python",
      "args": ["D:/all_swt_project/swt_flask_project/mcp_server.py"]
    }
  }
}
```

The MCP tools are `search_knowledge_base`, `ask_knowledge_base`, and
`refresh_knowledge_base`. The stdio server is intended to be launched by a
trusted local MCP client; it does not expose a network listener.

Pump `Start` and `Stop` commands are logical commands. On current default
firmware they become short relay pulses for the physical green Start and red
Stop/open circuits. Old maintained-relay firmware can still interpret them as
held relay ON/OFF. The backend command queue does not bypass or replace the
panel safety wiring.

## Local Setup

### 1. Copy the example config file

PowerShell:

```powershell
Copy-Item device.env.example device.env
```

### 2. Edit the copied file before first run

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

```


## Configuration Loading

### Pump automation configuration

The tracked [`config/pump_control.json`](config/pump_control.json) file defines the shared bootstrap defaults and validation bounds for Flask, Android, and firmware. Its current defaults are start at or below `30%` and stop at or above `95%`. Do not move it above this project directory: cPanel deploys this repository independently and Passenger loads the file at import time.

For an installed device, the `device_service_configs.auto_start_pct` and `auto_stop_pct` columns are authoritative. The admin device page and `GET/POST /api/mobile/device/thresholds` read/write the same record and queue `THRESHOLDS:<start>:<stop>` to firmware. A live snapshot may seed an unset record but does not replace an existing saved value.

The app reads local configuration from one canonical file:

1. `./device.env`

Values already present in the real process environment are preserved. In practice:

- system environment variables win
- `device.env` supplies Flask, database, notification, and shared device settings
- the sibling test repo can layer per-device virtual-device env files on top of the shared settings for its emulator tooling

Legacy `./.env` and `./flask_app/.env` files are no longer loaded. Keep every
local Flask/backend and shared device setting in `device.env`; virtual-device
overrides remain in the sibling `swt_test_cases_project` repo. Hosted process
environment variables may still be used when the platform injects them.

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
- Production firmware should also use per-role hardware binding. See
  [`../swt_firmware_project/docs/FIRMWARE_PROTECTION.md`](../swt_firmware_project/docs/FIRMWARE_PROTECTION.md).
  The explicit unbound-production option does not weaken Flask authentication,
  but it permits the same binary to run on another compatible MCU.
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
- `DATA_STALE_AFTER_SECONDS`: Threshold for stale telemetry. Defaults to 300 seconds.
- `LEVEL_FORECAST_MODEL_PATH`: Optional custom path to the forecast artifact.
- `MOBILE_TOKEN_MAX_AGE_HOURS`: Lifetime for mobile API tokens.

Check [`device.env.example`](device.env.example) for the currently wired backend defaults.

## Main Routes and APIs

### Browser UI

| Route | Purpose | Auth |
| --- | --- | --- |
| `/` | Redirects to the correct dashboard for the logged-in user | Session |
| `/login/admin` | Admin login page | Public |
| `/login/customer` | Customer login page | Public |
| `/login/customer/forgot-password` | Request a customer password reset email | Public |
| `/login/customer/reset-password/<token>` | Complete a customer password reset | Public token |
| `/account/password` | Change the current admin dashboard password | Admin |
| `/admin/customers` | Customer account management and device/customer mapping | Admin |
| `/admin/devices/register` | Register device credentials and optional customer account | Admin |
| `/admin/customers/<device_id>/services` | Update service flags and cloud-feed mode for one device | Admin |
| `/admin/customers/<device_id>/delete` | Delete a device through the normal admin flow; purges device-scoped rows and keeps the deleted-device marker until re-registration | Admin |
| `/admin/customers/<device_id>/firmware` | Upload master, slave, repeater1, or repeater2 firmware artifacts; validates the binary role and keeps both repeater indices separate | Admin |
| `/admin/releases/android` | Upload a customer Android APK release | Admin |
| `/admin/releases/android/prune` | Prune old uploaded Android APK releases | Admin |
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
| `/analytics/export.csv` | CSV export for analytics/history records | Session |
| `/system/status` | Structured device/system state | Session |
| `/monitoring/summary` | Monitoring summary | Session |
| `/monitoring/alerts` | Alert list | Session |
| `/monitoring/audit` | Audit trail | Session |
| `/events` | Recent event feed, including live current-status snapshots generated from the latest device snapshot | Session |
| `/relay/health` | Relay queue and relay connectivity summary | Admin |
| `/admin/db-summary` | DB size/retention/row summary | Admin |
| `/admin/device-source-mode` | Get or set the active backend `device_source_mode` | Admin |
| `/ml/predict` | Forecast payload for a device | Session |

### Mobile API

The mobile API uses signed tokens, not browser sessions.

| Route | Method | Purpose |
| --- | --- | --- |
| `/api/mobile/auth/login` | `POST` | Exchange customer credentials for a mobile token; admin sign-in is web-dashboard-only |
| `/api/mobile/bootstrap` | `GET` | Initial dashboard/mobile payload |
| `/api/mobile/analytics` | `GET` | Analytics payload |
| `/api/mobile/local-sync` | `POST` | Store local-device status observed by the mobile app |
| `/api/mobile/last` | `GET` | Latest snapshot |
| `/api/mobile/device/status` | `GET` | Snapshot + system + monitoring status |
| `/api/mobile/device/services` | `GET`, `POST` | Read or update device service settings |
| `/api/mobile/device/local-auth/reset` | `POST` | Queue/reset local firmware auth from an authenticated mobile session |
| `/api/mobile/device/firmware` | `GET` | Latest identity-specific firmware for `role=master`, `slave`, `repeater1`, or `repeater2` |
| `/api/mobile/device/firmware/<artifact_id>/download` | `GET` | Authenticated firmware artifact download for the scoped device and role |
| `/api/mobile/app/update` | `GET` | Android update manifest for the latest uploaded APK |
| `/api/mobile/app/latest.apk` | `GET` | Authenticated latest Android APK download |
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
  including the persisted `device_setup_type`. Existing databases receive this
  column through startup schema maintenance; no manual SQL migration is needed.
- `firmware_artifacts`: Uploaded firmware binaries and metadata for device-scoped master/slave/repeater1/repeater2 updates
- `android_app_releases`: Uploaded Android APK metadata for website downloads and update checks
- `app_settings`: Persisted app secret and dashboard password settings

The public homepage visitor count is incremented atomically in `app_settings` and the database-confirmed value is rendered with no-store cache headers. This avoids stale per-process counts when Passenger or Gunicorn runs multiple workers.

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

This repo keeps a small backend-only pytest layer for local startup/env parsing checks, runtime event generation, dashboard status mapping, device purge behavior, and optional simulator-import coverage.

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
pytest tests/test_activity_events_runtime.py
pytest tests/test_device_purge.py
pytest tests/test_device_setup_type_scenarios.py
```

Flask integration, API, Playwright UI, ML script, and virtual-device tests live in the sibling repository `../swt_test_cases_project`.

If you use the sibling test repo's virtual-device runner, point Flask at it with:

```powershell
$env:SWT_FLASK_TEST_REPO = "..\\swt_test_cases_project"
```

## Utility Scripts

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
- deploy both `server.py` and `flask_app/server.py` together on cPanel / Passenger so the root wrapper and the main app stay aligned
- `requirements.txt` already includes the ML dependency set used by `/ml/predict`

### Live Production `stderr.log` over FTPS

The cPanel Passenger log is stored at:

```text
/home/salewellco/repositories/swt_flask_project/stderr.log
```

Create a dedicated cPanel FTP account for read/diagnostic access:

```text
Login: logviewer@salewell.co.in
Directory: repositories/swt_flask_project
FTP server: ftp.salewell.co.in
Explicit FTPS port: 21
```

Enter the FTP login name before the directory because cPanel may update the
directory automatically when the login changes. The resulting full directory
must be `/home/salewellco/repositories/swt_flask_project`. Creating an FTP
account for this existing directory does not delete its contents. Do not store
the FTP password in the repository.

From the workspace root, follow the end of the production log with:

```powershell
python .\swt_flask_project\scripts\watch_stderr_ftps.py --user logviewer@salewell.co.in --remote-file stderr.log --insecure-ftps
```

The watcher prompts for the password, initially displays the last 64 KiB,
prints newly appended data, reconnects after temporary failures, and detects
log truncation or rotation. To display only entries written after the watcher
starts, use:

```powershell
python .\swt_flask_project\scripts\watch_stderr_ftps.py --user logviewer@salewell.co.in --remote-file stderr.log --insecure-ftps --tail-bytes 0
```

The hosting server currently presents a TLS certificate whose hostname does
not match `ftp.salewell.co.in`. `--insecure-ftps` keeps the connection encrypted
but disables server identity verification; it is a temporary workaround. The
hosting provider should install a certificate valid for the FTP hostname.

If FTP login succeeds but only `.ftpquota` appears, the account is jailed in an
empty directory. Recreate it with `repositories/swt_flask_project` as its cPanel
Directory. A `421 Home directory not available` error also indicates an invalid
FTP account home and occurs before the requested log path is evaluated.

Operational messages seen in this log include:

- `Telemetry postprocess skipped ... background worker is busy`: raw telemetry
  was saved, but optional derived/background processing was skipped.
- `Dashboard summary refresh failed ... MySQL ... not reachable or credentials
  are invalid`: verify `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`,
  `MYSQL_PASSWORD`, and `MYSQL_DATABASE` in the production environment.

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

1. Copy `device.env.example` to `device.env`.
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

## Complete Local Test Environment (WSL)

The recommended isolated backend setup uses the test harness script:

```bash
cd ~/workspace/all_swt_project/swt_test_cases_project
chmod +x ./scripts/setup_test_env.sh
./scripts/setup_test_env.sh
```

It creates `.venv`, installs requirements, starts MySQL on host port `3307` and Flask on `http://127.0.0.1:8000`, waits for `/health`, and runs the focused virtual-device tests with the required `SWT_TEST_MYSQL_*` variables. Start the interactive device separately:

```bash
source .venv/bin/activate
python ./scripts/run_virtual_devices.py \
  --base-url=http://127.0.0.1:8000 \
  --device-id=swt-test-001 \
  --device-key='DockerDeviceKey2026!' \
  --dashboard-port=8765
```

Open `http://127.0.0.1:8765`, `/health`, and `/admin/customers`; then validate normal operation, sensor faults, dry-run, pump failure, device/slave offline, municipal state, telemetry freshness, events/alerts, and command acknowledgements. Use `./scripts/setup_test_env.sh --skip-tests` for startup only or `--reset-database` when test data may be deleted. Full manual pytest commands and troubleshooting are in the [test harness README](../swt_test_cases_project/README.md) and [Word guide](../swt_test_cases_project/docs/SaleWell_Virtual_Device_Test_Setup_WSL.docx).
