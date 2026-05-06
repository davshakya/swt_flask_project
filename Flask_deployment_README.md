# Smart Water Tank Flask Deployment Guide

Last refreshed: `2026-05-06`

This guide is specifically for deploying the `swt_flask_project` backend from the wider Smart Water Tank workspace on cPanel with Passenger WSGI.

Use this file together with:

- [`README.md`](README.md) for local setup, API surface, scripts, and testing
- [`render.yaml`](render.yaml) if you are comparing cPanel deployment with the Render deployment shape
- [`scripts/upload_repo_ftps.py`](scripts/upload_repo_ftps.py) if you are pushing code to the host over FTPS

The deployment target referenced in this repo today is `https://salewell.co.in/`.

## What This Repo Uses

Important files in this project:

```text
swt_flask_project/
|-- server.py
|-- passenger_wsgi.py
|-- requirements.txt
|-- device.env
`-- flask_app/
    |-- server.py
    |-- .env
    |-- templates/
    `-- static/
```

How it works:

- `server.py` exposes the Flask app as `app`
- `passenger_wsgi.py` exposes that app to Passenger as `application`
- `requirements.txt` is the single dependency file for the whole project
- `flask_app/.env` holds backend settings
- `device.env` holds shared device/cloud settings

## Scope

This document focuses on:

- cPanel application creation
- file placement
- Passenger startup settings
- environment/config files
- dependency installation
- restart and validation steps

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

Create these two files before first production boot:

1. `flask_app/.env`
2. `device.env`

Start from:

- `flask_app/.env.example`
- `device.env.example`

### Recommended `flask_app/.env` values

Use real secrets, not the placeholder values:

```dotenv
APP_SECRET_KEY=replace-with-a-long-random-secret
DB_BACKEND=mysql
DATABASE_URL=
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=<cpanel-user>_swtadmin
MYSQL_PASSWORD=replace-with-mysql-password
MYSQL_DATABASE=<cpanel-user>_swtadmin
LOGIN_USERNAME=admin
LOGIN_PASSWORD=replace-with-a-strong-password
SESSION_COOKIE_SECURE=true
SESSION_COOKIE_SAMESITE=Lax
RELAY_STATUS_URLS=
RELAY_COMMAND_URLS=
AUTO_RELAY_LOCAL_TO_SHARED_CLOUD=false
```

Notes:

- create the MySQL database and assign the MySQL user in cPanel before restarting the app
- leave `RELAY_STATUS_URLS` and `RELAY_COMMAND_URLS` blank when `salewell.co.in` is the main backend
- blank relay settings are explicit clears, so a stale hosting-level relay environment value will not survive if `device.env` also has blank relay keys
- keep `AUTO_RELAY_LOCAL_TO_SHARED_CLOUD=false` on the public cloud host; use `true` only on a LAN Flask instance that should bridge low-heap device HTTP telemetry to cloud

### Recommended `device.env` values

```dotenv
SWT_DEVICE_ID=swt-000-000-000-001
SWT_DEVICE_API_KEY=replace-with-a-real-device-key
SWT_LOCAL_WEB_AUTH_USERNAME=swtadmin
SWT_LOCAL_WEB_AUTH_PASSWORD=replace-with-a-strong-local-password
SWT_LOCAL_DEVICE_URL=http://192.168.1.50/
SWT_CLOUD_BASE_URL=https://salewell.co.in/
SWT_FLASK_CHANNEL_MODE=both
SWT_DEVICE_SOURCE_MODE=real
```

If you will manage multiple devices, use `DEVICE_KEYS` or `SWT_DEVICE_KEYS` in `flask_app/.env` instead of relying on only one shared device ID and key.

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
- create `flask_app/.env` and `device.env` in the app folder instead

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
5. Add `flask_app/.env`.
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

## Related Workspace Docs

- Workspace overview: [`../README.md`](../README.md)
- Backend overview: [`README.md`](README.md)
- Firmware project: [`../swt_firmware_project/README.md`](../swt_firmware_project/README.md)
- Android project: [`../swt_android_app_project/README.md`](../swt_android_app_project/README.md)

## Deployment Summary

```text
Upload repo -> create Passenger app -> set flask_app/.env and device.env ->
install requirements -> restart app -> test /health and /login/admin
```
