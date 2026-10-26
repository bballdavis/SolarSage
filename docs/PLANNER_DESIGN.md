# Solar EV charging planner: design

October 25, 2026. This is the design that [PLAN.md](PLAN.md) phase 2 asks for: how the planner decides, its settings, how it uses Home Assistant, what it cannot do, and how it compares with an evcc-like surplus follower in simulation.

- **The code** is the pure-Python package [`custom_components/solar_ev_planner/core/`](../custom_components/solar_ev_planner/core/): standard library only, no Home Assistant imports. Its unit tests are in [`tests/planner/core/`](../tests/planner/core/). The real-data tools load the same folder as the package `planner_core`, which is the name used below.
- **The Home Assistant integration** around it (entities, settings, commands) is described in [PLANNER.md](PLANNER.md), and testing it with real data in [TESTING.md](TESTING.md).
- **The simulation results** below come from a design run whose harness and comparison tools are not part of this repository ([section 13](#13-where-things-are)).
- **The free hours changed after the simulation.** The simulated planner charged the car in a 10 PM to 6 AM night window, and the simulated site held its battery in free hours and wanted 40% when they began. The planner now charges the car at its full current from the moment the free hours begin. In those hours the owner's Sol-Ark refills the battery to 100% from the grid on its own settings, so the planner models the refill and adds a fourth power, Spread (section 2). Full-current free hours, Spread, the refill and the 30% default are covered by unit tests, not by the simulation; the shadow week and `tools/replay.py --whatif` on real days are their check.

## 1. The decision

Three designs were built and compared against four baselines on 30 simulated scenarios (3 weather and load draws each), plus 19 scenarios re-drawn with seeds no design was tuned on. All runs use the owner's tariff: free 8 PM to 6 AM, $0.32/kWh otherwise, $0.00 buyback, and the night charging window used then, 10 PM to 6 AM (see the note above).

| Design | What it is | Owner score, all 72 rows (lower is better) |
| --- | --- | ---: |
| evcc-like follower (b0) | follows the measured surplus every 5 min; free hours at full current | +2123 |
| MPC | a dynamic-programming plan over 30-min stages with a home/away scenario tree | −1799 |
| trajectory | a battery requirement curve plus a "taut string" current shape | −1639 |
| robust | one steady current from a 15-min battery forecast and three rules | −1849 |
| **final (recommended)** | the robust structure with the improvements below | **−1837** |

**Recommendation: the energy-budget planner (`final`)**. It keeps the robust design's structure and adds four things from the other designs and from this comparison:

- a multi-day Kalman bias on the Solcast forecast (from the MPC design);
- a within-day Bayesian weight calibrated against data;
- an honest, leave-one-out spread for house load;
- a wider spread when the forecast is stale.

It also gains the outputs the dashboard needs.

Why this one:

- **The designs are nearly equal wherever the owner's tariff applies.** On the 126 runs (453 simulated days) of free-nights scenarios without forced plans:
  - final and trajectory differ by 0.44 points (about $0.09) per 3-day run;
  - robust and MPC are 0.7 and 1.5 points behind trajectory.
  That is inside seed noise for almost every scenario. All of them put 5,300–5,700 kWh of solar into the cars, with almost no car-caused paid import (0.7–5 kWh in all).
- **Only plans separate them, and there the trajectory design fails.** On a daytime-only weekday plan ("80% by 17:00") the trajectory design scores 652, against 370 for final and 345 for robust. It pushes the plan in the last paid hours and leaves the home battery near empty into the evening (3,630 car-caused minutes below the floor).
- **The MPC is as good but heavier.** It takes 6.6 ms a step, against 2.0 ms for final including all dashboard outputs. Its decisions come out of a cost table that is hard to explain on a dashboard.
- **Final is the simplest to explain and to check.** One rule, `P = min(P_cap, max(P_noexport, P_fill, P_min))` (with free hours ahead, `P_spread` joins the max; section 2). Each decision's one-sentence reason names the limit that binds. The internal constants sit on a broad plateau: every variant tried moved the total by at most 2–3%.

## 2. How it decides, in plain words

### Daytime solar charging (paid hours, sun up)

Every 15 minutes, and at once on plug-in, on a new Solcast issue or when the mode changes, the planner does five things:

1. **Builds today's forecast as a range.** Each Solcast half hour is a bell curve on a log scale: the most likely value p50, and a spread taken from p10 and p90. Today's real production so far pulls the rest of today up or down. Early in the morning the forecast dominates; by early afternoon today's behaviour carries about two thirds of the weight.
2. **Predicts the house load** from the last 14 days, recency-weighted, with weekdays and weekends kept apart. Today's level (the last 24 hours against the profile) and any sustained jump in the last 2 hours (air conditioning, for example) are added on top.
3. **Predicts the home battery** in 15-minute steps from now until tomorrow's free hours, or tomorrow's sunset when there are no free hours. It uses the Sol-Ark's own rules: the sun serves the house first, the battery absorbs surplus and covers deficits, and at most 9 kW flows each way. In free hours the Sol-Ark's own settings charge the battery from the grid, so the prediction fills it at its full rate (the sun first, the grid for the rest) until it is full, and never discharges it. The prediction runs twice: once for the expected day, and once for a cautious day at the owner's caution level (default 90%), where solar is lower and house use higher.
4. **Works out four powers for the car:**
   - **Room (P_cap).** The highest steady power that keeps three things true on the cautious path all the way to tomorrow's free hours:
     - the battery never goes below the 30% floor (where the battery is already headed below the floor without the car, the car may not push it lower);
     - it still holds the wanted level when the free hours start (30% by default, the floor; the simulation used 40%);
     - the car causes no paid import.
     In this test the car holds its power for 2 hours and then takes only surplus, because the planner will have re-planned long before then.
   - **Spill (P_noexport).** The lowest steady power at which the expected day exports nothing while the car still wants energy: the solar the battery cannot store. Battery energy that the sun refills later counts here, so "drain now, refill later" comes out of the forecast.
   - **Plan (P_fill).** Only with a plan: the lowest steady power that reaches the plan's target by the end of the solar window on the cautious day.
   - **Spread (P_spread), only when free hours lie ahead.** The free hours fill the battery from the grid, so what it would still hold when they begin is worth nothing tomorrow. Spread is the highest steady power that, held through the whole solar window (not just 2 hours), keeps the floor and the wanted level on the cautious path. After the 2 hours the car is held only as far as the sun and the battery supply it without import, since the safety rule cuts paid import within minutes. So a car plugged in by day gets the battery's spare energy spread over the afternoon, and the cautious path reaches the wanted level when the free hours start. The dashboard's battery outlook then shows that held path.
5. **Sets the car's power** to `P = min(Room, max(Spill, Plan, Spread, the car's minimum current))`, then moves the real current toward it slowly:
   - changes under 5 A are ignored, and under 10 A for decreases that the battery rules do not require;
   - at most one change every 15 minutes, and at most 6 A per change;
   - no cut in the last 45 minutes before the car reaches its limit.

So **the floor is a limit on the predicted path, not a trigger.** If the cautious prediction stays above the floor through the evening, the night and tomorrow morning, the battery is allowed to drain now. Clouds then come out of the battery, and the car's current stays where it is.

### Free hours at full current

- **The car charges at its full current** from the moment the free hours begin (8 PM for now), up to its charge limit, then stops. Full current is the car's maximum, capped by the Wall Connector's setting (48 A). The dashboard shows, for example, *"Free hours: charging at 48 A to the 85% limit."*
- **The battery itself is never commanded.** The Sol-Ark's own settings charge it from the grid in free hours (PLAN.md decision 14). The planner's prediction fills it at its full charge power (9 kW by default) until it is full and never discharges it there, so the next paid hours start at 100%.

### Worked example

The example is from the summer simulation, Tuesday 16 June, a partly cloudy day. It ran with the earlier setup (a 10 PM night window, a battery that held its level in free hours, no Spread; see the top of this page), so the numbers are as recorded then. The Rivian (limit 85%) plugs in at 10:00 at 63%. The home battery is at 78%.

| Step | Numbers at 10:00 |
| --- | --- |
| Solcast for the rest of today (10:00–20:00) | p10 31.0, p50 37.5, p90 45.3 kWh. Produced so far 5.3 kWh, 3% above the forecast in force; only 10% of the day is seen, so the correction is small (×1.03). |
| House | The last 24 h ran at 0.86× the 14-day profile, so 18.0 kWh is expected to 20:00. |
| Caution 90% (z = 1.28) | z is split by each side's spread: solar 1.01, house 0.78. The cautious day has 34.0 kWh of solar and 21.2 kWh of house use to 20:00. |
| Room (P_cap) | 5.1 kW. The cautious battery path bottoms at 54% at 09:00 tomorrow and holds 68% at 20:00. |
| Spill (P_noexport) | 2.8 kW. |
| Minimum | 8 A = 1.9 kW. |
| Decision | `min(5.1, max(2.8, 1.9))` = 2.8 kW = 11.6 A. The Rivian takes even amps, so 10 A. |

The reason the dashboard shows: *"Solar charging at 10 A: on a 90%-confidence forecast a steady rate takes the solar the battery cannot store and the battery bottoms at 54% at 09:00 tomorrow (the battery rules would allow 5.1 kW)."*

The rest of the day:

- **12:15:** after a brighter late morning, Spill is 3.9 kW (16 A). That is 6 A above the current setting, past the deadband, so the current changes once, to 16 A.
- **17:30:** solar no longer covers the house, so the car pauses: *"No solar window now; the R1S waits for free hours."*
- **22:00:** the simulated night window starts and the car charges at 48 A to 85%. The planner now starts this at 20:00, when the free hours begin.

The day took 5 current changes. Solar put 18.9 kWh into the car, the battery 7.0 kWh (clouds the battery absorbed, refilled by the sun the same afternoon), and free hours 15.7 kWh. There was no paid import, and the battery never went below 59%.

The evcc-like follower on the same day made 43 changes, put 20.2 kWh of solar and 2.8 kWh of battery into the car, and let the battery fall to 42%.

## 3. Uncertainty (the Gaussian treatment)

**Solar.** Each half hour is modelled as `p50 · exp(b + e)`:

- `b` is the day's forecast bias, shared by every slot of the day. A day that runs 20% under the forecast at 10:00 tends to stay under it.
- `e` is the slot's own cloud noise.
- Solcast states the spread itself: `sigma = ln(p90/p10) / 2.563`.

The day bias is learned in two layers:

- **The prior** for today is `N(b0, v0)`:
  - `b0` is a slow multi-day bias, from a Kalman random walk over past days' log(actual / forecast in force). It needs the integration's `PvDay` store; without it, `b0` is 0.
  - `v0` blends Solcast's stated spread for today with the day-to-day variance of past days.
- **The evidence** is today so far: `y = ln(actual / forecast in force)`, with noise `0.08² / fraction of today's forecast energy already seen`. The conjugate update gives the weight `k` on today's evidence.

The noise constant was fitted to data. The table compares the model's `k` with the least-squares best weight for predicting the rest of the day, over 6,000 day-hours of simulated history:

| Hour | 9 | 11 | 13 | 15 | 16 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Best weight in hindsight | 0.40 | 0.44 | 0.73 | 0.63 | 0.64 |
| Model's k (noise 0.08) | 0.15 | 0.41 | 0.59 | 0.68 | 0.71 |
| Robust design's k (noise 0.15) | 0.05 | 0.18 | 0.32 | 0.41 | 0.43 |

So the planner stays cautious in the early morning and lets today dominate from about 11:00 on.

Around this update:

- The corrected rest of today is `p50 · exp(b)`, capped at p90.
- Each slot's spread shrinks as the bias becomes known.
- A forecast that has not been refreshed for more than 3 h (Solcast polls failing) gets 0.15 extra log-spread per 12 h on every future slot.

**House.** The 30-min profile and the 24-h and 2-h adjustments are described in section 2. Its spread is the recency-weighted root mean square of past days' cumulative error from this time of day until the check time. That error is computed leave-one-out (each day against a profile that excludes it), so it is honest out of sample.

**Caution.** The one-sided z of the caution level is split between solar and house in proportion to their spreads (`z_pv = z·sd_pv/sd_net`, `z_house = z·sd_house/sd_net`), so the net energy sits at the caution quantile. The labels are "near-certain" 0.97, "conservative" 0.90 (the default), "balanced" 0.75 and "typical day" 0.50.

**Measured coverage.** This is a known limit, measured by the design run's calibration check. In the simulation, the realised battery at the start of free hours was at or above the 90% path only **76%** of the time, and above the expected path 43% of the time. The gap has two causes:

- the house load is under-predicted on sunny days: the simulated air conditioning follows sunshine, which a 14-day average cannot see;
- the designed surprises: an AC jump and a heavy evening, both absent from history.

The closed loop compensates: re-planning every 15 minutes from the measured battery, plus the safety rules. Car-caused time below the floor on the owner's tariff was 1,420 minutes over 453 days, about 3 minutes a day. Section 11 gives the remedy.

## 4. Settings

These are the owner's settings, all on the dashboard. They map to `planner_core.Settings`; prices and free hours live in the `Tariff`, and each car's limit and plans in its `CarState`.

| Setting | Default | Meaning | Field |
| --- | --- | --- | --- |
| Planner | on | Off: nothing is sent to either car. The heartbeat then goes down, so by day Rivian schedule control parks the Rivian's charging window; in the free hours the night's one window runs on to their end ([PLANNER.md](PLANNER.md#planning-off-and-shadow-mode)). | `enabled` |
| Shadow mode | on | Plan and explain, send nothing (phase 3) | `shadow` |
| Free hours | 8 PM–6 AM daily | Schedule helper | `Tariff` window at $0 |
| Price outside free hours | $0.32/kWh | Normal import price | `Tariff.price` |
| Solar buyback | $0.00/kWh | Export price | `Tariff.buyback` |
| Daytime solar charging | on | Off: a car plugged in by day waits for the free hours | `daytime_solar` |
| Caution | conservative | near-certain / conservative / balanced / typical day | `caution` |
| Battery floor | 30% | The predicted battery stays above this | `floor_pct` |
| Battery level wanted when free hours start | 30% (the floor; 40% in the simulation) | At sunset when there are no free hours. With free hours ahead, the car may use the battery down to it (Spread); raise it to keep more in the battery | `wanted_at_free_start_pct` |
| Minimum time between changes | 15 min | | `min_change_min` |
| Largest single change | 6 A | | `max_step_a` |
| Paid grid tolerance | 5 min | Paid import accepted this long before the safety rule acts | `paid_tolerance_min` |
| Per car: charge limit | the car's own | Read from the car | `CarState.limit_pct` |
| Per car: plan | none | "X% by HH:MM", optionally on chosen weekdays | `RepeatingPlan`, `CarPlan` |

The internal constants are `planner_core.Params`, not owner settings. Each was varied over the whole suite.

| Constant | Value | Tested | Effect on the total |
| --- | --- | --- | --- |
| deadband | 5 A (10 A for decreases the battery rules do not require) | 3, 7 A (robust sweep) | within ±25 |
| commitment in the room test | 2 h | 0.5, 1 h | ±10; shorter means more changes and more floor time |
| evidence noise (PV) | 0.08 | 0.15 | ±5; 0.08 matches the best weight (section 3) |
| multi-day PV bias | on when `pv_days` are given | off | ±13, or ±30 counting one weekday-plan seed that flips (the simulation has no persistent bias) |
| house level prior / offset fade | 4 kWh / 3 h | 2 kWh; 6 h; 3-h window | within ±10 |
| run at minimum current without spill | yes | only with ≥50% / 100% spill | 14–21 worse, +0.5 changes a day |
| take measured export when full | no | yes | 20 worse, more floor time |

## 5. How tariffs change behaviour

Every plan is settings only: Schedule helpers painted with prices (`WeeklySchedule.from_ha`), a normal price and a buyback.

- **Free nights (today).** By day the car takes the spill and, because the battery refills overnight, the battery's spare energy spread over the afternoon (Spread). The wanted-level check is at 8 PM. From 8 PM the car charges at its full current while the Sol-Ark refills the battery from the grid, so the next paid day starts full. A car plugged in all day and night gets its solar and spare battery by day and the rest free at night. (The simulation ran this tariff with a 10 PM night window, a battery that held overnight and a 40% check.)
- **Time of use (no free hours).** Nothing is free, so there is no night mode and no refill. The wanted-level check is at sunset. By day the car takes the spill. Without a plan, a car plugged in at night waits for the sun. A plan's shortfall is charged at full current in the cheapest paid hours, as late as possible before the deadline. In paid hours the battery serves the car before the grid, so night plan charging drains the home battery first (section 11).
- **Flat.** The same as time of use with one price: solar by day, and paid power only when a plan needs it.
- **Free weekend.** Free daylight hours count as free hours. The car charges at its full current through them, and the prediction fills the battery as on a free night, the sun first.
- **Buyback above $0.** The decisions do not change. Spill still goes to the car, which is right while the car's energy is worth more than the buyback. The rule `P_noexport` would need a price comparison if buyback ever exceeds that value.

## 6. Plans

A plan is "X% by T", from the dashboard. `RepeatingPlan(80, "07:30", weekdays)` makes the concrete `CarPlan`s for the next few days. The sources are used in the owner's order:

1. **Sun and battery within the floor rule.** `P_fill` raises the steady power, up to Room, so the target is reached by the end of the solar window.
2. **Free hours.** The car charges at full current from the moment they begin, so each free hour before the deadline counts at the car's full power.
3. **Paid power, only for what 1 and 2 cannot cover.** The car charges at full current:
   - **now**, when the deadline falls inside today's solar window, so the sun can refill the battery before the evening;
   - otherwise in the cheapest paid hours, as late as possible.
   Once a push has started it continues until the deadline (hysteresis).

The dashboard outlook shows where each car's energy will come from, for example *"about 33 kWh from the sun, about 26 kWh in free hours"*. Every simulated plan was met: 72 of 72, including a 90%-by-07:00 Rivian that arrives at 10%.

## 7. Two cars

Only one car can be on the Wall Connector. The integration decides which car is plugged in: the charger sees a vehicle, and that car's own cable or connection sensor is on and it is at home. The planner then commands only that car; every other car's command is "send nothing". A new `plugged_since` starts a new session: limits and latches reset, and the first command is sent without rate limits.

For every car, the planner also reports an outlook and `tonight_car`: the car that should be on the charger in the coming free hours (the one whose plan needs them, else the one that needs the most energy).

## 8. Safety rules

These are checked on every call, and they override the plan:

| Situation | Action |
| --- | --- |
| Paid import above 0.25 kW while the car draws, for longer than the tolerance (5 min) | Cut the car by the import plus 1 A; no increase for 30 min |
| Battery at the floor and discharging into the car in paid hours | Cut the car by the discharge; no increase for 30 min |
| Battery at 97% or more, or charging at full rate, and exporting more than 0.4 kW for 10 min | An increase may skip the 15-min interval |
| Car unplugged, at its limit, or its data stale | Send nothing |
| Planner off | Send nothing to either car (by day Rivian schedule control then parks the Rivian's charging window; the free night's one window runs on to its end) |

Safety rules do not act within 10 minutes of a change, while a command is in flight, or while a plan is being pushed.

When the planner stops (Planning off, or it cannot plan), its heartbeat goes unavailable and, by day, Rivian schedule control parks the Rivian's charging window after 3 minutes. When Home Assistant itself is down, a daytime window lapses on its own within about 25 minutes. In the free hours the Rivian has one window to their end, written once, which runs on to that end either way. The Tesla keeps its last current up to its own limit in both cases.

## 9. Seasonal behaviour

Nothing seasonal is fixed:

- the solar window, the end of the surplus and the reserve come from the forecast on every run;
- house demand comes from the last 14 days.

The rows below are from the simulation, with its earlier setup (see the top of this page).

| Season | What the planner does |
| --- | --- |
| Summer | Long surplus. On the partly cloudy chart day the car takes 10–20 A by day, the battery still reaches 95% (the evcc-like follower's peaks at 65%) and holds overnight. |
| Winter (Dec 21, 5 kW peak, heating) | House use exceeds solar most of the day. The car gets no battery energy, night charging does the work, and no paid import comes from the car. All three designs score the same here; free-hours-only scores up to 7 points better only because its night started at 8 PM and theirs at 10 PM (section 11). |
| Air conditioning jump (+40%) | The 24-h level and the 2-h offset raise the house prediction on the day of the jump; the recency weights carry it from the next day. The trajectory design adapts a little faster: 15 car-caused floor minutes per week against 83 for final. |
| Weekday / weekend | Separate profiles. A home-on-weekends household gets a bigger weekend reserve. |

## 10. Home Assistant: entities and the integration contract

### Entities

Config-flow suggestions are in [`core/entities.py`](../custom_components/solar_ev_planner/core/entities.py), and every mapping stays configurable.

| Input | Entity | Unit |
| --- | --- | --- |
| Solar | `sensor.sol_ark_inverter_pv_power` | W |
| Grid | `sensor.sol_ark_inverter_grid_power` | W, negative = export |
| Battery power | `sensor.sol_ark_inverter_battery_power` | W, positive = charging |
| Battery level | `sensor.sol_ark_inverter_battery_state_of_charge` | % |
| Battery capacity | `sensor.sol_ark_battery_capacity_estimate` | kWh |
| Car power | `sensor.tesla_wall_connector_total_power` | kW |
| Car current (confirms a change) | `sensor.garage_tesla_wall_connector_vehicle_current` | A |
| Car on the charger | `binary_sensor.tesla_wall_connector_vehicle_connected` (+ contactor) | |
| Forecast | `sensor.solcast_pv_forecast_forecast_today` / `_tomorrow` (`detailedForecast`) | kW per half hour |
| Forecast age | `sensor.solcast_pv_forecast_api_last_polled` | time |
| Free hours | the Schedule helper (weekly config via `schedule.get_schedule`) | |
| Rivian | `sensor.r1s_battery_state_of_charge`, `sensor.r1s_battery_state_of_charge_limit`, `binary_sensor.r1s_charger_connection`, `device_tracker.r1s_location` | % |
| Rivian control | `switch.r1s_schedule_control_solar_charging`, `number.r1s_schedule_control_solar_current`; the standing request from `sensor.r1s_schedule_control_control_state` | A, even 8–48 |
| Tesla (Fleet, car "Roxxo") | `sensor.roxxo_battery_level`, `number.roxxo_charge_limit`, `binary_sensor.roxxo_charge_cable`, `device_tracker.roxxo_location` | % |
| Tesla control | `switch.roxxo_charge`, `number.roxxo_charge_current` (today in tesla_custom: `switch.roxxo_charger`, `number.roxxo_charging_amps`) | A |

House load is not measured. It is `pv + grid − battery − wall connector` (`house_load_kw`).

**Freshness.** An unchanged number keeps an old `last_reported` timestamp, so a value is stale only when it is unavailable, or when its integration has gone quiet. It is never stale just because of its own timestamp.

### The API the integration calls

All names are importable from `planner_core`.

```python
planner = Planner(settings: Settings | None = None, params: Params | None = None)
decision = planner.decide(snapshot: Snapshot) -> Decision      # every 1-5 min
planner.settings = Settings(...)                               # when the owner edits them
blob = planner.state_dict(); planner.load_state(blob)          # JSON-safe, persist in a Store

slots   = parse_detailed_forecast(today_attrs, tomorrow_attrs, tz=None, naive=True)
tariff  = Tariff(price=0.32, buyback=0.0,
                 windows=(PriceWindow(WeeklySchedule.from_ha(cfg), 0.0, "free"),))
samples = samples_from_statistics(pv_rows, grid_rows, battery_rows, wall_rows)  # W,W,W,kW
profile = build_load_profile(samples, today, days=14, half_life_days=5.0,
                             other_type_weight=0.1)
```

The integration fills one `Snapshot` per call:

| Field | Meaning (units) |
| --- | --- |
| `now` | Local time (naive local, or aware local; do not mix) |
| `soc_pct` | Home battery level (%) |
| `battery` | `BatterySpec(capacity_kwh, max_charge_kw, max_discharge_kw, efficiency, reserve_pct)`; in free hours the prediction charges it at `max_charge_kw` until full |
| `tariff` | A `Tariff`, or anything with `import_price(t)`, `export_price(t)` and `is_free(t)` in $/kWh |
| `forecast` | `ForecastSlot(start, p10, p50, p90)` half hours, today 00:00 to tomorrow 24:00 (kW); past slots keep their last values |
| `load` | The `LoadProfile` from `build_load_profile`, rebuilt once a day |
| `house_recent` | `LoadSample(start, kw, minutes)` for the last 24 h (5-min samples are best) |
| `pv_today_kwh` | Solar since local midnight (kWh) |
| `pv_recent_kw` | Solar, mean of the last 15 min (kW) |
| `measured` | `Measured(pv_kw, grid_kw, battery_kw, car_kw, soc_pct)`, the latest 5-min means; used by the safety rules |
| `cars` | One `CarState(car_id, spec, soc_pct, limit_pct, plugged, plugged_since, commanded_a, pending, stale, plans)` per car |
| `forecast_issued_at` | The Solcast `api_last_polled` time |
| `pv_days` | Optional `PvDay(day, actual_kwh, forecast_kwh)` for the last 14 days, from the planner's own store, since HA does not record Solcast's half hours |
| `volts`, `charger_max_a` | 240 V and the Wall Connector's setting (48 A) |

`Decision` returns:

| Field | Meaning |
| --- | --- |
| `mode` | `off` / `empty` / `stale` / `full` / `day` / `night` (`night` is any free hour; the dashboard labels it "Free hours") |
| `car_id` | The car on the charger |
| `commands` | One `CarCommand(car_id, amps, reason)` per car: None = no call; 0 = switch off; > 0 = set the current number, then switch on if it was off |
| `reason` | One sentence for the dashboard |
| `shadow` | True means show, do not send |
| `trajectory` | `TrajectoryPoint(t, soc_pct, soc_expected_pct, soc_no_car_pct, pv_kw, pv_expected_kw, house_kw, car_kw, price)` in 15-min steps to tomorrow's free start: the battery outlook |
| `outlooks` | `CarOutlook(car_id, plugged, need_kwh, target_pct, by, solar_kwh, free_kwh, paid_kwh, short_kwh, summary)` |
| `next_change` | `PlannedChange(at, amps, why)` |
| `safety` | `"paid_import"`, `"battery_floor"` or None |
| `debug` | Planner values, and `tonight_car` |

Each loop of the integration's coordinator does five things:

1. Read the states.
2. Keep 5-min means of the four powers.
3. At start-up, at each date change and every 6 hours, rebuild the load profile from 14 days of hourly statistics and the last 24 hours of 5-minute statistics, off the event loop.
4. Build the `Snapshot` and call `decide`.
5. Unless the decision is shadow, send the plugged car's command; then remember `commanded_a` and save `state_dict()` in the integration's store.

In shadow mode the planner keeps its own virtual current, so the shadow plan evolves as if it had been obeyed. [`tests/planner/core/ha_fixture.py`](../tests/planner/core/ha_fixture.py) shows the whole mapping on HA-shaped data (states, attributes, statistics rows, the schedule config) with made-up values; run it with `python -m tests.planner.core.ha_fixture`.

## 11. Known limits

- **The caution level is not fully honest yet.** The 90% path held 76% of the time in the simulation (section 3). The remedy needs data:
  - in shadow mode, log the predicted level at the start of free hours against the realised one, then either re-scale z or add a sun-dependent house term;
  - neither should be tuned on simulated data.
- **Plans are forced through battery-first physics.** Paid power for a plan comes from the home battery first. On a cloudy daytime-only weekday plan this empties the battery, and in the simulation the free-hours hold kept it low overnight (weekday_plan: 1,500–2,900 car-caused floor minutes per 5 days); with the refill it is full again by morning. On time of use, where nothing is free, night plan charging drains the home battery too, and the planner's only lever is not to charge (tou: about 85 kWh of plan-driven paid import over 3 days for every design). Plans outrank the floor, as the owner asked. The early/late push split is a hand-made rule, and it is sensitive near the boundary.
- **The free-hours refill is assumed to reach 100%.** The planner assumes the Sol-Ark's own settings fill the battery from the grid in free hours, and never checks it. If the refill stops short (a changed time-of-use slot, a lower grid-charge rate), a day that Spread planned down to the wanted level leaves the next paid morning short.
- **Spread is not simulated** (see the top of this page). It cycles the home battery more than Spill alone, roughly the car's share of the battery each sunny day; raising the wanted level at free start limits that.
- **A steady current with a 2-hour commitment is an approximation.** Some midday export can remain when Room binds: 20–26 kWh over 3 flat-tariff days, where the trajectory design leaves 8–9. Taking the measured export when the battery is full was tried; it cost more floor time elsewhere.
- **The simulation's 10 PM night window cost it points.** The simulated planner charged the car from 10 PM, and simulated runs end at midnight, so the last night delivered only two hours. This is why the free-hours-only baseline (which starts at 8 PM) wins the season and winter rows by up to 7 points. The planner now charges from the start of the free hours, as that baseline does.
- **History builds up.** The multi-day PV bias needs past days' solar against their forecast, which the integration keeps in its own store; until it has a few days, the planner uses priors. The house profile comes from the recorder's statistics, so a new installation starts with whatever history the recorder already has.
- **Not measured here:**
  - the real Gen 2 Rivian schedule-write behaviour;
  - Tesla Fleet costs;
  - the real Solcast error structure;
  - stale car data (handled as "send nothing").
  Shadow mode (phase 3) checks all of these.

## 12. Results

**Method.** Every policy ran on the same simulated worlds (the design run's harness, unchanged), on the owner's tariff and the settings of the time: a 10 PM to 6 AM night window, and a battery that held its level in free hours with a 40% check (see the top of this page).

- **Score.** The harness's owner score: lower is better, and 1 point is about $0.20. Paid import, lost export, current changes, billed Tesla calls, minutes below the floor, plan shortfall and the battery's end level all count against a policy; energy delivered to the car counts for it. Costs are measured against a run of the same world in which the car never charges.
- **Seeds.** Each row is the mean of 3 seeds.
- **Groups:**
  - `suite`: the 10 harness scenarios, re-tariffed; `tou` keeps its time-of-use tariff.
  - `ext`: seasons, the AC week, weekend load, flat and free weekend.
  - `traj`: daytime-battery scenarios.
  - `rext`: a weekday plan and storms.
  - `heldout`, new in this run: `winter_short` (21 December, two cars, a weekday plan) and `stale_forecast` (Solcast polls fail for 12 h and the last issue was 40% optimistic).
  - `newseeds`: suite + ext re-drawn with seeds 7–9.

| Scenario | Group | b0 evcc-like | b1 free-only | b2 slow evcc | mpc | trajectory | robust | **final** |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| clear | suite | -50.0 | -74.2 | -63.4 | -69.4 | **-74.6** | -74.3 | -74.3 |
| fast_clouds | suite | 45.5 | 0.8 | 21.3 | **-22.8** | -21.2 | -22.1 | -22.6 |
| overcast | suite | -52.7 | **-66.8** | -41.3 | -63.2 | -65.6 | -61.7 | -62.3 |
| fc_optimistic | suite | 55.9 | 0.8 | 35.2 | -6.9 | **-8.2** | -1.7 | -7.6 |
| fc_pessimistic | suite | 4.9 | 0.8 | -27.7 | **-41.2** | -41.0 | -39.2 | -39.6 |
| battery_low | suite | -44.3 | -77.5 | -63.4 | -73.7 | **-77.6** | -75.9 | -75.9 |
| heavy_evening | suite | 108.0 | 0.8 | 99.5 | -36.3 | -34.5 | **-37.8** | -37.0 |
| two_car | suite | -81.8 | -59.0 | -91.4 | -101.6 | -100.3 | **-104.1** | -103.9 |
| deadline | suite | -101.6 | -96.7 | **-102.9** | -92.0 | -94.6 | -93.0 | -92.9 |
| tou | suite | 176.0 | 208.0 | 169.1 | 136.1 | **117.4** | 143.9 | 143.4 |
| season_summer | ext | -26.3 | **-68.4** | -27.0 | -60.2 | -63.8 | -61.3 | -62.0 |
| season_autumn | ext | 146.3 | **-68.4** | 177.9 | -62.9 | -60.1 | -62.6 | -63.0 |
| season_winter | ext | 36.0 | **-68.4** | 39.4 | -63.9 | -63.9 | -63.9 | -63.9 |
| season_spring | ext | 29.6 | **-68.4** | 49.3 | -62.5 | -62.8 | -62.2 | -62.2 |
| winter_daytime | ext | 4.7 | **0.8** | 9.0 | **0.8** | **0.8** | **0.8** | **0.8** |
| ac_week | ext | 112.3 | 1.9 | 49.9 | -75.0 | **-76.2** | -73.4 | -71.4 |
| weekend_load | ext | -43.1 | **-96.4** | -77.3 | -94.1 | -95.4 | -95.6 | -96.0 |
| flat | ext | 122.6 | 234.0 | 158.6 | -37.2 | **-41.2** | -34.2 | -33.7 |
| free_weekend | ext | 45.2 | -82.6 | 88.3 | **-90.9** | -90.6 | **-90.9** | -90.2 |
| dayseason_summer | traj | 18.9 | 1.1 | -22.2 | -33.3 | -31.3 | **-34.8** | -34.6 |
| dayseason_autumn | traj | 234.0 | 1.1 | 281.2 | -4.1 | -4.9 | **-5.2** | -5.1 |
| dayseason_winter | traj | 45.8 | **1.1** | 49.6 | **1.1** | **1.1** | **1.1** | **1.1** |
| dayseason_spring | traj | 183.4 | 1.1 | 193.5 | -7.7 | -8.3 | -1.6 | **-8.6** |
| ac_home | traj | -52.2 | -114.7 | -91.8 | -112.9 | **-115.4** | -114.2 | -114.0 |
| ac_day | traj | 148.5 | 1.4 | 171.5 | -30.8 | **-34.3** | -33.5 | -34.1 |
| weekend_day | traj | 24.5 | 1.3 | -40.8 | -53.9 | **-54.5** | -53.1 | -52.9 |
| weekday_plan | rext | 666.2 | 583.7 | 656.9 | 379.3 | 652.3 | **344.9** | 370.1 |
| storms | rext | 40.8 | 0.5 | 48.5 | -6.5 | -7.6 | -9.5 | **-10.1** |
| winter_short | heldout | **-95.2** | **-95.2** | -70.5 | -88.2 | -88.2 | -88.2 | -88.2 |
| stale_forecast | heldout | 34.0 | 0.8 | 7.2 | -15.4 | -15.8 | **-17.4** | -15.0 |

| Group | b0 evcc-like | b1 free-only | b2 slow evcc | mpc | trajectory | robust | **final** |
|---|---:|---:|---:|---:|---:|---:|---:|
| suite | 59.8 | -162.9 | -65.0 | -371.2 | -400.2 | -366.0 | -372.7 |
| ext | 427.3 | -216.0 | 468.0 | -545.9 | -553.3 | -543.3 | -541.5 |
| traj | 602.9 | -107.6 | 541.0 | -241.5 | -247.6 | -241.2 | -248.1 |
| rext | 707.0 | 584.2 | 705.4 | 372.9 | 644.7 | 335.4 | 360.0 |
| heldout | -61.2 | -94.4 | -63.4 | -103.5 | -104.0 | -105.6 | -103.1 |
| newseeds (19 scenarios, seeds 7–9) | 386.7 | -355.4 | 359.8 | -909.3 | -978.3 | -928.3 | -931.6 |
| **all 72 rows** | 2122.5 | -352.1 | 1945.8 | -1798.7 | -1638.7 | -1849.0 | -1837.1 |

The next two tables total the same runs with the car-caused metrics: kWh, dollars and minutes compared with a run where the car never charges. Battery>car and sun>car are energy delivered to the car.

All 72 rows, including time of use, flat and plan-forced scenarios:

| Policy | Paid kWh | Paid $ | Floor min | Car kWh | Sun>car | Battery>car | Plans met | Changes/day | Tesla calls | ms/step |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| b0 evcc-like | 287 | 60.17 | 30,997 | 6,345 | 2,956 | 257 | 72/72 | 27.9 | 3,066 | 0.05 |
| b1 free-only | 433 | 90.60 | 5,427 | 4,565 | 138 | 187 | 51/72 | 2.1 | 112 | 0.03 |
| mpc | 255 | 46.88 | 3,968 | 5,898 | 2,198 | 597 | 71/72 | 4.2 | 361 | 6.60 |
| trajectory | 234 | 39.52 | 5,422 | 5,879 | 2,262 | 358 | 72/72 | 3.6 | 310 | 0.14 |
| robust | 264 | 48.16 | 4,358 | 5,917 | 2,301 | 388 | 72/72 | 3.9 | 326 | 0.59 |
| **final** | 266 | 48.17 | 4,238 | 5,894 | 2,280 | 377 | 72/72 | 3.9 | 324 | 2.02 |

The owner's tariff only: free nights, no plan that forces paid power. That is 126 runs and 453 simulated days.

| Policy | Score | Paid kWh | Floor min | Car kWh | Sun>car | Battery>car | Changes/day | Tesla calls |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| b0 evcc-like | +2423 | 179.2 | 59,150 | 15,801 | 7,019 | 527 | 29.3 | 8,903 |
| trajectory | −6738 | 0.7 | 345 | 14,556 | 5,336 | 692 | 3.3 | 834 |
| robust | −6646 | 4.2 | 2,455 | 14,750 | 5,737 | 858 | 3.8 | 899 |
| **final** | −6682 | 2.6 | 1,420 | 14,695 | 5,679 | 843 | 3.7 | 892 |

**Against the evcc-like baseline**, on the owner's tariff:

- **paid import:** final cuts car-caused paid import from 179 kWh to 2.6 kWh;
- **floor time:** from 59,150 to 1,420 minutes below the floor;
- **changes:** from 29 current changes a day to 3.7, and from 8,903 billed Tesla calls to 892;
- **energy into the car:** final puts 81% as much solar straight into the car as b0 (5,679 against 7,019 kWh). b0 follows every minute of surplus. Final lets the battery absorb the swings and fills the rest from free hours, which costs nothing on this tariff.

**Held-out results.** On the held-out sets final is as good as on the tuning set:

- `stale_forecast`: −15.0, against +34.0 for b0;
- `winter_short`: the same as every design;
- new seeds: −931.6 for final, against −928 (robust), −909 (MPC) and −978 (trajectory, whose lead there is time of use −22, flat −10 and the AC week −10).

## 13. Where things are

| Path | What |
| --- | --- |
| [`custom_components/solar_ev_planner/core/`](../custom_components/solar_ev_planner/core/) | The package: `types.py`, `tariff.py`, `solcast.py`, `load_profile.py`, `pv.py`, `house.py`, `battery.py`, `planner.py`, `entities.py` |
| [`tests/planner/core/`](../tests/planner/core/) | Its 59 unit tests, including the HA-shaped end-to-end fixture `ha_fixture.py` |
| [`custom_components/solar_ev_planner/`](../custom_components/solar_ev_planner/) | The Home Assistant integration that feeds it and sends its commands ([PLANNER.md](PLANNER.md)) |
| [`tools/replay.py`](../tools/replay.py) | Runs the planner over the owner's recorded days ([TESTING.md](TESTING.md)) |

The simulation harness, the comparison and calibration tools and the chart data belonged to the design run and are not in this repository; the numbers in this document come from that run. On the owner's own data, `tools/replay.py` and shadow mode take their place: they show what the planner would have done on real days, and how its battery prediction compared with what happened.
