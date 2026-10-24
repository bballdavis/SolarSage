# HASS Solar Rivian

HASS Solar Rivian is a Home Assistant custom integration that adds conservative
"charge on sunshine" control to the established
[Rivian (Unofficial)](https://github.com/bretterer/home-assistant-rivian)
integration. It controls a Tesla through its Home Assistant current and charger
entities and controls Rivian current through short, renewable charging schedules,
with a separate commissioned physical start/stop switch. The controller is designed for a shared Tesla Wall Connector and a home
battery/solar system.

This project is an Apache-2.0 fork of `bretterer/home-assistant-rivian`. It keeps
the original vehicle telemetry and controls, then adds the solar controller in
the same `rivian` integration so one HACS-compatible install contains the whole
feature.

> [!CAUTION]
> This project uses an unofficial Rivian cloud API. It is not affiliated with
> Rivian or Tesla. Keep shadow mode enabled until you have reviewed the entity
> mapping and observed at least one full day of decisions. Gen 2 Rivian schedule
> writes must pass the built-in unplugged compatibility probe. That probe only
> verifies the schedule API. Live charging additionally requires a supported
> physical stop control, a home identity, and a successful physical stop test.
> Upstream does not currently support paired remote control on Gen 2 vehicles.

The initial 1.6.0 release was a prototype. The subsequent hardening work and
hardware limitations are documented in [the control contract](docs/CONTROL_DESIGN.md).
The 1.7.0b2 source remains a commissioning beta, not a certification of live control.
Read the [live entity findings](docs/ENTITY_CONTRACT.md) before commissioning;
the inspected installation does not currently expose a Rivian charging switch.

## What it does

- Calculates true EV-available power from grid flow, house-battery flow, the
  EV's present load, and a configurable safety margin.
- Starts only after stable surplus and ramps up by at most 2 A per minute.
- Smooths increases, reduces current promptly, and bounds cloud deficits by time,
  energy, and hard import/discharge limits. A minimum-off delay prevents flapping.
- Reads optional existing solar forecast entities to reserve house-battery and
  household energy before permitting new sessions.
- Preserves a house-battery reserve with separate entry and exit thresholds.
- Stops immediately for stale inputs, ambiguous vehicle identity, charge-limit
  completion, or configured blocker helpers.
- Uses Tesla's Home Assistant current and charger switch entities.
- Uses a short Rivian schedule lease without changing the vehicle charge limit.
- Snapshots and restores the complete Rivian schedule list, including fields the
  controller does not understand.
- Detects external schedule edits by hash and refuses to overwrite them.
- Persists transaction journals so Home Assistant can recover an interrupted
  Rivian probe or charging lease after restart.
- Provides shadow mode, Charge now, Resume automatic, health, compatibility,
  state, target-current, available-power, and diagnostic entities.
- Provides a Solar charging sidebar page and [Savant-styled dashboard card](docs/CARD.md)
  with guided native setup, physical feedback, forecast advice, cloud response,
  input age/unit diagnostics, capability gaps, and recovery controls.

## Control model

The configured signs are important:

- Grid power is negative while exporting and positive while importing.
- House-battery power is negative while discharging and positive while charging.
- Wall Connector power is positive consumption in watts.

Every 10 seconds the controller evaluates the newest measurements:

```text
discharge_w = max(0, -battery_power_w)
available_w = ev_power_w - grid_power_w - discharge_w - safety_margin_w
target_a    = floor(available_w / wall_voltage_v)
```

The target is clamped to 8–48 A. Rivian targets use 2 A steps; Tesla targets use
1 A steps. An inactive session may start only when all inputs are valid, export
is non-negative, available power supports at least 8 A, house-battery state of
charge is at least `reserve + entry margin`, and the condition has held for the
configured start time. An active session stops immediately at the reserve floor
or for a hard blocker. Power deficits use the configured stop hold to reject
short transients. The Wall Connector integration itself normally polls every 30
seconds, so this is not a promise of a 10-second physical response. Increasing
power is smoothed; decreasing measured power immediately bounds the target.

## Safety boundaries

The implementation treats Rivian schedules as a transaction:

1. Read and persist the complete current list and its SHA-256 hash.
2. Write a 10-minute schedule lease for the current weekday and current home
   location, guarded by the previously read hash.
3. Persist both pending and confirmed lease hashes before and after the write.
4. Renew the lease every five minutes while control remains active.
5. On exit, unload, disable, restart, or manual resume, command physical OFF and
   verify fresh switch and wall-power feedback before restoring a schedule.
6. Restore the exact snapshot only when the current hash is one the controller
   owns. If another actor changed the schedule, stop and expose a fault instead
   of overwriting that edit.
7. Reassert OFF after restoration and verify charging stayed stopped before
   clearing ownership. Failed cleanup remains journaled and visible.

The compatibility probe uses the same journaled process while the Rivian is
unplugged. It makes one reversible amperage change, reads it back, restores the
original list, and requires an exact restoration hash before marking the API
compatible. It does not prove physical stopping or Gen 2 remote-control support.

For both vehicles, local ownership and incomplete cleanup are persisted, but the
vehicle does not hold a server-enforced safety lease. The controller stops its
owned charger switch during a normal shutdown or reload, but
a total Home Assistant host failure cannot send that cleanup command. Vehicle
charge limits remain the final backstop for both cars.

## Requirements

- Home Assistant 2025.1 or newer.
- A Rivian account and vehicle supported by the upstream integration.
- Grid power, house-battery power, and house-battery SOC entities.
- A Wall Connector connected/power/voltage entity set.
- For Tesla control: connected, SOC, charge-limit, charging-current, and
  charger-switch entities.
- For live Rivian control: connected, home, SOC, charge-limit, and an actual
  supported start/stop switch, plus physical commissioning.
- For live Tesla control: a home entity and physical commissioning in addition
  to its current and switch controls.
- Home Assistant latitude and longitude set to the charging location.

The controller does not require the charger itself to expose a writable current
limit. Tesla is controlled through the vehicle integration; Rivian is controlled
through its cloud schedule API plus a supported stop control. The standard Tesla
Wall Connector integration is measurement-only; it is not a stop control.
All mapped vehicle controls, SOC/limit, connection, and home entities must belong
to the same HA device. Generic EVSE switch adapters are not implemented.
Use single/split-phase power measurements with the correct line-to-line voltage;
three-phase charging is not supported by this controller's power calculation.

## Installation

### HACS-compatible repository

The repository contains exactly one integration directory, as required by HACS.
When the project is available from a GitHub mirror or release, add it as a HACS
custom integration repository, install **HASS Solar Rivian**, and restart Home
Assistant. HACS itself does not manage Forgejo repositories directly.

### Manual installation from Forgejo

1. Download or clone this repository.
2. Back up any existing `custom_components/rivian` directory.
3. Copy `custom_components/rivian` from this repository to
   `<home-assistant-config>/custom_components/rivian`.
4. Restart Home Assistant.
5. Add or reload **Rivian (Unofficial)** in Settings → Devices & services.

This is a fork of the upstream Rivian integration, so it replaces that custom
component rather than installing beside it. Existing Rivian config entries are
retained by Home Assistant.

Before upgrading from the prototype, stop charging in the vehicle app, unplug,
and disable the old solar controller so it can restore its schedule. Version 1.6
did not store a physical stop binding. If an unfinished old journal is found,
this version preserves it and blocks with `cleanup_binding_missing`; it does not
guess a control switch or discard the original schedules. Keep that journal and
resolve the missing binding/schedule recovery before attempting live use.

## Configuration

Open the Rivian integration's **Configure** dialog. Its menu separates the original
Rivian settings, solar setup, vehicle control, forecasts, and cloud-response tuning.
The **Solar charging** sidebar page appears after the controller is configured.

1. Leave **Enable solar-surplus EV charging** off initially and save once after
   upgrading.
2. Open Configure again, choose **Rivian only**, **Tesla only**, or **Rivian and
   Tesla**, then map the base entities and the entities for the enabled vehicles:

   | Setting | Required behavior |
   | --- | --- |
   | Grid power | W; negative export, positive import |
   | House battery power | W; negative discharge, positive charge |
   | House battery SOC | Percent |
   | Wall connected | On only while a vehicle is physically connected |
   | Wall total power | Present EV load in W |
   | Wall voltage | Present line voltage in V |
   | Tesla connected | Tesla modes only; Tesla-specific binary sensor |
   | Tesla SOC / limit | Tesla modes only; comparable percent values |
   | Tesla charging amps | Tesla modes only; writable `number` entity |
   | Tesla charger switch | Tesla modes only; writable start/stop `switch` |
   | Rivian connected | Rivian modes only; Rivian-specific binary sensor |
   | Rivian SOC / limit | Rivian modes only; comparable percent values |

3. Map the home entity and actual charge switch for each vehicle. Select any
   blocker helpers you use, such as storm-watch or battery-grid-charge modes.
   There are no guessed helper defaults. A selected unavailable blocker prevents
   charging. Configure forecast sensors and policy separately if desired.

4. Keep **Shadow mode** on, enable solar charging, and save.
5. Observe the controller state, available-power, target-current, and block-reason
   entities for at least one representative day. Shadow mode prevents new vehicle
   commands. Recovery may still stop a previously owned real charging session.
6. Pause automatic charging. After checking signs, voltage, identity and limits,
   turn off shadow mode for explicit commissioning. While a vehicle is actively
   charging at home, run **Test physical stop control**. It stops the charge and
   does not restart it. An already-idle vehicle cannot pass this test.
7. With the Rivian and Wall Connector unplugged, run **Test Rivian schedule
   control** and verify the Rivian app still shows the original schedules. This
   is a separate write/read/restore test, not a substitute for physical control.
8. Enable automatic charging only after capability and feedback checks pass,
   then observe the first live solar session. Missing Gen 2 remote-control
   capability remains a blocker; a schedule probe cannot override it.

Disable any other automation that writes the same Tesla current/switch or Rivian
schedule while solar control is enabled. External Rivian edits are detected and
preserved, but conflicting controllers will cause faults rather than useful
charging behavior.

The prototype's automatic fallback to ordinary Tesla scheduled charging has been
removed: it could immediately undo a solar safety stop. Existing stored schedule
settings are retained but are not actuated. Pause solar control before using an
independent overnight schedule; do not run competing controllers concurrently.

## Default tuning

| Option | Default |
| --- | ---: |
| Shadow mode | On |
| Vehicle mode | Rivian and Tesla |
| House-battery reserve | 25% |
| Entry margin above reserve | 5% |
| Power safety margin | 500 W |
| Stable surplus before start | 120 s |
| Sustained deficit before stop | 30 s |
| Maximum transient battery discharge | 250 W |
| Maximum transient grid import | 500 W |
| Maximum solar current | 48 A |
| Upward smoothing | 60 s |
| Minimum pause before restart | 180 s |
| Transient deficit energy budget | 10 Wh |
| Immediate-stop grid import | 1,500 W |
| Immediate-stop battery discharge | 1,000 W |
| Physical feedback timeout | 90 s |
| Forecast policy | Off |
| Forecast confidence | 75% |
| Forecast minimum session | 15 min |

## Controller entities

| Entity | Purpose |
| --- | --- |
| Automatic solar charging switch | Master enable; disabling releases owned control |
| Charge now button | Requests the configured maximum current; physical, reserve, blocker and hard-power guards still apply |
| Resume automatic button | Ends manual mode and resumes solar eligibility checks |
| Test Rivian schedule control button | Runs the unplugged transactional probe |
| Controller state sensor | Normal, waiting, solar active, manual, blocked, or fault |
| Active vehicle sensor | Tesla, Rivian, or empty when ambiguous/disconnected |
| Target current sensor | Current target in amperes |
| Available solar power sensor | Calculated controllable power in watts |
| Block reason sensor | Current eligibility, blocker, or fault reason |
| Last command sensor | Last successful control intent |
| Controller healthy binary sensor | Off when the coordinator has a hard fault |
| Rivian compatible binary sensor | On only after the probe succeeds |

The sidebar page groups these entities and clearly separates requested current,
wall power, actuator acknowledgement, forecast energy, restart delay, and faults.
It also exposes **Test physical stop control**, **Stop and release control**, and
**Clear recovered fault**. Clearing a fault does not bypass pending cleanup.

## Rivian schedule services

The fork exposes two response-only services for advanced diagnostics and for the
solar controller:

- `rivian.get_charging_schedules` takes `vehicle_id` and returns the complete
  schedule list plus a canonical hash.
- `rivian.set_charging_schedules` takes `vehicle_id`, the complete `schedules`
  list, an optional `expected_hash`, and an optional `reason`. It performs a fresh
  compare, writes, reads back, validates caller-controlled fields, and returns
  the verified list and full hash.

Do not call the set service casually: it replaces the complete list. Use the hash
guard and preserve the full response from the get service.

## Known limitations

- Rivian cloud endpoints are unofficial and can change without notice.
- The schedule hash guard is optimistic inside Home Assistant. Rivian does not
  expose a server-side conditional revision, so an external edit in the narrow
  read/write gap can still race. Readback verification and the conflict latch
  limit this risk but cannot eliminate it.
- Gen 2 schedule mutation is not assumed to work; the controller requires a live
  compatibility probe on the actual vehicle.
- A Rivian lease can recur for ten minutes on the same weekday if Home Assistant
  never returns to restore the original list. The persisted journal restores it
  automatically when Home Assistant starts again.
- A Tesla command cannot be transactionally rolled back after a total host or
  network failure.
- The controller assumes single-phase current from the configured voltage and
  does not perform phase switching.
- It supports one shared charging point and acts only when exactly one configured
  vehicle identity is connected.
- Installation and unit tests do not prove live vehicle behavior. Validate in
  shadow mode, with the unplugged probe, and with the separate physical stop
  commissioning test before enabling automatic charging.

## Development and verification

```bash
uv run --with ruff ruff check custom_components tests
uv run --with pytest pytest -q
python -m compileall -q custom_components tests
```

The dependency-free unit suite covers schedule normalization/hashing, server
metadata handling, surplus entry/exit holds, current ramping, reserve behavior,
blockers, stale inputs, and ambiguous vehicle identity.

The HA-facing suite additionally checks native service/state/storage behavior,
ignored OFF commands, journal recovery, settings and panel registration. CI runs
the suite with HA 2025.1.4/Python 3.13 and HA 2026.9.3/Python 3.14. Browser checks
use `tools/preview-panel.html`, whose actions are mocked. Neither this preview nor
the HA tests are a live installation or vehicle test.

The repository keeps the original project as the `upstream` Git remote. To review
future upstream changes, fetch that remote and merge or rebase deliberately; do
not overwrite the solar transaction and controller changes without re-running the
tests and the live compatibility probe.

## License and attribution

Licensed under Apache License 2.0. This fork is based on
`bretterer/home-assistant-rivian`; see [LICENSE.md](LICENSE.md) and the Git history
for attribution. Thanks to the upstream maintainers and contributors for the
Rivian integration and API groundwork.
