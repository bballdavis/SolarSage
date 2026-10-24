# Verified entity contracts and remaining commissioning gates

Read-only native Home Assistant MCP inspection on September 19, 2026 informed
this implementation. This was not a deployment or physical charging test.
Entity names below are examples of the inspected providers, not hard-coded
bindings or permission to control those devices.

## Measurement and control are separate

| Source | Observed shape | Controller use |
| --- | --- | --- |
| Sol-Ark MQTT grid power | Signed W | Positive import, negative export; confirm CT placement covers the charger |
| Sol-Ark battery power | Signed W | Positive charge, negative discharge; corroborated against changing SOC in hourly history |
| Sol-Ark battery SOC | Percent | Reserve guard; unchanged values still need a trustworthy freshness signal |
| Tesla Wall Connector total power | kW | Multiply by 1,000 for watt-based control and physical feedback |
| Wall voltage | V | Power/current conversion; not a substitute heartbeat for stale wall power |
| Wall vehicle current | A | Diagnostic measurement, not a writable current limit |
| Wall connection/contactor | Binary sensor | Connection/contact feedback, not an actuator |
| Rivian connection/location/SOC/limit | Binary, tracker, numeric sensors | Vehicle identity and charge-limit checks |
| Rivian start/stop | **No switch present in inspected HA** | Missing capability; schedule access does not prove physical start/stop |
| Tesla vehicle charging/current | Switch and number | Separate vehicle command path; still requires commissioning |

The controller already normalizes W/kW. PV output is useful context but does not
alone establish surplus: grid flow, battery flow and existing EV draw determine
the available budget. Home-battery capacity is an explicit user setting, never
inferred from vehicle battery capacity.

Hourly grid history showed daytime export means around 3.2-4.5 kW followed by
nighttime import. This corroborates that a measured-export policy can apply here;
it is not a claim that a zero-export/curtailed-PV controller has been implemented.

## Solcast

Use aggregate sensors, not a sum of their aggregate and per-rooftop attributes:

| Setting | Inspected entity | Native unit |
| --- | --- | --- |
| Remaining today | `sensor.solcast_pv_forecast_forecast_remaining_today` | kWh |
| Next hour | `sensor.solcast_pv_forecast_forecast_next_hour` | Wh |
| Source updated | `sensor.solcast_pv_forecast_api_last_polled` | Timestamp state |

Both energy sensors expose aggregate `estimate`, `estimate10`, and `estimate90`
attributes in their own native unit. Select one explicitly when desired;
missing selected attributes are not silently replaced with a different estimate.
The confidence multiplier is an additional planning derating factor, not a
statistical confidence interval. P10 plus a derating factor is deliberately
more conservative than using the central estimate alone.

The daily sensor also contains `detailedForecast`: half-hour `period_start`
entries with `pv_estimate` in **kW**, despite its parent state's kWh unit. This
controller uses the provider's aggregate energy sensors instead; it does not
mistake interval power for energy or double-count intervals.

The installed Solcast schedule showed five daily refreshes about 2h26m apart.
Source-age checking uses the actual successful-poll timestamp, separately from
derived entity freshness. Hourly sensor recalculation cannot make an old source
forecast fresh. A six-hour source-age default accommodates that observed cadence;
configure a stricter limit if appropriate. Nighttime zero is valid forecast data,
not an unavailable marker. Protect mode remains conservative on stale data.

Forecasting gates the start budget after household energy and battery recovery
are reserved. It does not command amps or predict individual passing clouds.
Measured power still drives downward response, bounded cloud ride-through,
upward smoothing, current ramps, hard limits, and restart cooldown.

Provider reference: [Solcast integration](https://github.com/BJReplay/ha-solcast-solar).

### Initial setup for the inspected installation

Keep the solar feature disabled while mapping, and leave shadow mode on. In
Forecast guidance, select the three aggregate entities above. Start with advisory
policy, `estimate10` for a conservative estimate, and a six-hour maximum source
age. The additional forecast fraction is separate derating; using both P10 and
the default 0.75 fraction deliberately reserves more headroom.

Enter the actual usable house-battery capacity, desired battery target and
remaining household energy reserve before using protect policy; those values
cannot be inferred safely from the observed sensors. Watch the normalized budget
and input-age diagnostics. Do not enable real Rivian charging until the physical
control and freshness issues below have been resolved and commissioned.

## What still prevents unattended use

1. The inspected R1S has no charging switch. Establish a supported start/stop
   path before enabling real Rivian control. The upstream project's
   [remote-control limitations](https://github.com/bretterer/home-assistant-rivian)
   are relevant; a schedule round trip cannot bypass them.
2. Several unchanged sensors had old `last_reported` values: wall power/current
   were hours old while voltage updated, and home SOC was tens of minutes old
   while battery power updated. The current conservative limits (wall power 75s,
   grid/battery power and voltage 180s, vehicle/house SOC inputs 300s) block this
   snapshot. Old timestamps do not prove the device is offline, but they also
   cannot prove a recent measurement. Input diagnostics distinguish this case.
3. A provider-native refresh/heartbeat contract must be verified before changing
   this behavior. Never stamp retained MQTT values as fresh or borrow an unrelated
   sensor timestamp. Do not simply increase timeouts to make commissioning pass.
4. Confirm grid CT placement, hardware generation/pairing support, then supervise
   physical stop, start/current changes, missing inputs, and restart recovery.

Live screenshot capture was unavailable because the HA MCP screenshot beta is
disabled. Savant's live compact card configuration and local source were inspected;
new UI rendering and interactions are verified in a local simulated preview.
