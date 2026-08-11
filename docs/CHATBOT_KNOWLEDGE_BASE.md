# SaleWell Smart Water Tank — Public Chatbot Knowledge Base

Last refreshed: `2026-08-11`

Answers must stay within the shipped capabilities in
[`../../docs/PROJECT_DESIGN_AND_ARCHITECTURE.md`](../../docs/PROJECT_DESIGN_AND_ARCHITECTURE.md); future or unshipped clients must not be presented as available.

This document is written for website visitors, homeowners, apartment managers,
installers, dealers, and commercial customers. Prices and availability should
be confirmed with SaleWell before purchase because the final quote depends on
the site, tank count, pump wiring, city, installation scope, and optional
modules.

## What the SaleWell system does

SaleWell monitors water-tank level and includes automatic pump control, alerts, local
phone access, remote cloud access, history, reports, and water-usage insights,
depending on the selected plan and installed hardware.

A typical installation has a wireless sensor node near the tank and a main
controller near the pump panel. The sensor node sends tank readings directly
to the controller. Local-only plans do not require home internet. Cloud access
from outside the property requires a suitable internet connection.

The controller supplements the site's electrical safety wiring. It does not
replace an appropriate contactor, overload protection, MCB/fuse, earthing,
manual OFF control, or qualified electrical installation.

## Common benefits

- View the current tank level without climbing to the tank.
- Reduce overflow and unnecessary water loss.
- Receive status and alert information through supported app or cloud plans.
- Control or automate a pump when the required hardware is installed.
- Monitor source and overhead tanks in supported multi-tank configurations.
- Review history, usage trends, reports, and AI insights on eligible plans.
- Keep local tank control operating even when the cloud is unavailable.

## How overflow protection works

The tank sensor reports the water level to the controller. In an automatic-pump
installation, configured stop thresholds allow the controller to request pump
shutdown before the tank overflows. The exact threshold and safe shutdown
behavior are confirmed during commissioning. A working sensor, correct tank
calibration, suitable pump wiring, and a verified physical OFF circuit are
required.

If sensor information is missing, stale, or unreliable, the system should use
fail-safe behavior rather than treating the pump as safe to run. Customers
should never depend only on an app button as the pump's safety mechanism.

## Pump control and dry-run protection

Pump control is available only when the correct relay/contactor interface and
plan features are installed. High-current pumps must use an appropriately
rated contactor or power relay; a small controller relay must not directly
carry a load beyond its rating.

Dry-run protection depends on the installed source-water sensing or configured
water-availability logic. The controller can stop or prevent pumping when the
source is unavailable or when required safety data is not trustworthy. The
installer must test physical OFF, remote OFF, automatic OFF, and fail-safe
behavior before handover.

## Local access versus cloud access

Local access works at the property through the controller's supported local
connection. It can continue without internet, but it is not intended for
access while the customer is away from the property.

Cloud access allows eligible customers to check information remotely through
the internet. It can include history, reports, sharing, alerts, and analytics.
Temporary internet or server unavailability should not replace the controller's
local pump safety and automation logic.

## Current plan guide

### Home Basic

- Published price: Rs. 4,999 one-time equipment price.
- Intended for one home tank and controller setup at the lowest entry price.
- Includes the wireless tank sensor node and main controller.
- Includes automatic pump start/stop control and overflow cut-off.
- Does not include the phone app or cloud access.
- Home Wi-Fi and internet are not required.

### Home Control

- Published price: Rs. 5,999 one-time equipment and app price.
- Includes Home Basic capabilities plus live tank information on a phone while
  the customer is at the property.
- Uses a direct/local connection to the controller.
- It does not provide access from anywhere unless a cloud option is added.

### Home Cloud Pro

- Published price: Rs. 8,499 one-time equipment, app, and cloud-feature price.
- Intended for families, rental homes, and owners who want remote access.
- Includes remote access, family sharing, cloud history, usage trends, AI
  insights, and monthly water reporting.
- Requires suitable internet connectivity for cloud features.

### RWA Standard

- Published starting price: Rs. 10,999 per setup.
- Intended for apartments, RWAs, hostels, small hotels, schools, and shared
  buildings.
- Supports managed visibility, pump control, source-tank monitoring, supported
  overhead/underground tank arrangements, alerts, dashboard, and reports.

### Commercial AI Pro

- Published starting price: Rs. 13,999 per setup.
- Intended for large apartments, hotels, factories, and institutions.
- Adds multi-tank visibility, advanced reports, AI analytics, leak insight,
  source-to-destination logic, and priority support to eligible installations.
- Customers may optionally request a separately quoted monthly or annual
  maintenance contract. It is not a mandatory subscription.

### Dealer / Installer Kit

- Published starter-kit price: Rs. 12,999; volume pricing is discussed
  separately.
- Intended for dealers, plumbers, electricians, resellers, and local
  installation partners.
- Includes a working wireless demonstration kit, checklist, setup guidance,
  product menu, and partner follow-up support.

### Enterprise Modular

- Custom one-time pricing, usually starting from Rs. 25,999,.
- Intended for builders, townships, industrial customers, multi-site
  deployments, and large rollouts.
- Can include custom dashboards, integrations, reports, user roles, rollout
  support, and bulk device planning.
- Customers may optionally request a separately quoted monthly or annual
  maintenance contract. It is not a mandatory subscription.

## Optional additions

Depending on plan compatibility and site survey, optional work can include
automatic pump control, cloud access, AI analytics, source-tank monitoring,
extra tank/sensor nodes, wireless range extension, custom reports, dealer
branding, and installation services. These are not automatically included in
every base plan.

The Municipal Water Kit is Rs. 7,999 one-time. It includes two motorized
valves, one municipal-water availability sensor, and controller integration.
Installation, plumbing modifications, and non-standard valve sizes are charged
separately according to the actual work.

Every plan price is an equipment or service-package price. Installation and
plumbing are additional and depend on wiring, pipe size, valve size, pump and
starter panel, access, civil work, and installation location.

Overhead-tank quantity and upper-MCU quantity are not always the same. When
overhead tanks are on the same surface, interconnected, and operate as one main
tank group, one upper MCU can monitor that group. Tanks on separate levels or
not interconnected require independently scoped upper sensor nodes. Source
tanks are counted separately and normally require their own source sensors.

## Information needed for a quote or site check

Customers should provide:

- Name and contact number.
- City and property type.
- Number and type of tanks.
- Approximate tank height and capacity when known.
- Pump location and pump rating.
- Whether pump automation or phone control is required.
- Whether access is needed only at the property or from anywhere.
- Location of power, pump panel, source tank, and overhead tank.
- Known wireless-signal or internet limitations.

The final installation design must be confirmed at the site. A quoted product
price may not include electrician work, contactor/panel changes, special
enclosures, long cable routes, range extension, civil work, or travel.

## Installation and electrical safety

- Mains power must be isolated before pump, relay, contactor, or controller
  wiring is touched.
- A qualified electrician must perform mains-voltage pump wiring.
- Use correctly rated protection, terminals, enclosure, strain relief, and
  earthing according to local requirements.
- Keep electronics away from rain, splashing, condensation, and direct sun.
- Keep sensor wiring separated from mains and motor cables.
- Mount an ultrasonic sensor perpendicular to the water surface and away from
  tank walls, inlet flow, ladders, float valves, and other obstructions.
- Do not bypass manual pump controls without customer and electrician approval.
- Verify manual OFF and automatic OFF before leaving the installation.

## Troubleshooting guidance

### Tank level looks incorrect

Check for sensor obstruction, incorrect mounting angle, condensation, movement,
wrong tank-height calibration, inlet water hitting the sensing area, or a stale
wireless reading. Do not change pump automation thresholds until the reading is
verified safely.

### Pump does not start

Possible reasons include a full destination tank, low or unavailable source
water, missing sensor data, an active fail-safe, manual mode, pending command,
offline controller, electrical protection trip, or a pump-panel problem. A
qualified person should inspect the status and physical panel; safety controls
must not be bypassed merely to force a start.

### Pump does not stop

Use the physical/manual OFF control first if safe to do so, isolate power when
required, and obtain electrical support. Check the level sensor, stop threshold,
relay/contactor feedback, wiring, and controller reachability before restoring
automatic operation.

### App cannot connect locally

Confirm that the controller is powered, the phone is using the intended local
connection, local permissions are allowed, and the correct device is selected.
Cloud login and local controller access are different connection modes.

### Remote information is stale

Check site internet, controller power, last-seen time, cloud status, and device
authentication. Local controller safety should continue independently, but
remote history and commands require connectivity.

## Support and service area

SaleWell publishes support through `support@salewell.co.in` and
`+91-8796452878`. The website currently describes service in Delhi NCR,
including Delhi, Noida, Gurugram/Gurgaon, Faridabad, and Ghaziabad. Customers
outside these areas should request an availability check.

For the fastest response, share the city, property type, tank count, pump
control requirement, and a brief description of the water-management problem.

## Chatbot boundaries

The chatbot provides product and general support information. It must not ask
for device API keys, administrator passwords, database credentials, payment
card details, or other secrets. It cannot confirm that mains wiring is safe,
diagnose a hazardous panel remotely, or replace a qualified installer or
electrician. Pricing and compatibility must be confirmed before purchase.
