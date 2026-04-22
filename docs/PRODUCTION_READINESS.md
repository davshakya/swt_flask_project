# Production Readiness Guide

Last refreshed: `2026-04-03`

This project is beyond lab-only status, but it still needs deliberate rollout controls before broad customer deployment. Use this guide as the current go-live checklist.

## 1. Current Readiness Position

As the repo stands today:

- firmware, Flask, and Android flows are implemented
- automated tests exist for Flask and ML helpers
- Render deployment wiring exists with persistent disk support
- pilot and support workflows still need disciplined execution before scale

The right framing today is:

- suitable for internal testing and controlled pilots
- not a plug-and-play mass-deployment product without rollout discipline

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

For hosted deployment:

- use persistent storage for SQLite
- back up the DB regularly
- avoid ephemeral filesystems for production state
- monitor disk usage and retention windows

The repo already points Render at `/var/data/tank.db` in [render.yaml](/d:/Dev_Progs/smart_water_tank/render.yaml). Keep that pattern or an equivalent persistent DB strategy.

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

Current repo note:

- `FORCE_BUILD_WEB_AUTH_PASS=1` means flashing will restore the build/default firmware UI password
- `FORCE_BUILD_CHANNEL_MODE=1` means flashing will restore the build/default channel mode

Those are useful for controlled rollouts but should be understood by installers and support staff.

## 6. Hardware Safety Readiness

Do not treat software readiness as electrical readiness.

Before production:

- verify relay or contactor sizing against the real pump circuit
- verify default fail-safe relay behavior on boot, reset, and power loss
- use proper isolation and protection on the motor path
- validate power stability with the real relay load attached
- protect sensor and logic wiring from noise, splash, and corrosion

Use [HARDWARE_HARDENING.md](/d:/Dev_Progs/smart_water_tank/docs/HARDWARE_HARDENING.md) as the field checklist.

## 7. Validation Before Customer Rollout

Minimum validation set:

- complete a 7-day bench soak
- complete at least one real-site pilot
- validate start/stop thresholds on the actual tank
- validate Wi-Fi recovery after router restart
- validate safe behavior during sensor faults
- validate customer and admin login paths
- validate Android local and cloud flows if the app is part of delivery

Use [PILOT_SOAK_TEST_PLAN.md](/d:/Dev_Progs/smart_water_tank/docs/PILOT_SOAK_TEST_PLAN.md) as the execution plan.

## 8. Support And Installer Readiness

Before wider rollout:

- define who owns flashing and device labeling
- define who owns Wi-Fi onboarding
- define who owns customer account creation
- define who handles first-line support
- prepare screenshots or exact endpoints support should request first
- confirm the runbook is usable by someone other than the main developer

Use [INSTALLER_SUPPORT_RUNBOOK.md](/d:/Dev_Progs/smart_water_tank/docs/INSTALLER_SUPPORT_RUNBOOK.md) during rollout.

## 9. Go / No-Go Checklist

Treat rollout as `GO` only when all are true:

- unique device ID and API key are assigned
- production secrets replaced the defaults
- persistent backend storage is confirmed
- telemetry, command queue, and alert routes are healthy
- 7-day soak is complete
- pilot feedback is reviewed
- hardware wiring review is signed off
- installer handover checklist is ready
- rollback and reflash path is documented

If any of those is false, stay in pilot mode.
