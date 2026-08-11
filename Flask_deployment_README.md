# SaleWell Smart Tank Flask Deployment Guide

Last refreshed: `2026-08-11`

This guide is specifically for deploying the `swt_flask_project` backend from the wider SaleWell IoT Solutions workspace on cPanel with Passenger WSGI.

The backend is only the remote dashboard and command layer. Field devices keep their local control loops on the ESP32 master (or a supported legacy ESP8266 master), so they continue working if this Flask deployment is offline.

Use this file together with:

- [`README.md`](README.md) for local setup, API surface, scripts, and testing
- [`scripts/upload_repo_ftps.py`](scripts/upload_repo_ftps.py) if you are pushing code to the host over FTPS
- [`../docs/CPANEL_CAPACITY_DEPLOYMENT_GUIDE.md`](../docs/CPANEL_CAPACITY_DEPLOYMENT_GUIDE.md) for the phase-by-phase capacity schema, feature-flag, cron, validation, and rollback rollout

The production capacity rollout is currently held after Phase 1 because cPanel
LSAPI process saturation and shared-MySQL connection/lock pressure were observed
during the Phase 2 trial. Keep Phase 2 and later write-path flags disabled until
the recovery gates in the [production incident section](../docs/CPANEL_CAPACITY_DEPLOYMENT_GUIDE.md#production-incident-phase-2-blocked-by-cpanel-resource-pressure)
have passed.

The deployment target referenced in this repo today is `https://salewell.co.in/`.

## What This Repo Uses

Important files in this project:

```text
swt_flask_project/
├── server.py
├── passenger_wsgi.py
├── requirements.txt
├── device.env
├── flask_app/
│   ├── server.py
│   ├── .env
│   ├── templates/
│   └── static/
```

How it works:

- `server.py` exposes the Flask app as `app`
- `passenger_wsgi.py` exposes that app to Passenger as `application`
- `requirements.txt` is the single dependency file for the whole project
- `device.env` is the single local file for backend, database, notification,
  shared device, and cloud settings

On the first restart after this release, startup schema maintenance adds the
persisted `device_service_configs.device_setup_type` column when it is missing.
Back up MySQL first, restart Passenger once, and confirm a Device Setup Type can
be saved and reloaded from the device-detail page. Presets send automatic
scenario commands only when current telemetry identifies simulator-enabled
test firmware; production devices receive service flags only.

## Scope

This document focuses on:

- cPanel application creation
- file placement
- Passenger startup settings
- environment/config files
- dependency installation
- restart and validation steps

SSH/Terminal access is optional. Use cPanel Backup/phpMyAdmin, File Manager or
FTPS, Setup Python App, Restart, Cron Jobs, and Metrics/Errors when shell access
is unavailable. The phase-by-phase capacity guide documents the UI equivalents.

It does not replace the main backend README, which covers routes, background jobs, mobile APIs, service controls, firmware artifacts, virtual devices, ML tooling, and day-to-day development.

## Recommended cPanel Layout

Use the domain root for this app:

- Domain: `salewell.co.in`
- Base URL: `/`

Do not deploy this app under a subfolder like `/tankapp`.

Reason:

- the PWA manifest uses root-scoped paths
- the service worker is registered at `/service-worker.js`
- several templates link to absolute routes such as `/login/admin`

Recommended server-side paths:

```text
/home/<cpanel-user>/apps/swt_flask_project
/home/<cpanel-user>/swt_data/
```

Keep the app code outside `public_html` when possible and let cPanel map the domain to the Passenger app.

Important:

- the remote upload folder must match the `Application root` you configure in cPanel
- this guide uses `apps/swt_flask_project` as the recommended cPanel application root
- if your FTP hosting layout requires a different folder such as `salewell.co.in/swt_flask`, use that same folder consistently in both cPanel and your upload command

## Files To Upload

Upload the whole project folder, including:

- `server.py`
- `passenger_wsgi.py`
- `requirements.txt`
- `flask_app/`
- `device.env`

Do not upload:

- `.venv/`
- `__pycache__/`
- local test databases

## Upload With The Python FTPS Script

This repo now includes a Python uploader:

- [`scripts/upload_repo_ftps.py`](scripts/upload_repo_ftps.py)

Current built-in defaults:

- FTP server: `ftp.salewell.co.in`
- Port: `21`
- Username: `swt_flask@salewell.co.in`
- Protocol: explicit FTPS
- Compatibility mode: FTPS certificate validation is disabled by default because this host presented a certificate hostname mismatch during testing
- Remote root default: empty
- With an empty remote root, files upload into the FTP account's current login directory

Run it from the project root:

```powershell
python .\scripts\upload_repo_ftps.py "your-ftp-password"
```

Dry run:

```powershell
python .\scripts\upload_repo_ftps.py "your-ftp-password" --dry-run
```

If your cPanel `Application root` is `apps/swt_flask_project`, override the remote folder when uploading:

```powershell
python .\scripts\upload_repo_ftps.py "your-ftp-password" --remote-root apps/swt_flask_project
```

If your server certificate starts working correctly later and you want normal FTPS validation again:

```powershell
python .\scripts\upload_repo_ftps.py "your-ftp-password" --secure-ftps
```

Security note:

- passing a password on the command line can expose it in shell history
- using an environment variable or prompt is safer, but the script supports a positional password for convenience
- if `DEFAULT_REMOTE_ROOT = ""`, keep it that way unless you explicitly want uploads to go into a subfolder

## cPanel Startup Target

For this cPanel setup, use the root [`server.py`](server.py) file directly.

Why:

- `server.py` already exposes the Flask WSGI app as `app`
- using `passenger_wsgi.py` as the cPanel startup target caused a recursive self-load on this host
- the stable cPanel values for this project are `server.py` and `app`

## Required Configuration Files

Create `device.env` before first production boot, starting from
`device.env.example`. Legacy `.env` and `flask_app/.env` files are not loaded.

### Recommended `device.env` values

Use real secrets, not the placeholder values:

```dotenv
APP_SECRET_KEY=replace-with-a-long-random-secret
SWT_HOSTING_PROFILE=cpanel
DB_BACKEND=mysql
DATABASE_URL=
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=<cpanel-user>_swtadmin
MYSQL_PASSWORD=replace-with-mysql-password
MYSQL_DATABASE=<cpanel-user>_swtadmin
MYSQL_AUTO_CREATE_DATABASE=false
MYSQL_CONNECT_TIMEOUT_SECONDS=5
MYSQL_READ_TIMEOUT_SECONDS=20
MYSQL_WRITE_TIMEOUT_SECONDS=20
MYSQL_LOCK_WAIT_TIMEOUT_SECONDS=5
MYSQL_OPTIMIZE_ENABLED=false
BACKGROUND_DB_MAX_WORKERS=1
ANALYTICS_SYNC_EVENTS_ON_REQUEST=false
DASHBOARD_SUMMARY_RECONCILIATION_ENABLED=false
LOGIN_USERNAME=admin
LOGIN_PASSWORD=replace-with-a-strong-password
CUSTOMER_COMMUNICATION_FROM_EMAIL=support@salewell.co.in
CUSTOMER_COMMUNICATION_FROM_NAME=SaleWell Smart Tank Support
SMTP_HOST=mail.salewell.co.in
SMTP_PORT=465
SMTP_USERNAME=support@salewell.co.in
SMTP_PASSWORD=replace-with-support-mailbox-password
SMTP_USE_TLS=false
SMTP_USE_SSL=true
WHATSAPP_TEAM_PHONE=918796452878
# Set this only after deploying a real provider/proxy endpoint.
# Example if you host the proxy on your SaleWell domain:
# WHATSAPP_WEBHOOK_URL=https://salewell.co.in/integrations/whatsapp/send
WHATSAPP_WEBHOOK_URL=
WHATSAPP_WEBHOOK_SECRET=replace-with-long-random-webhook-secret
WHATSAPP_PROVIDER=meta
WHATSAPP_META_GRAPH_VERSION=v24.0
WHATSAPP_META_PHONE_NUMBER_ID=replace-with-meta-phone-number-id
WHATSAPP_META_ACCESS_TOKEN=replace-with-meta-access-token
WHATSAPP_META_CONFIRMATION_TEMPLATE=salewell_demo_confirmation
WHATSAPP_META_TEAM_TEMPLATE=salewell_team_new_enquiry
WHATSAPP_META_TEMPLATE_LANGUAGE=en
SESSION_COOKIE_SECURE=true
SESSION_COOKIE_SAMESITE=Lax
RELAY_STATUS_URLS=
RELAY_COMMAND_URLS=
```

Notes:

- create the MySQL database and assign the MySQL user in cPanel before restarting the app
- keep `MYSQL_AUTO_CREATE_DATABASE=false`; shared cPanel database users normally cannot create databases
- keep `MYSQL_OPTIMIZE_ENABLED=false`; use cPanel/phpMyAdmin maintenance during a planned window instead of locking production tables from a web request
- the one-worker background database limit is intentional for the account's restricted memory/process allowance
- ordinary reads may reconnect and retry once after a dropped connection; writes, DDL, and locking reads are never replayed automatically
- if `DATABASE_URL` is used instead of `MYSQL_*`, percent-encode special characters in its username and password
- create or verify the cPanel mailbox `support@salewell.co.in`; `SMTP_PASSWORD` must be that mailbox password for customer forgot-password emails
- `WHATSAPP_TEAM_PHONE=918796452878` sends internal demo/enquiry notifications to the SaleWell team number
- `WHATSAPP_WEBHOOK_URL` must point to a real WhatsApp Business provider/proxy endpoint; use `https://salewell.co.in/integrations/whatsapp/send` only if you deploy that route on `salewell.co.in` and it forwards to Meta WhatsApp Cloud API, Twilio, WATI, Interakt, AiSensy, or another provider
- for the built-in Meta Cloud API route, set `WHATSAPP_WEBHOOK_SECRET`, `WHATSAPP_META_PHONE_NUMBER_ID`, `WHATSAPP_META_ACCESS_TOKEN`, and create approved Meta templates named `salewell_demo_confirmation` and `salewell_team_new_enquiry`
- leave `RELAY_STATUS_URLS` and `RELAY_COMMAND_URLS` blank when `salewell.co.in` is the main backend
- blank relay settings avoid accidental forwarding to another server or back into the same app

Meta template examples:

`salewell_demo_confirmation` body with 3 variables:

```text
Hi {{1}}, thanks for booking a SaleWell Smart Tank demo/enquiry. We received your request for {{2}}. Our team will contact you soon on WhatsApp for {{3}}.
```

`salewell_team_new_enquiry` body with 6 variables:

```text
New SaleWell Smart Tank enquiry.
Name: {{1}}
Phone: {{2}}
City: {{3}}
Project: {{4}}
Devices: {{5}}
Plan: {{6}}
```

### Recommended `device.env` values

```dotenv
SWT_DEVICE_ID=swt-000-000-000-001
SWT_DEVICE_API_KEY=replace-with-a-real-device-key
SWT_LOCAL_WEB_AUTH_USERNAME=swtadmin
SWT_LOCAL_WEB_AUTH_PASSWORD=replace-with-a-strong-local-password
SWT_LOCAL_DEVICE_URL=
SWT_CLOUD_BASE_URL=https://salewell.co.in/
SWT_FLASK_CHANNEL_MODE=cloud
SWT_DEVICE_SOURCE_MODE=real
DEVICE_AUTH_REQUIRED=1
AUTO_REGISTER_DEVICE_KEYS=0
AUTO_REGISTER_DEVICE_ID_PREFIXES=swt-
AUTO_REGISTER_DEVICE_KEY_MIN_LENGTH=32
CUSTOMER_COMMUNICATION_FROM_EMAIL=support@salewell.co.in
CUSTOMER_COMMUNICATION_FROM_NAME=SaleWell Smart Tank Support
SMTP_HOST=mail.salewell.co.in
SMTP_PORT=465
SMTP_USERNAME=support@salewell.co.in
SMTP_PASSWORD=replace-with-support-mailbox-password
SMTP_USE_TLS=false
SMTP_USE_SSL=true
SMTP_TIMEOUT_SECONDS=10
WHATSAPP_TEAM_PHONE=918796452878
WHATSAPP_WEBHOOK_URL=
WHATSAPP_WEBHOOK_SECRET=replace-with-long-random-webhook-secret
WHATSAPP_PROVIDER=meta
WHATSAPP_META_GRAPH_VERSION=v24.0
WHATSAPP_META_PHONE_NUMBER_ID=replace-with-meta-phone-number-id
WHATSAPP_META_ACCESS_TOKEN=replace-with-meta-access-token
WHATSAPP_META_CONFIRMATION_TEMPLATE=salewell_demo_confirmation
WHATSAPP_META_TEAM_TEMPLATE=salewell_team_new_enquiry
WHATSAPP_META_TEMPLATE_LANGUAGE=en
```

If you will manage multiple devices, use `SWT_DEVICE_KEYS` in `device.env` for already-known devices. For new devices after deployment, register the device ID and API key from the admin customer page; Flask stores the key hash in the database and no server restart is needed.

The project-level `device.env` is allowed to override SMTP settings even when cPanel environment variables are missing or stale. Restart the Python app after editing `device.env`.

## cPanel Setup Steps

### Option A: Application Manager

If your host provides `Application Manager`, use:

- Application name: `salewell`
- Deployment domain: `salewell.co.in`
- Base application URL: `/`
- Application path: `apps/swt_flask_project`
- Environment: `Production`

After the app is registered, make sure the app uses the repo root as the source directory.

### Option B: Setup Python App / Python App

Some hosts show a Python app form with startup fields. If you see those fields, use:

- Python version: `3.11.14`
- Application root: `apps/swt_flask_project`
- Application URL domain: `salewell.co.in`
- Application URL path: leave the path box empty
- Startup file: `server.py`
- Entry point: `app`

If the URL path field does not allow blank, use `/`.

### Exact values for the cPanel screen

If your cPanel page looks like the Python form with these fields:

- `Python version`
- `Application root`
- `Application URL`
- `Application startup file`
- `Application Entry point`

then enter exactly:

| Field | Value |
| --- | --- |
| Python version | `3.11.14` |
| Application root | `apps/swt_flask_project` |
| Application URL | domain `salewell.co.in`, path box empty |
| Application startup file | `server.py` |
| Application Entry point | `app` |

Important:

- `Application root` is relative to your cPanel home directory
- the app files should end up in `/home/<cpanel-user>/apps/swt_flask_project/`
- this setup makes `https://salewell.co.in/` serve the Flask app
- do not set the startup file to `passenger_wsgi.py` for this host; use `server.py`
- do not set the entry point to `application`; use `app`

### Environment variables section in cPanel

For this repository, the cleanest setup is:

- leave the cPanel `Environment variables` section empty
- create `device.env` in the app folder instead

This project already loads those files automatically on startup.

## Install Dependencies

Install packages from the project root:

```bash
pip install -r requirements.txt
```

If your cPanel UI has a dependency install button such as `Run Pip Install` or `Enable Dependencies`, run it after upload.

## Folder Preparation

Before the first restart, make sure this path exists on the server for upload artifacts:

```text
/home/<cpanel-user>/apps/swt_flask_project/
/home/<cpanel-user>/swt_data/
```

## Restart The App

After config or code changes:

- use the cPanel restart button if available
- or touch `tmp/restart.txt` inside the app root

Passenger only reloads the app after a restart signal.

## First URLs To Test

Open these after deployment:

- `https://salewell.co.in/health`
- `https://salewell.co.in/login/admin`
- `https://salewell.co.in/login/customer`

Healthy signs:

- `/health` returns a success response
- admin login page opens
- templates and static files load correctly

After the basic smoke test, also verify:

- the device can post telemetry to `https://salewell.co.in/status`
- `/api/mobile/bootstrap` responds after a successful mobile login
- `/api/mobile/device/services` responds after a successful mobile login
- customer login works for at least one mapped device account
- admin firmware uploads land in the configured artifact directory
- admin Android APK uploads are visible at `/static/version.json` and `/downloads/android/latest.apk`
- MySQL tables are created in the configured database

## Quick Create Checklist

1. In cPanel, create the Python app with the values listed above.
2. Upload the repo into the same folder you configured as the cPanel `Application root`.
3. Recommended path: `/home/<cpanel-user>/apps/swt_flask_project/`.
4. Create `/home/<cpanel-user>/swt_data/`.
5. Add `device.env` from `device.env.example`.
6. Add `device.env`.
7. Install dependencies with `pip install -r requirements.txt`.
8. Restart the app.
9. Open `https://salewell.co.in/health`.

## Important Production Notes

- keep `SESSION_COOKIE_SECURE=true` because the site should run on HTTPS
- replace every `change-me` value before first launch
- back up the MySQL database regularly
- keep firmware artifact storage on persistent storage
- keep Android release storage on persistent storage
- `requirements.txt` already includes the dependency needed by `/ml/predict`

## Common Problems

### 500 Internal Server Error

Check:

- `stderr.log` in the app directory
- missing environment variables
- missing dependencies
- wrong Passenger startup file
- if the log shows `imp.load_source(... 'passenger_wsgi.py')` repeating, switch cPanel to `server.py` with entry point `app`

### Login opens but session does not persist

Check:

- `APP_SECRET_KEY` is set
- `SESSION_COOKIE_SECURE=true`
- the site is actually loading over `https://salewell.co.in`

### State disappears after restart

Check:

- MySQL service is running
- `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, and `MYSQL_DATABASE` are correct
- the MySQL user is assigned to the database
- `APP_SECRET_KEY` is stable and not changing between restarts

### Device telemetry does not reach the dashboard

Check:

- `SWT_DEVICE_ID`
- `SWT_DEVICE_API_KEY`
- `SWT_CLOUD_BASE_URL=https://salewell.co.in/`
- whether the device is posting to `/status`
- whether the `swt_esp32_master` firmware (or legacy `swt_master`) has telemetry service enabled and the matching device key is registered in Flask

## Related Workspace Docs

- Workspace overview: [`../README.md`](../README.md)
- Backend overview: [`README.md`](README.md)
- Firmware project: [`../swt_firmware_project/README.md`](../swt_firmware_project/README.md)
- Android project: [`../swt_android_app_project/README.md`](../swt_android_app_project/README.md)

## Deployment Summary

```text
Upload repo -> create Passenger app -> set device.env ->
install requirements -> restart app -> test /health and /login/admin
```
