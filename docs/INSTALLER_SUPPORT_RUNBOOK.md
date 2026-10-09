# Installer And Support Runbook

Current Wi-Fi and command reference (2026-10-07): [setup, generic device-number build/upgrade, automatic master discovery, and password-free progress logs](../../docs/WIFI_BUILD_UPGRADE_LOGS.md).

For the current ESP32 master, the hotspot name uses the last six device-ID
characters (example: `SWT_MASTER_SETUP_00-008`). It starts immediately without
saved Wi-Fi, or after 90 seconds of a saved-network outage. Use the local
maintenance login at `http://192.168.4.1/wifi/recovery`; new router credentials
are tested for up to 45 seconds and saved after five stable seconds. A failed
trial retains the previous network, and setup does not reboot the controller.
The Android device Wi-Fi flow supports both the setup hotspot and an existing
network. Never include Wi-Fi passwords or API keys in logs or support captures.


Firmware workflow updated 2026-10-03: [build, automatic hardware MAC detection, latest-package install, and repeater recovery](../../swt_firmware_project/docs/CURRENT_FIRMWARE_WORKFLOW.md).

Last refreshed: `2026-10-03`

Use this runbook to keep installations, handovers, and first-line support consistent across the current SaleWell IoT Solutions stack.

Give customers the [simple English/Hindi PDF guide](SaleWell-Smart-Tank-Customer-Installation-Guide-English-Hindi.pdf). Use the [technical bilingual guide](INSTALLATION_GUIDE_EN_HI.md) and this runbook for installation, commissioning, and support work.

## 1. Pre-Install Preparation

Before traveling to site:

- assign the final `device_id`
- confirm the matching device API key exists on the backend
- prepare the correct firmware build inputs
- upload the intended firmware artifact in Flask if a Wi-Fi firmware update is planned
- label the enclosure with the device ID
- carry the Wi-Fi setup steps and support contact path

Bring:

- flashed controller or laptop for flashing
- power supply and relay hardware
- sensor hardware and mounting accessories
- phone or laptop for Wi-Fi onboarding
- serial monitor access if possible

## 2. Installation Workflow

Recommended order at site:

1. mount the enclosure and sensor safely
2. confirm relay output is fail-safe with motor off
3. power the controller
4. provision Wi-Fi through `SWT_MASTER_SETUP_<device-suffix>` if needed
5. open the local firmware UI
6. confirm current level, motor status, and sensor health
7. confirm Flask receives telemetry
8. confirm the correct customer/admin account mapping
9. confirm service settings match the customer plan
10. verify Android local/cloud access when the app is part of delivery

## 3. Commissioning Checks

Minimum commissioning checklist:

- correct `device_id` is visible
- tank level looks believable
- motor is initially safe/off
- local UI login works
- Flask dashboard shows the same device
- `/monitoring/summary` is healthy
- Android local view and cloud login work when applicable
- service settings are correct for cloud feed, AI analysis, source tank, buzzer, and LED
- Wi-Fi reset path is understood

## 4. Handover Checklist

Before leaving site:

- customer has the correct dashboard URL
- customer knows whether they use local, cloud, or both
- customer login is verified
- device ID and site name are recorded
- firmware version is recorded
- local Wi-Fi reset flow is explained
- expected alarm or status behavior is explained
- support contact path is recorded

## 5. First-Line Support Intake

Ask for these first:

- site name
- device ID
- current symptom
- screenshot of dashboard if available
- screenshot of `/system/status` if they have admin access
- screenshot of `/monitoring/alerts`
- recent device log or serial output if available

## 6. Common Triage Paths

### Telemetry is stale

- confirm power is present
- confirm Wi-Fi is connected
- confirm Flask host is reachable
- check `/monitoring/summary`
- check `/relay/health`

### Sensor reading looks wrong

- inspect mounting height and splash conditions
- inspect JSN-SR04T wiring and echo-line protection
- compare the dashboard reading against a manual tank-level check

### Motor does not start or stop as expected

- confirm current mode is `AUTO` vs manual
- check source tank blocking if that feature is enabled
- check dry-run or abnormal flags
- review recent events and alerts

### Customer cannot access local firmware UI

- confirm current LAN IP
- confirm firmware UI password
- remember that flashing with current repo defaults may restore the build password

### Router changed or password changed

- use the Wi-Fi reset path
- reconnect to `SWT_MASTER_SETUP_<device-suffix>`
- reprovision the device

### Firmware update fails

- confirm the phone is on the same Wi-Fi as the controller
- confirm local firmware credentials are current
- confirm the local device ID matches the cloud device selected in the app
- confirm a firmware artifact exists for that device in Flask
- fall back to USB flashing if local Wi-Fi upload is not reliable

## 7. Escalation Triggers

Escalate beyond first-line support when:

- relay or motor safety is in question
- repeated reboots continue after reprovisioning
- dry-run or pump-failure alerts appear repeatedly without clear cause
- firmware upload/update repeatedly fails on known-good Wi-Fi
- MySQL/MariaDB persistence or hosted deployment health appears compromised


## Android changes reviewed 2026-10-01

For ?System Health Good but Tank update delayed remains Active,? inspect the alert timestamp, device identity, and current local versus cloud connectivity. Android repairs old GMT-date cache entries and can show local resolution only with newer fresh same-device telemetry. Reading an alert hides its unread badge without resolving it. Ask customers to use Email Support: support@salewell.co.in and review the prefilled details before sending.

See [Android alerts, support, and build versions](../../docs/ANDROID_ALERTS_AND_BUILDS.md) for behavior, limitations, and validation details.
