# Rivian charge adapter

**What it is.** The Rivian charge adapter (`custom_components/rivian_solar_adapter`) is the one control component this project owns in the [architecture decision](SOLAR_CHARGING_ARCHITECTURE.md). It gives evcc a start/stop switch and a current setting for a Gen 2 Rivian, which has no usable remote charging command. It does this by writing the vehicle's **charging schedule**.

**Status: in development, software-tested only.** The schedule and ownership core is tested against a simulated Rivian provider. The Home Assistant layer this guide describes is being built. Nothing has been run against a real account, vehicle, or Wall Connector. **It installs in dry-run mode, and nothing is written until you turn dry run off.**

## How it works

| evcc asks for | The adapter writes | Expected vehicle behavior (to be confirmed by the [trial](#attended-trial-roadmap-phase-3)) |
| --- | --- | --- |
| Charging on, at N A | A **lease**: one schedule window that started a few minutes ago and ends `lease_minutes` from now, at N A rounded down to an even 8–48 A. It is renewed before it ends. | Charges at N A |
| A different current | The same lease at the new amperage. Decreases go out at the next allowed write; increases wait for the increase interval. | Changes current mid-session |
| Charging off | A **parked window** that ended a few minutes ago, so no window covers now | Waits |
| Nothing (adapter or HA stopped) | Nothing; the lease lapses on its own | Stops at lease end |

**Ownership rules:**

1. **Snapshot first.** The first command while the car is plugged in at home takes ownership. Before writing anything, the adapter reads the owner's **complete** schedule list and persists it with a hash.
2. **Guard every write.** Every write is preceded by a fresh read. If the provider no longer holds what the adapter last verified, the adapter **faults and writes nothing**. That happens after an edit in the Rivian app or by another integration.
3. **Readback decides.** A write counts only when a fresh read shows it, including after timeouts. A write that lands late is recognized. Repeated non-application faults.
4. **Exact restore.** When the car is unplugged or away for `release_delay_s`, or on the **Release** button or service, the owner's original list is written back exactly. This also happens when dry run is turned on while owning. The restore is subject to the same guard.
5. **Restarts resume.** The journal survives restarts. An interrupted write is resolved by reading the provider.

**While the adapter owns the schedule, the owner's own schedules are suspended.** If the Rivian normally charges overnight on its own schedule, give it an evcc **repeating plan** (target SoC by a time) instead. Otherwise it charges only when evcc asks.

## Install and configure

1. Install upstream [`home-assistant-rivian`](https://github.com/bretterer/home-assistant-rivian) 2.x for telemetry, if you have not already.
2. Install this repository through HACS (custom repository, category integration), or copy `custom_components/rivian_solar_adapter` into `/config/custom_components/`. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → Rivian Solar Charge Adapter.**
   - **Login:** Rivian email and password, plus the one-time code if your account uses one. Only session tokens are stored, never the password.
   - **Vehicle:** pick the R1S.
   - **Entities:** map them from the upstream Rivian integration and the Wall Connector:

     | Option | Map to | Required |
     | --- | --- | --- |
     | Plugged in | The Rivian charger connection binary sensor | Yes |
     | Location | The Rivian device tracker | Yes |
     | Charger state | The Rivian charger state sensor | No, but **recommended**: without it evcc's status can never be "charging" (`c`) |
     | Wall Connector vehicle connected | `binary_sensor.tesla_wall_connector_vehicle_connected` | No, but **recommended**: the status then reports `a` unless the Wall Connector also sees a vehicle |

4. Options (**Configure**):

   | Option | Default | Meaning |
   | --- | --- | --- |
   | Dry run | **on** | Compute and show the would-be schedule; never write |
   | Lease minutes | 20 | How long a charging window lasts without renewal: the fail-safe horizon |
   | Renew before (minutes) | 8 | Renew when this little time remains |
   | Clock skew (minutes) | 5 | Margin for a vehicle clock that differs from Home Assistant's |
   | Minimum write interval (s) | 30 | Spacing between any two schedule writes |
   | Increase interval (s) | 120 | Minimum spacing before raising the current |
   | Release delay (s) | 60 | How long unplugged or away must last before restoring the owner's schedule |

## Entities

The examples assume the config entry is titled `R1S`; your entity IDs follow your vehicle's name.

| Entity | Purpose | evcc key |
| --- | --- | --- |
| `switch.r1s_solar_adapter_solar_charging` | Charging requested | `start_charging` |
| `number.r1s_solar_adapter_solar_current` | Requested current, 8–48 A in 2 A steps | `setMaxCurrent` |
| `sensor.r1s_solar_adapter_evcc_status` | `a` / `b` / `c` for evcc detection | `status` |
| `sensor.r1s_solar_adapter_control_state` | `idle`, `waiting_for_vehicle`, `lease_active`, `lease_lapsed`, `paused`, `writing`, `fault`, `dry_run`. Its attributes show the lease, last write, hashes (prefixes only), errors, and the dry-run target. | — |
| `switch.r1s_solar_adapter_lease_renewal` | Turn off to let a lease lapse (trial T4). Turns back on at restart. | — |
| `button.r1s_solar_adapter_release` | Restore the owner's schedule now | — |
| `button.r1s_solar_adapter_clear_fault` | Resume after a fault, if the provider holds a state the adapter knows | — |
| `button.r1s_solar_adapter_read_schedules` | Read the provider's schedule list. Shows days, times, amps, and enabled flags; never coordinates. | — |

Services: `rivian_solar_adapter.release`, `.clear_fault`, `.abandon_ownership` (forget the journal without writing, after you resolved a conflict in the Rivian app), and `.read_schedules` (returns the list summary).

evcc vehicle (see [`config/evcc/evcc.example.yaml`](../config/evcc/evcc.example.yaml)):

```yaml
  - name: rivian
    type: template
    template: homeassistant
    uri: http://supervisor/core
    phases: 1
    minCurrent: 8
    soc: sensor.r1s_battery_state_of_charge
    status: sensor.r1s_solar_adapter_evcc_status
    start_charging: switch.r1s_solar_adapter_solar_charging
    setMaxCurrent: number.r1s_solar_adapter_solar_current
```

## Attended trial (roadmap phase 3)

The adapter is also the safest tool for the [attended Gen 2 trial](ROADMAP.md#phase-3--rivian-attended-control-trial). Every step is journaled and restorable, unlike editing schedule entities by hand. Keep evcc's Rivian vehicle **without** `start_charging`/`setMaxCurrent` during the trial, so that you are the only writer.

**Prerequisites:**

- Owner present.
- Other schedule writers paused; don't edit in the Rivian app during the test.
- Wall Connector readings fresh.
- Dry run **off**.

**Recording.** Record time, action, and `control_state` attributes for each step. Also record the Wall Connector current/power and the Rivian app's view.

| Test | Steps with the adapter | Pass condition |
| --- | --- | --- |
| Inventory | Press **Read schedules**. | Note the count and fields. Exactly one schedule is the simplest case; any count is restored exactly. |
| T1 Write and readback (no charging possible) | The adapter only acts while the car is plugged in at home. First set the Rivian's charge limit at or below its current SoC so no session can start. Set current 16, turn **Solar charging** on, then off, then press **Release**. | `control_state` goes `lease_active` → `paused` → `idle` with no fault. The Rivian app shows each change. **Read schedules** shows the original list. Restore the charge limit afterwards. |
| T2 Amps mid-session | While charging under a lease: 16 → 10 → 16 A. | Wall Connector current follows. Record latency. |
| T3 Window off now | Turn **Solar charging** off. | Charging stops. Record latency. |
| T4 Lease expiry | Turn **Lease renewal** off, then **Solar charging** on, and wait `lease_minutes`. | `control_state` becomes `lease_lapsed`, and charging stops at about that time. |
| T5 Window back on now | Turn **Solar charging** on again (renewal on). | Charging resumes. Record latency. |
| T6 Plug in while parked | With the window parked, unplug and replug within the release delay, so the adapter keeps ownership. Then try "charge now" in the Rivian app. | Car waits after replugging. Record what "charge now" does, and whether it changes the schedule, which would make the adapter fault with `external_edit`. |
| T7 Exact restore | **Release** (or unplug for the release delay). | **Read schedules** matches the inventory. |

Then follow the [roadmap decision table](ROADMAP.md#phase-3--rivian-attended-control-trial).

## Faults and recovery

| Fault code | Meaning | What to do |
| --- | --- | --- |
| `external_edit` | The schedule changed outside the adapter; nothing was overwritten | Check the Rivian app. Once the owner's schedule is back, **Clear fault**. To accept the current schedule as the owner's, call `abandon_ownership`. |
| `conflict_after_write` | A readback matched neither the write nor the previous state | Same as above |
| `write_not_applied` | Rivian accepted no change after repeated writes | Check the vehicle and account, then **Clear fault** |
| `unsupported_schedule` | The provider returned a schedule shape the adapter does not understand | Nothing was written. Report the redacted shape from **Read schedules**. |

While faulted, the adapter writes nothing. Any lease lapses on its own. Rate limits pause writes for five minutes. An expired login starts Home Assistant's re-authentication flow.

## Known limits

- **Vehicle behavior is unproven.** No Gen 2 vehicle has been observed following these writes. The trial exists to establish that.
- **Owner schedules are suspended.** While owned, the owner's schedules are replaced by a single lease or parked window.
- **Coordinate tolerance.** Restores are exact for the six schedule fields the provider returns. Coordinates are compared to about 1 m.
- **Lease lapse is the only cloud-free stop.** A cloud or network outage cannot stop a lease that is already active; it lapses on its own. The trial (T4) must show that a lapsing window stops a session.
