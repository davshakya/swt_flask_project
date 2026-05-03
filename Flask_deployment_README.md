# Smart Water Tank Flask Deployment Guide

Last refreshed: `2026-04-30`

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

- `server.py` exposes the Flask app as both `app` and `application`
- `passenger_wsgi.py` exposes the same Flask app as both `app` and `application` for hosts that auto-load Passenger's default file
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
/home/<cpanel-user>/repositories/swt_flask_project
/home/<cpanel-user>/swt_data/tank.db
```

Keep the app code outside `public_html` when possible and let cPanel map the domain to the Passenger app.

Important:

- the remote upload folder must match the `Application root` you configure in cPanel
- this guide uses `repositories/swt_flask_project` as the recommended cPanel application root
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
- Passive transfer command: EPSV by default, with PASV available via `--passive-command pasv`
- Remote root default: empty
- With an empty remote root, files upload into the FTP account's current login directory

Run it from the project root:

```powershell
python .\scripts\upload_repo_ftps.py
```

For repeated uploads, store the FTP password in a protected local config file that the uploader skips, such as `device.env`:

```text
SWT_FTP_PASSWORD=replace-with-ftp-password
```

Dry run:

```powershell
python .\scripts\upload_repo_ftps.py --dry-run
```

If upload fails during `STOR` with a data-connection timeout, try the older PASV passive command:

```powershell
python .\scripts\upload_repo_ftps.py --passive-command pasv
```

Active mode is available for diagnosis:

```powershell
python .\scripts\upload_repo_ftps.py --active-mode
```

If active mode says the server will not open a connection to a `192.168.x.x` address, active FTP is being blocked by NAT. Use passive mode and ask the host to open/fix the FTPS passive data port range for this FTP account.

You can also shorten or lengthen the socket timeout while diagnosing the host:

```powershell
python .\scripts\upload_repo_ftps.py --timeout 30
```

If your cPanel `Application root` is `repositories/swt_flask_project`, override the remote folder when uploading:

```powershell
python .\scripts\upload_repo_ftps.py "your-ftp-password" --remote-root repositories/swt_flask_project
```

If your server certificate starts working correctly later and you want normal FTPS validation again:

```powershell
python .\scripts\upload_repo_ftps.py --secure-ftps
```

Security note:

- command-line FTP passwords are not accepted because they can expose secrets in shell history
- the uploader checks the process environment first, then protected local config files such as `device.env`
- the uploader intentionally skips runtime files such as `device.env`, `.env`, local databases, `data/`, and logs; create or update those files directly on the server
- if `DEFAULT_REMOTE_ROOT = ""`, keep it that way unless you explicitly want uploads to go into a subfolder

## cPanel Startup Target

For the current working cPanel setup, use [`passenger_wsgi.py`](passenger_wsgi.py) as the startup file and `application` as the entry point.

Why:

- `passenger_wsgi.py` adds the project root to `sys.path`
- `passenger_wsgi.py` imports `server.app` and exposes it as `application`
- `server.py` still exposes `app` and `application`, so direct WSGI imports remain compatible
- the known-good cPanel values for the SaleWell host are `passenger_wsgi.py` and `application`

The repo also keeps [`passenger_wsgi.py`](passenger_wsgi.py) compatible for hosts that auto-load Passenger's default `passenger_wsgi.py` file. It exposes the same app as `application`, so either cPanel mode can boot the backend.

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
```

Notes:

- create the MySQL database and assign the MySQL user in cPanel before restarting the app
- leave `RELAY_STATUS_URLS` and `RELAY_COMMAND_URLS` blank when `salewell.co.in` is the main backend
- blank relay settings avoid accidental forwarding to another server or back into the same app

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
- Application path: `repositories/swt_flask_project`
- Environment: `Production`

After the app is registered, make sure the app uses the repo root as the source directory.

### Option B: Setup Python App / Python App

Some hosts show a Python app form with startup fields. If you see those fields, use:

- Python version: `3.11.15`
- Application root: `repositories/swt_flask_project`
- Application URL domain: `salewell.co.in`
- Application URL path: leave the path box empty
- Startup file: `passenger_wsgi.py`
- Entry point: `application`

If the URL path field does not allow blank, use `/`.

### Exact values for the cPanel screen

If your cPanel page looks like the Python form with these fields:

- `Python version`
- `Application root`
- `Application URL`
- `Application startup file`
- `Application Entry point`

then enter the values from the working SaleWell cPanel deployment:

| Field | Value |
| --- | --- |
| Python version | `3.11.15` |
| Application root | `repositories/swt_flask_project` |
| Application URL | domain `salewell.co.in`, path box empty |
| Application startup file | `passenger_wsgi.py` |
| Application Entry point | `application` |

Important:

- `Application root` is relative to your cPanel home directory
- the app files should end up in `/home/<cpanel-user>/repositories/swt_flask_project/`
- this setup makes `https://salewell.co.in/` serve the Flask app
- the current known-good cPanel branch is `swt_cpanel_changes`
- `passenger_wsgi.py` imports `server.app` and exposes it as `application`
- `server.py` also exposes both `app` and `application`, but the working cPanel screen uses `passenger_wsgi.py` and `application`

### Working public_html .htaccess

The Python app should create the Passenger mapping in `/home/<cpanel-user>/public_html/.htaccess`.
For the current SaleWell host, the working content is:

```apache
# DO NOT REMOVE. CLOUDLINUX PASSENGER CONFIGURATION BEGIN
PassengerAppRoot "/home/salewellco/repositories/swt_flask_project"
PassengerBaseURI "/"
PassengerPython "/home/salewellco/virtualenv/repositories/swt_flask_project/3.11/bin/python"
PassengerAppType wsgi
PassengerStartupFile passenger_wsgi.py
# DO NOT REMOVE. CLOUDLINUX PASSENGER CONFIGURATION END
```

Do not duplicate the CloudLinux Passenger block. If `.htaccess` contains two Passenger blocks, keep only one block and make it match the values above.

If `https://salewell.co.in/` shows `Index of /`, the domain is still serving `public_html` directly instead of the Passenger app. Check that:

- cPanel Python app `Application URL` is `salewell.co.in/`
- cPanel Python app `Application root` is `repositories/swt_flask_project`
- `.htaccess` exists in `public_html` and hidden files are visible in File Manager
- the Passenger block points to `/home/salewellco/repositories/swt_flask_project`
- the app was restarted after saving Python app settings and `.htaccess`

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
/home/<cpanel-user>/repositories/swt_flask_project/
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
- the SQLite file is being written under the persistent path you configured

## Quick Create Checklist

1. In cPanel, create the Python app with the values listed above.
2. Upload the repo into the same folder you configured as the cPanel `Application root`.
3. Recommended path: `/home/<cpanel-user>/repositories/swt_flask_project/`.
4. Create `/home/<cpanel-user>/swt_data/`.
5. Add `flask_app/.env`.
6. Add `device.env`.
7. Install dependencies with `pip install -r requirements.txt`.
8. Restart the app.
9. Open `https://salewell.co.in/health`.

## Important Production Notes

- keep `SESSION_COOKIE_SECURE=true` because the site should run on HTTPS
- replace every `change-me` value before first launch
- back up the SQLite database regularly
- keep `DB_FILE` on persistent storage
- keep firmware artifact storage on persistent storage too; by default it is placed beside the active SQLite database
- `requirements.txt` already includes the dependency needed by `/ml/predict`

## Common Problems

### 500 Internal Server Error

Check:

- `stderr.log` in the app directory
- missing environment variables
- missing dependencies
- wrong Passenger startup file
- for the current SaleWell cPanel setup, keep the startup file as `passenger_wsgi.py` and the entry point as `application`

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
