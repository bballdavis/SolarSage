# Roadmap: seamless solar charging for the Tesla and the Rivian

This plan implements the [architecture decision](SOLAR_CHARGING_ARCHITECTURE.md). Each phase ends with an evidence gate. Do not skip a gate because software reports success: "accepted" and "physically changed" are separate results, and only TWC3 or independent meter readings prove the second.

## Definition of done

"Seamless" means all of the following, measured over at least a week of normal use:

1. **Plug in and walk away.** Plug either car into the Wall Connector at home, and within the detection window evcc identifies it. Charging follows solar surplus without anyone touching the car app.
2. **Battery respected.** The house battery is respected through `prioritySoc`/`bufferSoc`. Grid import during solar charging stays within the configured tolerance.
3. **One-tap override.** The owner can override from Home Assistant: "now" for full power, "off" to stop, and departure-SoC plans.
4. **Unplug restores normal behavior.** The Tesla returns to its normal amperage. The Rivian's own schedule is restored exactly.
5. **Failures are visible and bounded.** Each documented failure mode is covered: stale meters, evcc down, cloud down, Bluetooth down.

## Phase 0 — Inventory and housekeeping (no control)

- [ ] Answer the [open questions](SOLAR_CHARGING_ARCHITECTURE.md#open-questions-for-the-owner): hardware appetite, Tesla model/year and parking distance, schedule count, adapter type, tariff, export limits.
- [ ] Upgrade the Rivian integration to upstream [`home-assistant-rivian` 2.0.0](https://github.com/bretterer/home-assistant-rivian/releases/tag/2.0.0). Do not install the combined prototype fork.
  - Record the new schedule entity IDs.
  - Read, do not write, the schedule entities.
  - Record how many schedules the car has. Upstream entities only manage the first.
- [ ] Fix TWC3 telemetry: DHCP reservation, Wi-Fi signal, and the HA `tesla_wall_connector` fetch timeouts. Confirm the vitals update at least every 30 s for 24 hours.
- [ ] **Split-phase check.** During a normal manual charge, compare the Wall Connector's per-leg current sensors with its vehicle-current sensor. Record whether evcc's `currentA + currentB` would double-count ([why](SOLAR_CHARGING_ARCHITECTURE.md#evcc-pitfalls-to-configure-around)).
- [ ] **Tesla path.**
  1. Place an ESPHome Bluetooth proxy (`active: true`) near the Tesla's parking spot.
  2. Add HA core **Teslemetry** and its Bluetooth vehicle sub-entry, with the charging-manager key role if offered.
  3. Leave `tesla_custom` installed but unused for control until phase 2 passes.
- [ ] Copy [`config/homeassistant/packages/ev_solar_glue.example.yaml`](../config/homeassistant/packages/ev_solar_glue.example.yaml) into HA. Replace placeholders and confirm both evcc status sensors read `a`/`b`/`c` correctly:
  - each car away;
  - each car home but unplugged;
  - each car plugged in and idle;
  - each car charging.

**Gate:** entity map complete, TWC3 vitals fresh for 24 h, split-phase result recorded, both status sensors correct in every state.

## Phase 1 — evcc in observe-only mode

- [ ] Install the evcc add-on and the `ha-evcc` integration.
- [ ] Start from [`config/evcc/evcc.example.yaml`](../config/evcc/evcc.example.yaml). Configure meters, the `twc3` charger, the solar tariff, and both vehicles **without** control services. The loadpoint stays in mode `off`.
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
- [ ] Plug in each car. Confirm evcc identifies the right vehicle, and that it never identifies the other car while that car is away or plugged in elsewhere.

**Gate:** three sunny days observed. Power flows and signs correct. Detection correct for both cars with no wrong-vehicle identification.

## Phase 2 — Tesla live solar charging

- [ ] Add `start_charging` (switch) and `setMaxCurrent` (number) to the Tesla vehicle. Set the loadpoint to `smart` while attended.
- [ ] Attended checks. Log the command time, the acknowledgement, the Tesla's reported amps, and the TWC3 current:
  - [ ] Bluetooth reachability at the parking spot: no-wake status read while asleep, and at least 95% command success.
  - [ ] Amp steps while charging: 16 → 10 → 6 A and back up. Record time to settle.
  - [ ] Stop, sleep, wake, start round trip. Confirm no unintended wakes over one idle hour.
  - [ ] Cloud fallback: power off the Bluetooth proxy mid-session. Commands must still land once, with no duplicate start/stop.
  - [ ] Session end restores the normal amperage. A later manual start must not begin at 6 A.
- [ ] Enable the glue watchdog: stop and release on stale site meters or evcc unavailable.
- [ ] Retire `tesla_custom` control (remove it or disable its entities) so there is a single writer.

**Gate:** one week of unattended Tesla solar sessions. evcc logs reviewed. Grid import within tolerance. No stuck low amperage after sessions.

## Phase 3 — Rivian attended control trial

This replaces the earlier plan to build a standalone CLI first, **if** the car has exactly one charging schedule. In that case upstream 2.x entities can perform every step from HA Developer Tools. With several schedules, use the [standalone harness brief](RIVIAN_SOLAR_CHARGING_RESEARCH_AND_HARNESS_BRIEF.md#standalone-cli-implementation-brief) instead. The safety rules of the earlier [modulation trial](../reference/ha-prototype-docs/RIVIAN_SCHEDULE_MODULATION_TRIAL.md) still apply:

- attended, with the owner able to stop charging from the app;
- competing writers paused;
- fresh TWC3 readings;
- the exact original schedule recorded before any write.

| Test | Setup | Pass condition |
| --- | --- | --- |
| T1 Write and readback | Unplugged. Change amperage 48 → 40 → 48 A. | Each write accepted, readback equal, the Rivian app shows the same, no other schedule lost |
| T2 Amps mid-session | Plugged in, charging inside the window. Step 16 → 10 → 16 A. | TWC3 current follows each step. Record latency (target under 2 min). |
| T3 Window off "now" | While charging, move the window to exclude the present time. | Charging stops (TWC3 contactor opens or current reaches 0). Record latency. |
| T4 Lease expiry | Window ending about 10 min ahead, no further writes. | Charging stops at window end without any write |
| T5 Window back onto "now" | After T3/T4, move the window to cover now. | Charging resumes. Record latency. |
| T6 Plug in outside the window | Plug in with the window elsewhere. Then test the app's "charge now". | Car waits; record how "charge now" interacts with later writes |
| T7 Exact restore | Write back the recorded original schedule. | Two fresh readbacks equal the original |

Record results with the evidence-note template in the [brief](RIVIAN_SOLAR_CHARGING_RESEARCH_AND_HARNESS_BRIEF.md#deliverables-and-acceptance), including vehicle software version. Do not record identifiers.

**Decision:**

| Result | Next step |
| --- | --- |
| T1–T5 and T7 pass with acceptable latency | Phase 4A: build the adapter |
| T2 passes, T3 or T4 fails | Phase 4B (current-only) or phase 5 (hardware) |
| T1 or T2 fails | Phase 5 (hardware) |

## Phase 4A — Rivian charge adapter

- [ ] **Upstream first.** Propose to `home-assistant-rivian`:
  - full-list `get_charging_schedules` / `set_charging_schedules` services (the prototype fork already had hash-guarded versions);
  - a fix for single-schedule writes dropping other schedules, if T1 showed that happens.

  The adapter then needs only public HA services: no second Rivian login, no private `hass.data`. Interim fallback: drive the upstream entities, valid only for a car with exactly one schedule.
- [ ] Build `custom_components/rivian_solar_adapter` in this repository to the [adapter contract](SOLAR_CHARGING_ARCHITECTURE.md#rivian-gen-2-r1s).
  - Port the prototype's schedule normalization and hashing, and its persisted ownership journal (snapshot, owned hash, pending write, restore-only-if-owned).
  - Use a short lease (15 min, renewed every 5 min), an even-amp deadband, a minimum write interval, and restart recovery.
- [ ] Fixture tests:
  - lease write and renewal;
  - deadband and rate limit;
  - restore;
  - conflict refusal after an external edit;
  - timeout treated as ambiguous (re-read before retrying);
  - restart mid-lease;
  - unplug and away release.
- [ ] Add `start_charging: switch.rivian_solar_charging` and `setMaxCurrent: number.rivian_solar_current` to the evcc Rivian vehicle. Supervise the first sessions, then run unattended.

**Gate:** one week of Rivian solar sessions with every restore verified, and no schedule conflicts left unresolved.

## Phase 4B — Rivian current-only mode (if stop/start fails)

- Keep the owner's schedule as the start/stop authority: for example, a daytime window on solar days.
- Within it, the adapter modulates amperage only.
- In evcc, give the Rivian "always charge" so evcc never needs to stop it, and accept minimum-current (8 A, about 1.9 kW) grid or battery draw when solar falls short.
- Document this residual clearly, or proceed to phase 5.

## Phase 5 — Hardware path

- [ ] Choose the EVSE. Reference choice: OpenEVSE Advanced 48 A NACS. Have an electrician swap it for the TWC3 on the existing circuit.
- [ ] evcc: replace the `twc3` charger with `openevse`. Remove the control services from both vehicles; keep them for SoC, status, and plans only. The EVSE is now the single writer.
- [ ] Attended checks with each car:
  - pause and resume;
  - 6/8/16 A steps;
  - EVSE behavior when evcc or Wi-Fi is lost;
  - the Rivian through its NACS-to-J1772 AC adapter.

**Gate:** one week of unattended sessions per car.

## Phase 6 — Unattended operation and polish

- [ ] Dashboard: `ha-evcc` mode, plan, and power entities, plus the evcc ingress UI link.
- [ ] Notifications: identification failures ("guest"), watchdog trips, adapter faults, Tesla Bluetooth fallback usage.
- [ ] Departure-SoC plans per vehicle. Add a time-of-use tariff, if one applies, so plans pick cheap hours.
- [ ] Optional: the evcc sponsor optimizer for forecast-aware "wait for tomorrow's sun" decisions.
- [ ] Re-run the phase 2/3 spot checks after vehicle, charger, evcc, or integration updates that touch charging.
