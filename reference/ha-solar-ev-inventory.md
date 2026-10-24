# Solar EV charging discovery

Read-only native MCP verification, 2026-09-19. No charging commands, configuration
changes, dashboard writes, pairing, or deployment were performed.

- Existing `.env` MCP URL works over streamable HTTP; server reported
  `ha-mcp 8.4.3.dev2666`. Consume the JSON-RPC response matching the request ID:
  long reads can emit SSE progress notifications before the result.
- Savant reference: `dashboard-test`, Loads view,
  `custom:savant-energy-breaker-board-card`, compact density, name sort, 12h graph.
  Native screenshot beta remains disabled; configuration read succeeded.
- Sol-Ark inputs: `sensor.sol_ark_inverter_grid_power` and
  `sensor.sol_ark_inverter_grid_power_ct` (W),
  `sensor.sol_ark_inverter_pv_power` (W),
  `sensor.sol_ark_inverter_battery_power` (W),
  `sensor.sol_ark_inverter_battery_state_of_charge` (%). Battery sign was corroborated
  by hourly history: positive power accompanied rising SOC, negative falling SOC.
  Grid history also showed several daytime hourly means near -3.2 to -4.5 kW,
  followed by positive nighttime import. Grid meter placement still needs
  commissioning confirmation.
- Wall measurements: `sensor.tesla_wall_connector_total_power` is **kW**,
  `sensor.tesla_wall_connector_grid_voltage` is V,
  `sensor.garage_tesla_wall_connector_vehicle_current` is A;
  `binary_sensor.tesla_wall_connector_vehicle_connected` and
  `binary_sensor.tesla_wall_connector_contactor_closed` are connection/contact feedback.
  No wall charging-control entity was found.
- R1S telemetry: `binary_sensor.r1s_charger_connection`,
  `device_tracker.r1s_location`, `sensor.r1s_battery_state_of_charge`,
  `sensor.r1s_battery_state_of_charge_limit`. No switch or select entity was found
  in either search or the device's entity inventory. A supported physical start/stop
  command path is therefore unproven, not implicitly available from schedule API access.
- Tesla vehicle controls exist: `switch.roxxo_charger`,
  `number.roxxo_charging_amps`, `number.roxxo_charge_limit`; this is not wall control.
- Solcast aggregate forecasts: `sensor.solcast_pv_forecast_forecast_remaining_today`
  is **kWh**, `sensor.solcast_pv_forecast_forecast_next_hour` is **Wh**. Both expose
  aggregate `estimate`, `estimate10`, `estimate90` attributes in their native unit.
  Do not add per-array attributes to the aggregate again.
- `sensor.solcast_pv_forecast_api_last_polled` has a timezone-aware timestamp state
  and next-update scheduling attributes. Five daily polls were scheduled about
  2h26m apart. Derived hourly sensor reports do not establish forecast-source freshness.
  Daily `detailedForecast` has half-hour `period_start` entries and kW `pv_estimate`
  values, not per-row kWh despite the parent sensor's unit.
- Unchanged wall power/current and battery SOC had old `last_reported` timestamps
  despite fresh wall voltage/inverter power. Strict state-age checks in the local
  HASS-Solar-Rivian controller would block. Do not extend limits or reuse a sibling
  timestamp as proof without establishing source-level measurement freshness.

Implementation checkout: `Documents/Codex/2026-09-19/i-d-like-you-to-do/HASS-Solar-Rivian`.
Savant-Card is a read-only visual reference for that work.

## Sol-Ark battery capacity estimate — verified 2026-09-23

Live inspection and configuration used `homeassistant-ai/ha-mcp` 8.5.0.dev2706.

- Inputs: `sensor.sol_ark_inverter_battery_power` (W, power measurement) and
  `sensor.sol_ark_inverter_battery_state_of_charge` (%, battery, integer 0–100).
  Historical behavior confirms positive power with rising SOC and negative power
  with falling SOC. The power entity is an inverter-reported battery boundary;
  its metadata does not prove this equals cell-side DC energy.
- Derived power sensors split charging and discharging at ±50 W, with a separate
  `sensor.sol_ark_battery_flow_direction`. Freshness is required for power (120 s)
  and SOC (900 s). The charge and discharge energy sensors integrate to kWh with
  left Riemann integration and a one-minute maximum sub-interval.
- `automation.sol_ark_battery_capacity_run_tracker` tracks one direction at a time.
  It records a run only at a clean idle endpoint after idle has remained continuous
  for two minutes, and only when the SOC span is at least 10 percentage points and
  the energy delta is positive. Direct reversals restart the baseline without
  accepting the prior run. Missing/unavailable inputs, stale inputs, ambiguous
  direction, and Home Assistant restarts mid-run are rejected.
- The main entity is `sensor.sol_ark_battery_capacity_estimate` in
  `packages/sol_ark_battery_capacity_estimate.yaml`. It reports kWh with
  `device_class: energy_storage` and `state_class: measurement`. Attributes expose
  sample counts by direction, directional means, relative disagreement, quality,
  and a two-sided 95% Student-t sampling interval (null below two samples). The
  point is provisional until five samples include both directions. Directional
  disagreement above 20% is a diagnostic heuristic; the interval excludes
  systematic error.
- Supporting entities include `sensor.sol_ark_battery_charge_power`,
  `sensor.sol_ark_battery_discharge_power`, `sensor.sol_ark_battery_charge_energy`,
  `sensor.sol_ark_battery_discharge_energy`,
  `sensor.sol_ark_battery_capacity_charge_sample`, and
  `sensor.sol_ark_battery_capacity_discharge_sample`, plus separate seven-day
  mean, standard-deviation, and count Statistics helpers for each direction.
- A prior entity `sensor.sol_ark_battery_real_capacity` existed before this work
  and was left untouched. The new estimate entity has a distinct ID.

### Recorder history and backfill

- On 2026-09-23, the aligned five-minute power/SOC statistics contained 3,129 bins
  from 2026-09-12 09:15 UTC through the interval ending at 2026-09-23 06:00 UTC.
  Screening for same-direction power, a clean idle endpoint, valid SOC, and at least
  a 10-point SOC span produced ten discharge candidates between 30.556 and
  32.533 kWh. No charge candidate met the gates.
- Those ten capacity points were imported through `recorder/import_statistics`
  into the main entity and verified with `recorder/statistics_during_period` and
  `recorder/get_statistics_metadata`. The import API requires hour-aligned starts;
  each run is plotted at the start of the UTC hour containing its 01:25/01:35 UTC
  completion. Metadata readback is `source=recorder`, `unit_class=energy`,
  `mean_type=1`, `has_sum=false`, and kWh. A disposable ID was imported, read back,
  and cleared first; its metadata and rows were both confirmed absent afterward.
- Older hourly power/SOC statistics span 2025-08-19 through 2026-09-23, but their
  aggregations hide direction changes. They produced 51 widely varying candidates
  (roughly 30.6–49.2 kWh), so none were imported.
- The latest five-minute candidate (31.176617 kWh, completed 2026-09-23 01:35 UTC)
  seeds the rolling discharge sample helper. The live entity currently reads
  31.177 kWh with one discharge sample, no interval, and `quality_status=provisional`.
  The other nine imported points populate its Recorder trend; they are not counted
  as live Statistics-helper samples.
- Historical Recorder state does not preserve a reliable `last_reported` age for
  an unchanged numeric SOC value. The backfill rejects explicit unavailable/missing
  periods and mixed-direction intervals, but it cannot prove that an unchanged
  numeric SOC was fresh at every historical instant. Live runs use current
  freshness checks, and historical values should be treated as estimates at the
  inverter-reported boundary rather than laboratory capacity measurements.

## Solar EV test inventory — read-only recheck, 2026-09-25

- The configured MCP endpoint responded as `ha-mcp 8.5.0.dev2732`. The direct HA
  tools were not mounted in this task; the project's existing protocol probe and
  read-only calls reached the configured MCP endpoint. No vehicle or charger
  service call was made.
- Loaded integration domains were `rivian`, `tesla_custom`, and
  `tesla_wall_connector`. The Wall Connector's 20 registry entities remained
  sensors/binary sensors, with no writable charge or current entity. The Tesla
  vehicle still exposed a charge switch and 0–48 A current number. The Rivian
  device still lacked a physical charge switch.
- `ha_list_services(domain="rivian")` returned zero. The proposed integration's
  `get_charging_schedules` / `set_charging_schedules` services are therefore not
  deployed on this HA instance; a live Gen 2 schedule readback was not obtained.
- Read-only entity-registry checks put the Tesla switch, amp number, connection,
  SOC, charge limit, and location tracker on the same `tesla_custom` device. The
  analogous Rivian connection, SOC, limit, and tracker share one `rivian` device.
  This validates binding ownership only; it does not refresh their old states.
- At about 21:56 CDT, grid import was +1,440 W, PV 13 W, and battery power +40 W,
  each reported within seconds. Home battery SOC was 52% but last reported at
  20:25. Wall power and current remained 0.0 kW / 0.0 A with 11:53 report times;
  wall voltage later reported 241.4 V at 21:59. A fresh voltage state cannot
  establish fresh zero charging power. Rivian connected/charging states last
  reported on September 21. Those report ages block current commissioning.
- A sanitized subset is retained in the integration checkout at
  `tests/fixtures/exploratory/observed_ha_snapshot.json`. It contains no VIN,
  account identifier, coordinates, person name, or token. See
  `docs/EXPLORATORY_TEST_PLAN.md` there for the staged test gates.
- The pinned local HA Core `tesla_wall_connector` source polls vitals every 30 s
  and writes sensor state on each successful update, including unchanged zero
  values. If deployed behavior matches that source, the old power/current
  `last_reported` values are not explained by normal unchanged-value handling.
  Diagnose the deployed polling/entity path before relaxing the 75 s wall-power
  freshness gate or attempting charger commissioning.
