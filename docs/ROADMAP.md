# Roadmap: seamless solar charging for the Tesla and the Rivian

This plan implements the [architecture decision](SOLAR_CHARGING_ARCHITECTURE.md). Each phase ends with an evidence gate. Do not skip a gate because software reports success: "accepted" and "physically changed" are separate results, and only TWC3 or independent meter readings prove the second.

The [Rivian charge adapter](RIVIAN_CHARGE_ADAPTER.md) is built and software-tested, but untested against the real car. It installs in dry run, so phases 0–2 use it only to read. Phase 3 runs the attended trial with it, and phase 4A wires it into evcc.

## Definition of done

"Seamless" means all of the following, measured over at least a week of normal use:

1. **Plug in and walk away.** Plug either car into the Wall Connector at home, and within the detection window evcc identifies it. Charging follows solar surplus without anyone touching the car app.
2. **Battery respected.** The house battery is respected through `prioritySoc`/`bufferSoc`. Grid import during solar charging stays within the configured tolerance.
3. **One-tap override.** The owner can override from Home Assistant: "now" for full power, "off" to stop, and departure-SoC plans.
4. **Unplug restores normal behavior.** The Tesla returns to its normal amperage. The Rivian's own schedule is restored exactly.
5. **Failures are visible and bounded.** Each documented failure mode is covered: stale meters, evcc down, cloud down, Bluetooth down.

## Phase 0 — Inventory and housekeeping (no control)

- [ ] Answer the [open questions](SOLAR_CHARGING_ARCHITECTURE.md#open-questions-for-the-owner): hardware appetite, Tesla model/year and parking distance, schedule count, plug adapter type, tariff, export limits.
- [ ] Upgrade the Rivian integration to upstream [`home-assistant-rivian` 2.0.0](https://github.com/bretterer/home-assistant-rivian/releases/tag/2.0.0) for telemetry. Do not install the combined prototype fork.
  - Record the plugged-in, location (device tracker) and charger-state entity IDs that the adapter will read.
  - Do not write the upstream schedule entities. They show only the first schedule, from a local cache that is re-fetched every 15 minutes, and they do not expose how many schedules exist.
- [ ] Install the [Rivian charge adapter](RIVIAN_CHARGE_ADAPTER.md#install-and-configure) and leave **dry run on** (the default). Map plugged in, location, charger state, the Wall Connector's vehicle-connected sensor, and the vehicle override `input_select.twc3_vehicle_override` from the glue package. The evcc liveness entity follows in phase 1.
  - Press **Read schedules**, a read that dry run allows. Record how many entries the car has, including inactive ones, and whether their weekdays overlap. Cross-check the count in the Rivian app.
  - Rivian has rejected a write of overlapping entries ([upstream PR #308](https://github.com/bretterer/home-assistant-rivian/pull/308)). An overlapping list is therefore a risk for the adapter's exact restore; settle it before phase 3.
- [ ] Fix TWC3 telemetry: DHCP reservation, Wi-Fi signal, and the HA `tesla_wall_connector` fetch timeouts. Confirm the vitals update at least every 30 s for 24 hours.
- [ ] **Split-phase check.** During a normal manual charge, compare the Wall Connector's per-leg current sensors with its vehicle-current sensor. Record whether evcc's `currentA + currentB` would double-count ([why](SOLAR_CHARGING_ARCHITECTURE.md#evcc-pitfalls-to-configure-around)).
- [ ] **Tesla path.**
  1. Place an ESPHome Bluetooth proxy (`active: true`) near the Tesla's parking spot.
  2. Add HA core **Teslemetry**, then **Add local vehicle** (the Bluetooth sub-entry). HA pairs an owner-role key; there is no charging-manager option. Tap a key card on the console reader when prompted.
  3. Leave `tesla_custom` installed but unused for control until phase 2 passes.
- [ ] Copy [`config/homeassistant/packages/ev_solar_glue.example.yaml`](../config/homeassistant/packages/ev_solar_glue.example.yaml) into HA. Replace placeholders, including the Tesla's normal charging amps (default 32 A). Confirm that the Tesla's `sensor.evcc_tesla_status` and the adapter's `sensor.r1s_solar_adapter_evcc_status` read `a`/`b`/`c` correctly:
  - each car away;
  - each car home but unplugged;
  - each car plugged in and idle;
  - each car charging;
  - each car plugged in with the override set to the other car (expect `a`).

**Gate:** entity map complete, schedule inventory recorded, TWC3 vitals fresh for 24 h, split-phase result recorded, both status sensors correct in every state.

## Phase 1 — evcc in observe-only mode

- [ ] Install the evcc add-on (0.318.0 at the time of writing) and the `ha-evcc` integration.
- [ ] Start from [`config/evcc/evcc.example.yaml`](../config/evcc/evcc.example.yaml). Configure meters, the `twc3` charger, the solar tariff, and both vehicles **without** control services. The loadpoint stays in mode `off`; the Rivian keeps `mode: now` until phase 4A.
- [ ] Set the adapter's **evcc liveness** option to an `ha-evcc` entity, such as the loadpoint mode select. Without it, an evcc outage cannot stop the adapter's lease renewals.
- [ ] In the evcc UI, set:
  - loadpoint phases **1**;
  - min current 6 A;
  - max current = the TWC3's configured limit;
  - `prioritySoc`, `bufferSoc`, `bufferStartSoc` (suggested start: 60 / 80 / 95);
  - residual power 150 W.
- [ ] Verify against HA history:
  - grid sign (export negative);
  - battery sign (evcc shows battery charging when the Sol-Ark charges);
  - PV;
  - TWC3 charge power versus the HA Wall Connector power.
- [ ] Plug in each car. Confirm evcc identifies the right vehicle within a cycle or two, and that it never identifies the other car while that car is away or plugged in elsewhere. evcc's "guest" event means detection gave up after 10 minutes.

**Gate:** three sunny days observed. Power flows and signs correct. Detection correct for both cars with no wrong-vehicle identification.

## Phase 2 — Tesla live solar charging

- [ ] Add `start_charging` (switch) and `setMaxCurrent` (number) to the Tesla vehicle. Set the loadpoint to `smart` while attended.
  - For unattended use, also change the loadpoint's `mode:` in the YAML to `smart`. evcc re-applies that default mode at every unplug ([`core/loadpoint.go#L742-L750`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/core/loadpoint.go#L742-L750)), so a mode set only in the UI lasts one session.
- [ ] Attended checks. Log the command time, the acknowledgement, the Tesla's reported amps, and the TWC3 current:
  - [ ] Bluetooth reachability at the parking spot. Confirm that commands route over Bluetooth: with Teslemetry debug logging, no "not reachable over Bluetooth, using cloud" lines appear. At least 95% of current commands must be confirmed by the TWC3 current. Confirm that streamed state stays fresh without waking the car. Teslemetry never reads state over Bluetooth.
  - [ ] Amp steps while charging: 16 → 10 → 6 A and back up. Record time to settle.
  - [ ] Stop, sleep, wake, start round trip. Confirm no unintended wakes over one idle hour.
  - [ ] Cloud fallback: power off the Bluetooth proxy mid-session. Commands must still land once, with no duplicate start/stop.
  - [ ] Internet down: with Teslemetry loaded, disconnect the internet briefly and change the current. Record whether it still lands over Bluetooth, or whether the entities went unavailable.
  - [ ] Amp restore: set the normal amps in the glue package's `input_number.tesla_normal_charge_amps` (default 32). Unplug at home: the Tesla's charge-current number returns to that value. A later manual start must not begin at 6 A.
- [ ] Enable the glue watchdogs: evcc mode `off` on stale site meters, and stop on evcc unavailable. The watchdog's Rivian step acts only while the adapter's **Solar charging** switch is on.
- [ ] Retire `tesla_custom` control (remove it or disable its entities) so there is a single writer.

**Gate:** one week of unattended Tesla solar sessions. evcc logs reviewed. Grid import within tolerance. No stuck low amperage after sessions.

## Phase 3 — Rivian attended control trial

Run the trial with the adapter itself, following the step-by-step table in the [adapter guide](RIVIAN_CHARGE_ADAPTER.md#attended-trial-roadmap-phase-3). Every step is journaled and restorable, and the adapter snapshots the complete schedule list before its first write. The trial therefore no longer depends on the car having exactly one schedule. Keep evcc's Rivian vehicle **without** `start_charging`/`setMaxCurrent` during the trial, so the operator is the only writer. The [standalone harness brief](RIVIAN_SOLAR_CHARGING_RESEARCH_AND_HARNESS_BRIEF.md#standalone-cli-implementation-brief) remains the fallback if the adapter path is blocked.

The safety rules of the earlier [modulation trial](../reference/ha-prototype-docs/RIVIAN_SCHEDULE_MODULATION_TRIAL.md) still apply:

- attended, with the owner able to stop charging from the app;
- competing writers paused, including schedule edits in the Rivian app;
- fresh TWC3 readings;
- the exact original schedule recorded before any write (the inventory plus the adapter's snapshot);
- Home Assistant's time zone set to the vehicle's;
- adapter dry run **off** for the trial.

Judge readbacks by the adapter's **Read schedules** or the Rivian app. Upstream schedule entities are not readbacks: after a write they show their own cached value until the next 15-minute refresh.

| Test | What it establishes | Pass condition |
| --- | --- | --- |
| Inventory | The starting list | Count and fields recorded, including inactive entries and any weekday overlap |
| T1 Write and readback | Rivian accepts the lease, the parked window and the restore, with charging blocked by the charge limit | `control_state` goes `lease_active` → `paused` → `handed_back` with no fault. The Rivian app shows each change at Home Assistant's local time. **Read schedules** shows the original list. |
| T2 Amps mid-session | Amperage changes apply mid-session | TWC3 current follows each step. Record latency (target under 2 min). |
| T2b Continuity | Renewals and current changes do not interrupt a session | The TWC3 contactor stays closed, and current never drops to 0, through two renewals and two current changes |
| T3 Window off "now" | A parked window stops a session | Charging stops (TWC3 contactor opens or current reaches 0). Record latency. |
| T4 Lease expiry | A lapsing lease stops a session with no write | Charging stops at the window end. The adapter then parks the lapsed window. |
| T5 Window back onto "now" | A new lease restarts a session | Charging resumes. Record latency. |
| T6 Plug in while parked | The car waits at plug-in while the window is parked | Car waits; record how the app's "charge now" interacts with the schedule |
| T7 Exact restore | The owner's list comes back exactly | **Read schedules** matches the inventory, and the Rivian app agrees |
| T8 Midnight (optional) | A lease survives local midnight | Charging continues past midnight until the lease ends |

Record results with the evidence-note template in the [brief](RIVIAN_SOLAR_CHARGING_RESEARCH_AND_HARNESS_BRIEF.md#deliverables-and-acceptance), including vehicle software version. Do not record identifiers.

**Decision:**

| Result | Next step |
| --- | --- |
| T1–T5 and T7 pass with acceptable latency | Phase 4A: wire the adapter into evcc |
| T2 passes, T3 or T4 fails | Phase 4B (current-only, needs adapter work) or phase 5 (hardware) |
| T1 or T2 fails | Phase 5 (hardware) |
| T7 fails (restore rejected or inexact) | Stop. Recreate the owner's schedule in the Rivian app, resolve the fault, and fix the restore before any further control. |

## Phase 4A — Rivian adapter under evcc control

The adapter is already built, so this phase adds no code. It wires the adapter into evcc once the trial passes.

- [ ] Confirm the adapter state: `control_state` idle with no fault, dry run off, **Lease renewal** on, and the evcc liveness option set (phase 1).
- [ ] Change evcc's Rivian vehicle ([`config/evcc/evcc.example.yaml`](../config/evcc/evcc.example.yaml)):
  - Remove `mode: now`. It only made evcc ignore the missing current control.
  - Add `start_charging: switch.r1s_solar_adapter_solar_charging` and `setMaxCurrent: number.r1s_solar_adapter_solar_current`. Copy the real IDs from the adapter's device page; they follow the config entry's title.
  - Keep `minCurrent: 8`. The adapter never applies less than 8 A, and a lower evcc minimum makes evcc re-send the current every cycle.
- [ ] If the Rivian normally charges overnight on its own schedule, give it an evcc repeating plan. The owner's schedules are suspended while the adapter owns the schedule.
- [ ] Supervise the first sessions. In each one, `control_state` follows evcc's requests and the TWC3 current follows the lease. After unplug, **Read schedules** matches the phase 3 inventory.
- [ ] Then run unattended.
- [ ] Optional, later: propose full-list `get_charging_schedules`/`set_charging_schedules` services to `home-assistant-rivian`, which has none in 2.0.0. If upstream accepts them, the adapter could drop its own Rivian login.

**Gate:** one week of Rivian solar sessions with every restore verified, and no schedule conflicts left unresolved.

## Phase 4B — Rivian current-only mode (if stop/start fails)

- Keep the owner's schedule as the start/stop authority: for example, a daytime window on solar days.
- Within it, modulate amperage only. **The built adapter cannot do this yet.** It always replaces the owner's schedule with its own lease or parked window. Current-only mode needs an adapter change: rewrite only the amperage of the owner's schedule, under the same snapshot, guard and restore rules.
- In evcc, give the Rivian "always charge" so evcc never needs to stop it, and accept minimum-current (8 A, about 1.9 kW at 240 V) grid or battery draw when solar falls short.
- evcc still sends a start command each cycle while the car is plugged in but not charging. Without a `start_charging` entity, the TWC3 driver answers "vehicle not capable of start/stop" ([`core/loadpoint.go#L1058-L1071`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/core/loadpoint.go#L1058-L1071), [`charger/twc3.go#L121-L124`](https://github.com/evcc-io/evcc/blob/bf5c276cdb395443fda4601b99151c0a962d0db4/charger/twc3.go#L121-L124)). Either map `start_charging` to a permissive switch that records evcc's request but does not act, such as an HA template switch, or accept recurring enable errors while the car waits for its own window.
- Document this residual clearly, or proceed to phase 5.

## Phase 5 — Hardware path

- [ ] Choose the EVSE. Reference choice: OpenEVSE Advanced 48 A NACS. Have an electrician swap it for the TWC3 on the existing circuit.
- [ ] Hand the Rivian's schedule back: press **Restore owner schedule**, then turn the adapter's dry run back on. Its evcc status sensor keeps serving vehicle detection.
- [ ] evcc: replace the `twc3` charger with `openevse`. Remove the control services from both vehicles; keep them for SoC, status, and plans only. The EVSE is now the single writer.
- [ ] Attended checks with each car:
  - pause and resume;
  - 6/8/16 A steps;
  - EVSE behavior when evcc or Wi-Fi is lost;
  - the Rivian through its NACS-to-J1772 AC adapter.

**Gate:** one week of unattended sessions per car.

## Phase 6 — Unattended operation and polish

- [ ] Dashboard: `ha-evcc` mode, plan, and power entities, plus the evcc ingress UI link.
- [ ] Notifications: identification failures ("guest"), watchdog trips, Tesla Bluetooth fallback usage. The adapter already posts a notification for each fault.
- [ ] Departure-SoC plans per vehicle. Add a time-of-use tariff, if one applies, so plans pick cheap hours.
- [ ] Optional and experimental: the evcc optimizer's automatic mode `full`, for forecast-aware "wait for tomorrow's sun" decisions. It needs a sponsor token, the experimental flag, and a configured vehicle capacity. Verify what it does on this site, because its docs still describe it as advisory.
- [ ] Re-run the phase 2/3 spot checks after vehicle, charger, evcc, or integration updates that touch charging.
