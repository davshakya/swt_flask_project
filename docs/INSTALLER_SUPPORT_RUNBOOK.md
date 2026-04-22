# Installer And Support Runbook

Last refreshed: `2026-04-03`

Use this runbook to keep installations, handovers, and first-line support consistent across the current Smart Water Tank stack.

## 1. Pre-Install Preparation

Before traveling to site:

- assign the final `device_id`
- confirm the matching device API key exists on the backend
- prepare the correct firmware build inputs
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
4. provision Wi-Fi through `SMART_TANK_SETUP` if needed
5. open the local firmware UI
6. confirm current level, motor status, and sensor health
7. confirm Flask receives telemetry
8. confirm the correct customer/admin account mapping

## 3. Commissioning Checks

Minimum commissioning checklist:

- correct `device_id` is visible
- tank level looks believable
- motor is initially safe/off
- local UI login works
- Flask dashboard shows the same device
- `/monitoring/summary` is healthy
- Wi-Fi reset path is understood

## 4. Handover Checklist

Before leaving site:

- customer has the correct dashboard URL
- customer knows whether they use local, cloud, or both
- customer login is verified
- device ID and site name are recorded
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
- inspect HC-SR04 wiring
- confirm echo line protection
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
- reconnect to `SMART_TANK_SETUP`
- reprovision the device

## 7. Escalation Triggers

Escalate beyond first-line support when:

- relay or motor safety is in question
- repeated reboots continue after reprovisioning
- dry-run or pump-failure alerts appear repeatedly without clear cause
- SQLite persistence or hosted deployment health appears compromised
