# Rivian and Wall Connector control feasibility

Research date: September 20, 2026. This is a read-only feasibility assessment,
not commissioning evidence. No charging commands, schedule writes, pairing,
firmware changes, or Home Assistant configuration changes were performed.

## Conclusion

There are credible methods worth testing, but no verified plug-and-play control
path for this exact vehicle and charger. Investigate Rivian schedule control
first, without buying hardware. Keep automatic operation in shadow mode until
physical behavior and failure recovery have been demonstrated.

The current implementation's paired-stop requirement is a conservative design
boundary, not proof that schedule-only charging is impossible. Supporting a
schedule-only mode would require an explicit architecture change and a clearly
disclosed failure policy; this research does not remove the existing gate.

## Live installation evidence

Read through the configured native Home Assistant MCP bridge:

| Component | Observed |
| --- | --- |
| Rivian | Model-year 2025 R1S; vehicle software 2026.31.0 |
| Rivian HA integration | Installed source declares 1.5.3b0; integration loaded; options empty |
| Wall Connector | Tesla Wall Connector; firmware 26.26.10+g42855ec9b9cd1 |
| Available control entities | Neither device has a number, switch, select, or button control entity |

The vehicle model year was decoded privately from registry data; no VIN,
credentials, location coordinates, or device identifiers are recorded here.
Rivian identifies 2025+ R1 models as Gen 2 in its
[key-fob compatibility information](https://gearshop.rivian.com/products/key-fob).

Installed Rivian source reads the control selection from integration options and
only assigns a phone identity when enrollment is found. Empty options explain
why controls are not selected, but enabling that option is not a Gen 2 solution:
[upstream documents unsupported Gen 2 secure pairing](https://github.com/bretterer/home-assistant-rivian#remote-vehicle-control).
Supported vehicles pair over Bluetooth once and then send remote commands through
the cloud. This is not a missing dashboard button that can safely be exposed.

## Methods found in other projects

### 1. Rivian schedules: strongest software-only candidate

[rchav/rivian_solar_charge_ha](https://github.com/rchav/rivian_solar_charge_ha/blob/main/custom_components/rivian_solar_charging/rivian_client.py)
and [ostap-korkuna/rivian-charging-automation](https://github.com/ostap-korkuna/rivian-charging-automation/blob/master/charging_automation/RivianAPI.py)
use authenticated charging-schedule queries and mutations. They adjust schedule
amperage rather than relying on the paired START/STOP command path. Their control
policies use 8–48 A in 2 A increments; these are not independently verified limits
for this installation. Source snapshots inspected: rchav `0c4e6ec`, ostap
`c3cd9e1`.

Their pause technique moves charging outside the current time window. It is not
a zero-amp command, and disabling a schedule can permit immediate charging. A
future window eventually arrives if the controller stops updating it. Schedule
replacement also risks overwriting the owner's settings.

Reusable ideas: authenticated schedule access and modulation. Retain our
snapshot, ownership, conflict detection, and exact-restore protections. Neither
project establishes physical Gen 2 pause/resume on this vehicle and firmware.
An API success response or schedule readback alone is insufficient.

### 2. Wall Connector meter emulation: real hardware research path

[PVi1/esphome-twc-control](https://github.com/PVi1/esphome-twc-control) uses an
ESP32/RS485 adapter to emulate a Neurio meter and influence the charger's local
load-management behavior. It consumes HA measurements and targets solar
balancing. This avoids depending on Rivian's paired cloud commands, but requires
additional hardware and qualified installation around mains equipment.

[Tesla LoadPilot](https://github.com/zany92/tesla-loadpilot) builds on that method
with an HA integration and diagnostics. Its reported validation is a French
three-phase installation on firmware 26.18 and 26.26.1. Its README explicitly
labels single-phase support theoretical; it does not establish compatibility
with this US installation on 26.26.10.

Its [measured behavior notes](https://github.com/zany92/tesla-loadpilot/blob/main/docs/en/BEHAVIOR.md)
describe episodes where the charger distrusted the emulated measurements and
ignored control attempts. This is evidence from that installation, not a claim
that every charger behaves identically. Do not copy its calibrated constants or
treat an optimistic switch state as proof of physical stop.

The older [Klangen82 configuration](https://github.com/Klangen82/tesla-wall-connector-control)
explicitly warns that its method fails during active charging on firmware 26.2+.
Newer projects report improvements, but their results do not certify 26.26.10.

Tesla's [North American application note](https://digitalassets.tesla.com/tesla-contents/image/upload/NA_Wall_Connector_Smart_Current_Limits_App_Note_EN.pdf)
specifies a maximum 6 A fallback when the meter connection is lost, not a complete
stop. At 240 V that permits approximately 1.44 kW (calculated). Official dynamic
power management protects panel capacity; it is not itself a solar-surplus policy.
Do not buy or deploy an emulator on the assumption that loss of communications
guarantees zero grid charging.

### 3. Existing charger software does not remove the missing actuator

The standard [HA Wall Connector integration](https://www.home-assistant.io/integrations/tesla_wall_connector/)
provides telemetry. The [EVCC TWC3 adapter](https://github.com/evcc-io/evcc/blob/master/charger/twc3.go)
delegates both enable/disable and current adjustment to the selected vehicle; it
does not unlock a writable Wall Connector API. Merely switching to EVCC therefore
does not resolve this Rivian's control limitation.

A charger with documented local pause/resume, current limits, feedback, and
appropriate offline behavior would be the cleaner architectural alternative.
No replacement product has been selected or installation certified in this work.

## Proposed commissioning gates

Each live-write stage requires owner approval and an attended test window.
Use native HA services through MCP; do not extract credentials or bypass MCP to
call the vehicle directly. The installed integration may need a separately
approved update to expose the required schedule operations.

| Gate | Required evidence | What it does not prove |
| --- | --- | --- |
| Read-only capability check | Read schedules through the integration; validate schema, time zone, location scope, and ownership snapshot | Ability to write or affect charging |
| Unplugged round trip | With approval, write a bounded setting, read back, restore the exact original snapshot, and verify restoration | Physical current control or stopping |
| Attended charging test | At home, measure actual response to minimum current, a small upward step, downward step, pause, and resume | Unattended reliability |
| Failure campaign | Verify stale telemetry, command rejection, lost network, HA restart, manual schedule edits, and future-window rollover behavior | Electrical certification or indefinite compatibility |
| Operational acceptance | Record measured command latency, observation cadence, overshoot energy, repeatability, restore behavior, and supported firmware | A guarantee after an untested firmware/API change |

Before an active test, choose explicit abort limits compatible with the installed
circuit and vehicle, ensure the owner can stop charging normally, and disable
competing automations. Abort on unexpected current, missing fresh measurements,
ambiguous vehicle identity, or failed restoration. Never substitute a generic
smart plug, improvised relay, or repeated breaker switching for an EVSE control.

For any eventual schedule-only mode, show that distinction in setup and the card.
Do not advertise fail-closed or zero-grid behavior if an accepted schedule can
continue charging after controller loss. If that residual behavior is unacceptable,
require an independently verified stop-capable actuator.

## Separate telemetry work

Unchanged HA state timestamps are not sufficient evidence that a device is
offline. A future freshness improvement should use a real successful provider
poll and measurements from that response. Do not manufacture a heartbeat or
simply relax the stale-data guard. This is a software evidence issue separate
from proving physical control.
