# Production Readiness Guide

Last refreshed: `2026-04-30`

This project is beyond lab-only status, but it still needs deliberate rollout controls before broad customer deployment. Use this guide as the current go-live checklist.

## 1. Current Readiness Position

As the repo stands today:

- firmware, Flask, and Android flows are implemented
- automated tests exist for Flask, API, UI, ML helpers, and virtual-device tooling in `../swt_test_cases_project`
- Render deployment wiring exists for MySQL-backed hosting
- cPanel/Passenger deployment guidance exists in `../Flask_deployment_README.md`
- pilot and support workflows still need disciplined execution before scale

The right framing today is:

- suitable for internal testing and controlled pilots
- not a plug-and-play mass-deployment product without rollout discipline

Commercial deployments must pass the workspace-level gate in
[`../../COMMERCIAL_DEPLOYMENT.md`](../../COMMERCIAL_DEPLOYMENT.md), including:

- `python scripts\production_preflight.py --env-file <secure-production-env>`
- CI test/build completion
- signed Android release when Android is delivered
- hardware sign-off and field acceptance

## 2. Identity, Secrets, And Access

Before any field install:

- assign a unique `SWT_DEVICE_ID` to every controller
- assign a unique API key to every controller
- register device credentials in Flask before deployment
- replace default dashboard credentials
- replace default firmware UI credentials
- keep secrets out of Git and screenshots

Minimum secret set to rotate:

- `SWT_DEVICE_API_KEY`
- firmware UI password
- Flask admin password
- `APP_SECRET_KEY`

## 3. Backend Persistence And Hosting

The backend is stateful today because it stores:

- telemetry history
- command queues
- alerts and audit data
- customer accounts
- persisted dashboard/auth settings
- device service settings
- firmware artifact metadata and uploaded firmware binaries

For hosted deployment:

- use MySQL/MariaDB only
- back up the database regularly
- avoid ephemeral filesystems for uploaded firmware or Android artifacts
- monitor database size and retention windows

The repo's [`../render.yaml`](../render.yaml) expects a MySQL/MariaDB connection through `DATABASE_URL` or `MYSQL_*` values.

## 4. Monitoring And Alerting

Validate these routes before rollout:

- `/health`
- `/system/status`
- `/relay/health`
- `/monitoring/summary`
- `/monitoring/alerts`
- `/monitoring/audit`
- `/events`

Production alert categories to verify end-to-end:

- stale telemetry
- sensor fault
- dry-run
- pump failure
- abnormal or leak-related flags
- relay queue buildup
- cloud relay failures

If you use external notification channels, test your current webhook configuration before site handover.

## 5. Firmware Readiness

Before field rollout:

- confirm the correct `device.env` values were used for the flashed device
- confirm the device reports the right `device_id`
- confirm Wi-Fi reset and rejoin workflow are documented for support
- confirm local firmware upload/update behavior on a non-critical device before using it for customer updates

Current repo note:

- `SWT_FORCE_BUILD_WEB_AUTH_PASS=1` means flashing will restore the build/default firmware UI password
- `SWT_FORCE_BUILD_DEVICE_ID=0` preserves a stored exact provisioned device ID across later flashes

Those are useful for controlled rollouts but should be understood by installers and support staff.

## 6. Hardware Safety Readiness

Do not treat software readiness as electrical readiness.

Before production:

- verify relay or contactor sizing against the real pump circuit
- verify default fail-safe relay behavior on boot, reset, and power loss
- use proper isolation and protection on the motor path
- validate power stability with the real relay load attached
- protect sensor and logic wiring from noise, splash, and corrosion

Use [`../../swt_firmware_project/docs/HARDWARE_HARDENING.md`](../../swt_firmware_project/docs/HARDWARE_HARDENING.md) as the field checklist.

## 7. Validation Before Customer Rollout

Minimum validation set:

- complete a 7-day bench soak
- complete at least one real-site pilot
- validate the enclosure against the exact board, relay/control module, sensor board, connectors, and cable bend radius
- validate start/stop thresholds on the actual tank
- validate Wi-Fi recovery after router restart
- validate safe behavior during sensor faults
- validate customer and admin login paths
- validate Android local and cloud flows if the app is part of delivery
- validate service-control settings and firmware artifact workflow if those features are part of support

Keep soak-test and pilot evidence with the project records. Use
[`../../swt_firmware_project/docs/FIELD_VALIDATION.md`](../../swt_firmware_project/docs/FIELD_VALIDATION.md)
and [`../../swt_3d_print_enclosure/ENCLOSURE_VALIDATION.md`](../../swt_3d_print_enclosure/ENCLOSURE_VALIDATION.md)
as the release checklists.

## 8. Support And Installer Readiness

Before wider rollout:

- define who owns flashing and device labeling
- define who owns Wi-Fi onboarding
- define who owns customer account creation
- define who handles first-line support
- prepare screenshots or exact endpoints support should request first
- confirm the runbook is usable by someone other than the main developer

Use [`INSTALLER_SUPPORT_RUNBOOK.md`](INSTALLER_SUPPORT_RUNBOOK.md) during rollout.

## 9. Go / No-Go Checklist

Treat rollout as `GO` only when all are true:

- unique device ID and API key are assigned
- production secrets replaced the defaults
- persistent backend storage is confirmed
- telemetry, command queue, and alert routes are healthy
- 7-day soak is complete
- pilot feedback is reviewed
- enclosure validation is complete for the exact hardware variant
- hardware wiring review is signed off
- installer handover checklist is ready
- rollback and reflash path is documented

If any of those is false, stay in pilot mode.
