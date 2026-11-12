---
sidebar_label: Configuration
---

# SolarSage

**What it is.** The SolarSage (`custom_components/solar_ev_planner`) is the Home Assistant integration that decides each car's charging current. It plans from the Solcast forecast, the house's recent use and the home battery's predicted level, and commands the car on the Wall Connector: the Tesla through HA core Tesla Fleet, the Rivian through this repository's [Rivian schedule control](RIVIAN_SCHEDULE_CONTROL.md), which writes the Rivian's charging schedule through Rivian's API. How it decides, and how an earlier version did in simulation, is in [PLANNER_DESIGN.md](PLANNER_DESIGN.md). Testing it with real data, step by step, is in [TESTING.md](TESTING.md).

**Status: software-tested only.** Its tests run against Home Assistant 2026.10.3, with made-up states, a real recorder and Schedule helper, and the real Rivian schedule control over a simulated Rivian account. It has not been run against a real Home Assistant installation. **It starts in shadow mode: it plans and explains, but sends nothing until you turn shadow mode off.**

## What it does

- **Every minute** it reads the Sol-Ark, the Wall Connector, Solcast, the free-hours schedule and both cars. House use is `solar + grid − battery − Wall Connector` (there is no house sensor).
- **Every 15 minutes** (and at once on a plug-in, a new forecast or a mode change) it plans one steady current for the car on the charger: excess solar first, then the home battery within the floor rule (with free hours ahead, what it would still hold above the wanted level when they begin), then free hours, and paid grid power only when a plan cannot be met otherwise.
- **In free hours** the car on the charger charges at its full current (the car's maximum, capped by the Wall Connector) from the moment they begin until its charge limit. The Rivian gets this as one charging-schedule window to the end of the free hours, written once. The planner counts on the Sol-Ark's own settings to charge the home battery from the grid meanwhile, and predicts it charging to full.
- **Every minute** its safety rules check for paid import and the battery at the floor.
- **It commands only the car it sees on the charger**, and only what differs from what the car already holds. A pause is the charge switch, never 0 A.
- **It explains itself**: a mode, a one-sentence reason, the next planned change, the predicted battery path and each car's outlook.

## Install

Follow [Installation and upgrades](installation.md). HACS and manual installation both install the complete SolarSage integration, including bundled Rivian schedule control.

The site planner starts in shadow mode. For Rivian, configure its bundled controller from the SolarSage setup menu and leave dry run enabled while validating inputs.

### Before adding it

1. **Solcast** ([BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar)) with its forecast-today and forecast-tomorrow sensors. Their `detailedForecast` attribute gives the half hours with `pv_estimate10`, `pv_estimate` and `pv_estimate90`.
2. **A Schedule helper for the free hours.** Settings → Devices & services → Helpers → Create helper → Schedule. Name it "Free hours" (it becomes `schedule.free_hours`) and paint 00:00–06:00 and 20:00–24:00 on every day. Repaint it when the electricity plan changes. Leave it out for a flat or time-of-use plan.
3. **Tesla Fleet** set up for the Tesla, with its public key hosted at home ([plan, key hosting](PLAN.md#phase-0-hosting-the-key-through-traefik)), the virtual key approved in the car ([plan, the Tesla checklist](PLAN.md#phase-0-the-tesla-checklist)), and tesla_custom removed. The planner does not use tesla_custom: on a 2020 Model Y it cannot command without its proxy add-on, and its entities take Tesla Fleet's IDs. Teslemetry, the fallback, has the same entity IDs.
4. **Bundled Rivian schedule control** configured ([guide](RIVIAN_SCHEDULE_CONTROL.md#install-and-configure)). It can stay in dry run until the attended trial.

## Configure

Settings → Devices & services → Add integration → **SolarSage → Site planner**. There is one site planner per Home Assistant.

### The site: three steps

The form suggests example entity IDs only when those entities exist. Map every input to your own installation. Change any of them later with **Configure** on the planner's entry; saving reloads it and keeps its memory.

| Step | Field | Example entity or default |
| --- | --- | --- |
| Solar and home battery | Solar power | `sensor.sol_ark_inverter_pv_power` (W) |
| | Grid power | `sensor.sol_ark_inverter_grid_power` (W). The planner needs import positive and export negative; turn on **Invert** if yours is the other way. |
| | Battery power | `sensor.sol_ark_inverter_battery_power` (W). The planner needs charging positive. |
| | Battery level | `sensor.sol_ark_inverter_battery_state_of_charge` |
| | Battery capacity sensor | `sensor.sol_ark_battery_capacity_estimate` (optional; the fixed capacity, 31 kWh, is used without it) |
| | Maximum charge and discharge power | 9 kW each |
| | Battery reserve | 10%: where the inverter stops discharging |
| Wall Connector | Power | `sensor.tesla_wall_connector_total_power` (kW). **Required:** without it the planner cannot tell the house from the car, and does not plan. |
| | Vehicle current | `sensor.garage_tesla_wall_connector_vehicle_current` |
| | Vehicle connected | `binary_sensor.tesla_wall_connector_vehicle_connected` (recommended: off means no car is commanded) |
| | Contactor closed | `binary_sensor.tesla_wall_connector_contactor_closed` |
| | Maximum current, voltage | 48 A, 240 V |
| Forecast and free hours | Solcast forecast today, tomorrow | `sensor.solcast_pv_forecast_forecast_today`, `sensor.solcast_pv_forecast_forecast_tomorrow` |
| | Solcast last polled | `sensor.solcast_pv_forecast_api_last_polled` (optional: an old forecast widens the uncertainty) |
| | Free hours | `schedule.free_hours` (optional) |

Each power has a unit (W or kW). A sensor's own unit always wins; the configured one is for sensors that state none.

### Cars

On the planner's entry, choose **Add car**, then Rivian or Tesla. Each car is a subentry; **Reconfigure** changes it later.

| Field | Rivian (R1S) | Tesla (Tesla Fleet) |
| --- | --- | --- |
| Name | `R1S` | `Tesla` |
| Charge switch | `switch.r1s_schedule_control_solar_charging` (Rivian schedule control) | `switch.tesla_charge` |
| Charge current | `number.r1s_schedule_control_solar_current` (Rivian schedule control) | `number.tesla_charge_current` |
| Battery level | `sensor.r1s_battery_state_of_charge` | `sensor.teslamate_tesla_battery_level` (from [TeslaMate](#the-tesla-spending-guard); without it `sensor.tesla_battery_level`) |
| Charge limit | `sensor.r1s_battery_state_of_charge_limit` | `sensor.teslamate_tesla_charge_limit` (or `number.tesla_charge_limit`) |
| Charge cable connected | `binary_sensor.r1s_charger_connection` | `binary_sensor.teslamate_tesla_plugged_in` (or `binary_sensor.tesla_charge_cable`) |
| Location | `device_tracker.r1s_location`, or `device_tracker.r1s` on a fresh upstream 2.0.0 install (the form suggests whichever exists) | `device_tracker.teslamate_tesla_location` (or `device_tracker.tesla_location`) |
| Schedule control state | `sensor.r1s_schedule_control_control_state` (recommended: it tells a pause from no request) | — |
| Charging state | — | `sensor.teslamate_tesla_charging` (or `sensor.tesla_charging`; optional, recommended: the [Tesla guard](#the-tesla-guard) reads it) |
| Status | — | `binary_sensor.teslamate_tesla_status` (or `binary_sensor.tesla_status`; optional, recommended: on while the car is online; the [spending guard](#the-tesla-spending-guard) counts a wake only while it is not on, and without it counts one for every command) |
| Minimum, maximum, step | 8 A, 48 A, 2 A | 6 A, 48 A, 1 A |
| Usable pack energy | 135 kWh | 75 kWh |

- **The name sets the car's entity IDs:** R1S gives `…r1s_planner_…`, Tesla gives `…tesla_planner_…`.
- **The car's own current number caps the maximum:** Tesla Fleet's says what the car accepts on this charger.
- **Car ids** are `rivian` and `tesla` (a second car of a kind becomes `rivian_2`). The charger car override uses them, and Rivian schedule control's vehicle override treats `rivian` as its own car.
- **Never map the planner's own entities** as a car's switch or current; the form refuses them.
- **tesla_custom's entities are never suggested.** If its names (`switch.tesla_charger`, `number.tesla_charging_amps`) still exist, remove tesla_custom; `python -m tools.check` warns about them.

### Electricity plans

The free hours are the main entry's Schedule helper, at $0. In them, by night or by day, the car on the charger charges at its full current to its limit. Every other price level is a **price window**: on the planner's entry choose **Add price window**, pick a Schedule helper painted for that level, a name and a price (and optionally an export price). The price is then a number entity, `number.solarsage_<name>_price`, on its own device. The free hours apply first, then the price windows in the order they were added; outside all of them the planner's **Import price** applies, and the **Solar buyback** for export.

| Plan | Set up |
| --- | --- |
| Free nights (today: 8 PM–6 AM free, $0.32/kWh otherwise, $0.00 buyback) | Free hours schedule painted 20:00–06:00; Import price 0.32; Solar buyback 0 |
| Time of use | No free hours. A price window per level (for example "Peak" and "Off peak"); Import price for the rest |
| Flat | No free hours, no price windows; Import price |
| Free weekends | Free hours schedule painted over the weekend (and any free nights) |

## Pointing Rivian schedule control at the planner

Through three entities the planner provides, Rivian schedule control parks the Rivian's charging window by day if the planner stops, settles the planner's requests after a restart, and writes the free hours as one window. In the schedule control's **Configure**:

| Schedule control option | Set it to | What the planner's entity does |
| --- | --- | --- |
| Controller liveness (`controller_liveness_entity`) | `sensor.solarsage_heartbeat` | `shadow` or `active` while the planner is on and has decided within the last 3 minutes; unavailable while Planning is off, while it cannot plan (missing site readings, an unreadable schedule, an error) or once it has stopped deciding. It re-checks itself every 30 seconds. |
| Controller wants charging (`controller_charging_entity`) | `binary_sensor.r1s_planner_charging_wanted` | On while the last command the car accepted was a current, off while it was a pause; unknown in shadow mode, while Planning is off, and before the first command of a plug-in. It survives restarts. |
| Controller charging until (`controller_until_entity`) | `sensor.r1s_planner_charging_until` | The end of the free hours (06:00 on today's plan) while the planner charges the Rivian through them. It is set before the command it belongs to, so the schedule control writes that command once, as one window at full current to that time, and does not renew it. Unknown otherwise: by day, in shadow mode, while Planning is off, while the car is unplugged or at its limit. It survives restarts. |
| Wall Connector vehicle override (`vehicle_override_entity`) | `select.solarsage_charger_car_override` | `auto`, `rivian` or `tesla`; the schedule control treats `auto` and `rivian` as no override |
| Wall Connector vehicle connected (`wall_connector_entity`) | `binary_sensor.tesla_wall_connector_vehicle_connected` | The same sensor the planner reads |

- **Leave the increase margin and the ceiling hold at 0**, their defaults; the planner asks for exact currents and paces itself.
- **The heartbeat stays up in shadow mode,** so mapping it does not block the attended Rivian trial.
- **To check the mapping,** run `python -m tools.check` ([TESTING.md](TESTING.md)): the rows "rivian controller liveness" and "rivian controller charging" read what the schedule control itself sees. The check has no row for the until option, which is unknown until the planner first charges the Rivian through free hours; then `controller_until` under the control state's `observation` attribute shows what the schedule control reads.
- **One window per free night.** The window is not parked while the heartbeat is down, so turning Planning off in the free hours leaves the Rivian charging on free power until they end. When they end, the car stops by itself and nothing is written at 06:00. By day the schedule control still uses short leases, renewed while the heartbeat is up.

What the planner sends the schedule control, and the schedule control's rules for requests, are in [Driving schedule control from the planner](RIVIAN_SCHEDULE_CONTROL.md#driving-schedule-control-from-the-planner).

## Entities

### The planner (device "SolarSage")

| Entity | What it shows |
| --- | --- |
| `sensor.solarsage_mode` | `day` (solar charging), `night` (shown as "Free hours": the car charges at its full current), `full` (car at its limit), `stale` (car data stale), `empty` (charger empty), `off`, or `no_data` (cannot plan). Attributes: shadow, commanding, the car, the safety rule that acted. |
| `sensor.solarsage_reason` | The decision in one sentence; `full_reason` holds all of it |
| `sensor.solarsage_next_change` | When the current is expected to change next; attributes: the current and why |
| `sensor.solarsage_battery_minimum` | The home battery's predicted lowest level (at the caution level) until tomorrow's free hours (tomorrow's sunset when there are none). Attributes: when, the expected-day and no-car minimums, the floor, the level when free hours start, and the half-hourly path (not recorded). |
| `sensor.solarsage_battery_minimum_time` | When that minimum is reached |
| `sensor.solarsage_forecast_correction` | The factor today's production so far puts on the rest of today's forecast; attributes: the evidence weight, the multi-day bias, the forecast's age, and p10/p50/p90 left today |
| `sensor.solarsage_car_on_charger` | `none`, `rivian` or `tesla`, with why, the candidates, the Wall Connector readings and `tonight_car`: the car that should be on the charger in the coming free hours |
| `sensor.solarsage_heartbeat` | Rivian schedule control's liveness entity (above) |
| `sensor.solarsage_tesla_api_spend` | With a Tesla only: what the planner's own Tesla Fleet calls cost this calendar month, in USD at Tesla's prices. Attributes: the counts (`commands`, `wakes`, `data_requests`, `refreshes`), the `level` (`normal`, `essential_only`, `stopped`), the budget, Tesla Fleet's polling (`on`, `off`, `unknown`) and its estimate, the `cap`, where essential-only starts and where it stops, what is `left`, how wakes are counted, the last refresh, and last month's total. See [the spending guard](#the-tesla-spending-guard). |

### Each car (device "R1S planner", "Tesla planner")

| Entity | What it shows or sets |
| --- | --- |
| `binary_sensor.r1s_planner_charging_wanted` | Rivian schedule control's controller charging entity (above) |
| `sensor.r1s_planner_charging_until` | Rivian only: Rivian schedule control's controller until entity (above). The end of the free hours the Rivian's standing request belongs to. It is set when the planner charges the Rivian in free hours, also for a car already at its limit, and it keeps that time, even once it has passed, until a daytime command has replaced the night's request, so the ended window is not started again on it (a full car gets no command). Unknown before the first free-hours charge of a plug-in, after unplug, and while Planning is off. |
| `sensor.r1s_planner_planned_current` | The current the plan has the car at now (in shadow mode, the virtual car's); 0 off the charger. Attributes: the wanted and commanded current, what the car or Rivian schedule control holds, the schedule control state, why a command waits, the last error. |
| `sensor.r1s_planner_outlook` | Where the car's energy will come from, in words ("covered by sun", "about 7 kWh in free hours", "about 3 kWh paid") |
| `sensor.r1s_planner_energy_from_sun`, `_energy_in_free_hours`, `_energy_paid` | The same in kWh |
| `number.r1s_planner_charge_limit` | The limit the planner charges to; 0 uses the car's own (default) |
| `number.r1s_planner_plan_target`, `time.r1s_planner_plan_time`, `select.r1s_planner_plan_days` | A plan: "target % by time", on `off` (default), `once`, `weekdays`, `daily` or `weekends`. `once` is for the next such time and turns itself off afterwards. |
| `number.tesla_planner_restore_current_at_unplug` | Tesla only: the current set back when the Tesla is unplugged at home after the planner changed it (default 48 A; 0 turns it off). Rivian schedule control restores the owner's schedule itself. |

## Settings

All settings are entities on the planner's device. They are kept in the planner's store and survive restarts and updates. Every change re-plans at once.

| Entity | Default | Range | Meaning |
| --- | --- | --- | --- |
| `switch.solarsage_planning` | on | | Off: no plans and nothing sent; see [Planning off and shadow mode](#planning-off-and-shadow-mode) |
| `switch.solarsage_shadow_mode` | **on** | | Plan and explain, send nothing |
| `switch.solarsage_daytime_solar_charging` | on | | Off: a car plugged in by day waits for the free hours |
| `select.solarsage_caution` | conservative | near-certain, conservative, balanced, typical day | How much of the forecast to count on (97%, 90%, 75%, 50%) |
| `select.solarsage_night_charging_start` | with the free hours | with the free hours, or a half hour from 18:00 to 05:30 | In free hours the car waits until this time, then charges at its full current. Outside the free hours and without sun the car stays paused either way, unless a car's plan needs paid hours to make its deadline. A time outside the free period means its start. While the car waits, the Rivian gets no free-hours window yet |
| `number.solarsage_battery_floor` | 30% | 5–95% | The predicted battery must stay above it |
| `number.solarsage_battery_wanted_at_free_hours` | 30% | 0–100% | What the battery should still hold when free hours start (at sunset without free hours). With free hours ahead, the car may use the battery down to it by day, since the Sol-Ark refills it from the grid in them; raise it to keep more in the battery |
| `number.solarsage_minimum_time_between_changes` | 15 min | 1–120 min | |
| `number.solarsage_largest_change` | 6 A | 1–48 A | |
| `number.solarsage_paid_grid_tolerance` | 5 min | 0–60 min | How long paid import is accepted before the safety rule cuts the car |
| `number.solarsage_import_price` | 0.32 | 0–5 per kWh | Price outside the free hours and price windows |
| `number.solarsage_solar_buyback` | 0.00 | 0–5 per kWh | What exported solar earns |
| `select.solarsage_charger_car_override` | auto | auto, rivian, tesla | Names the car on the charger when both report being plugged in at home |
| `number.solarsage_tesla_api_budget` | $5.00 | $0–10 | With a Tesla only: what the planner's own Tesla calls may cost a month ([spending guard](#the-tesla-spending-guard)) |
| `number.solarsage_tesla_fleet_polling_estimate` | $5.20 | $0–30 | With a Tesla only: what Tesla Fleet's own polling may cost a month, kept free of the credit while that polling is on (not counted once it is off) |
| `number.solarsage_tesla_refresh_interval` | 60 min | 0–240 min | With a Tesla only: how often the planner refreshes a plugged-in Tesla while Tesla Fleet's polling is off; 0 turns the planner's refreshes off |

## Shadow mode and commanding

### Shadow mode (the default)

- The planner plans every cycle **as if it had been obeyed**: a virtual car follows its plan, and the dashboard shows that plan.
- **It calls no service.** The only call it makes is reading the Schedule helpers' weekly configuration (`schedule.get_schedule`), which changes nothing.
- The heartbeat shows `shadow`, and charging-wanted stays unknown, so Rivian schedule control neither starts nor drops anything because of the planner.
- The cars charge as they do today, from their own schedules or apps. The planner's reasons describe what it would do; while a car draws on its own, the measured import and battery differ from the plan.

### Turning shadow mode off

Turn `switch.solarsage_shadow_mode` off. From the next cycle:

1. The planner starts a fresh session and sends its first decision at once to the car on the charger, and only to that car.
2. **Tesla:** the charge current, then the charge switch. Every command is billed by Tesla's API, so a command is never repeated on the planner's own initiative.
3. **Rivian:** Rivian schedule control's current, then its switch. In the free hours the charging-until sensor is set first, so the schedule control writes one window to their end. If the schedule control drops the request (after a restart, an outage or a replug), the planner sends it again, except while the schedule control is handed back, faulted or waiting for the vehicle. With the schedule control still in dry run, the requests are recorded but nothing is written to Rivian.
4. **Limits:** the planner's own (15 minutes between changes, 6 A at a time; in free hours the car goes straight to its full current), and on top at most one command per car per minute and 12 an hour (a pause is never held back), with a back-off after a failure of up to 15 minutes. The hourly count survives restarts. For the Tesla, the [Tesla guard](#the-tesla-guard) and the [spending guard](#the-tesla-spending-guard) also apply.
5. **At unplug** the Tesla's current is set back to its restore current, if the planner changed it during that plug-in.

### The Tesla guard

Each Tesla command is billed and may wake the car. A start refused because the car is at its limit, retried every 15 minutes with a wake, would cost about $2 a day. So for the Tesla:

- **No start while the car cannot start.** Nothing is sent, not even the current, while its charging state (`sensor.tesla_charging`, if mapped) reads complete, disconnected or no power, while its level is at or above its own limit, or after it answered a start with "complete" and its plug, level and limit have not changed since. The command waits and is checked again each cycle, which costs nothing.
- **"Already there" is success.** A refusal that says the car already is where the command would take it counts as done: "complete" or "is_charging" for a start, "not_charging" for a pause.
- **Three failures in a row stall it.** No more starts or current changes for 3 hours, or until its plug, level or limit takes a new known value; then it tries at once. A value going unavailable does not count. A pause keeps the short back-off.
- **It survives restarts:** the hourly command count, the "complete" answer and the stall are saved.

The car's planned-current sensor shows why a command waits. The Rivian has no guard: its commands are not billed, and it keeps the back-off of up to 15 minutes.

### The Tesla spending guard

Tesla's Fleet API bills every request below status 500 (2026 prices, USD): a command $0.001, a data request $0.002, a wake $0.02, with a $10 discount per account and month. The guard makes sure the planner's own calls, with Tesla Fleet's polling, stay inside that discount, so the owner is never charged.

- **It counts every billable call the planner causes**, per calendar month, kept across restarts: each charge switch or charge current call (also when refused or failed), a wake for each send to a Tesla whose status (`binary_sensor.tesla_status`, mapped on the car) does not read online, with its three follow-up state checks as data requests, and each refresh as two data requests. Without the status sensor every send counts a wake. `sensor.solarsage_tesla_api_spend` shows the total and the counts.
- **Its cap** is the smaller of the budget (default $5) and $10 less a $1 margin less Tesla Fleet's polling estimate (default $5.20: about 84 reads a day with the car awake 14 hours). With Tesla Fleet polling the cap is $3.80; once its polling is off, the estimate is not counted and the cap is the budget.
- **From 70% of the cap: essential commands only.** The Tesla is started only in the free hours, and by day a decrease becomes a stop, so the car never draws paid power the planner did not want: the free hours' end and the paid-import and battery-floor safety rules stop it. Daytime starts and current changes, timed refreshes and the restore at unplug wait. The reason says so.
- **At the cap less the cost of one more send: nothing.** No start, current change or refresh until the first of the next month. A stop to a Tesla that is charging still goes out: it is awake, so the stop costs one command, far less than paid-hours charging. A car that is not charging keeps its last state: set a Tesla charging schedule or a Wall Connector schedule as a backstop for paid hours. The reason says so, and the car's planned-current sensor says why its command waits.
- **The Rivian is never counted:** its commands are not billed.

**Tesla Fleet's polling.** Tesla Fleet polls an awake car every 10 minutes: its state, then vehicle_data, $5 or more a month (more if Tesla bills the state check, which HA calls free). The planner's refreshes cost less, so turn that polling off: Settings → Devices & services → Tesla Fleet → ⋮ → System options → "Enable polling for updates" off. The guard reads that option. While it is off, the planner calls `homeassistant.update_entity` on a Tesla Fleet entity of the car (its charge switch):

- once when the Wall Connector reports a car connected (is it the Tesla?) and once when the Tesla leaves it, so its plug reading changes (not with TeslaMate's readings, below);
- every `number.solarsage_tesla_refresh_interval` (60 minutes) while the Tesla is on the Wall Connector, never while it is off the charger (not with TeslaMate's readings);
- before a command when its data is more than 10 minutes old, so Tesla Fleet neither wakes an awake car (its status would read asleep) nor commands an asleep one.

The refreshes run while Planning is on, also in shadow mode, and never while Tesla Fleet polls. At about 14 hours a day on the charger, hourly refreshes cost about $1.70 a month. Keep `binary_sensor.tesla_status` enabled and mapped, Sentry Mode off at home, and check the developer dashboard weekly: the guard counts only the planner's calls, not the Tesla app's or other automations'.

**TeslaMate for the Tesla's readings.** TeslaMate already follows the car through Tesla's streaming API, outside the developer app, so its readings cost nothing. For an optional TeslaMate setup, a Mosquitto add-on can bridge the topics the planner needs from your TeslaMate broker (`/share/mosquitto/teslamate.conf`, kept in [`deploy/mosquitto/teslamate.conf`](https://github.com/bballdavis/SolarSage/blob/main/deploy/mosquitto/teslamate.conf); read-only, no GPS), and an MQTT discovery message (`homeassistant/device/teslamate_car_1/config`, retained) makes the device "Tesla (TeslaMate)":

| Entity | From TeslaMate | Notes |
| --- | --- | --- |
| `sensor.teslamate_tesla_battery_level` | `battery_level` | % |
| `sensor.teslamate_tesla_charge_limit` | `charge_limit_soc` | % |
| `binary_sensor.teslamate_tesla_plugged_in` | `plugged_in` | |
| `sensor.teslamate_tesla_charging` | `charging_state` | lowercased as Tesla Fleet's (`no_power` for NoPower) |
| `binary_sensor.teslamate_tesla_status` | `state` | on while online, charging, driving, updating or suspended; off asleep or offline |
| `device_tracker.teslamate_tesla_location` | `geofence` | home inside TeslaMate's home geofence (its name is in the discovery message) |
| `sensor.teslamate_tesla_charger_current`, `…_charger_power`, `…_charge_current_request`, `…_state` | | for the dashboard |

All of them are unavailable while the bridge is down (`bridge/teslamate/state` 0). Mapped on the car (the form suggests them when they exist), they replace the timed refreshes: the planner refreshes Tesla Fleet only before a command whose data is more than 10 minutes old, on a Tesla Fleet entity of the car (the charge switch), because Tesla Fleet still decides from its own last read whether to wake the car. The guard reads Tesla Fleet's polling option from that entity too. The charge switch and current stay Tesla Fleet's: every command still goes through it.

**Tesla's account.** Add no payment method and leave the billing limit at $0. Tesla suspends an app that goes past its limit until the next billing cycle, so a mistake should cost a suspended API, not money (Tesla's billing page was read through search extracts only; confirm it once on developer.tesla.com). The whole plan for staying inside the free credit, Sentry Mode off at home included, is in [the plan](PLAN.md#phase-0-staying-inside-the-free-credit).

**Settings.** Keep the defaults: a $5 budget (`number.solarsage_tesla_api_budget`), a $5.20 polling estimate (`number.solarsage_tesla_fleet_polling_estimate`, counted only while Tesla Fleet polls) and a 60-minute refresh interval (`number.solarsage_tesla_refresh_interval`). Lower the budget for more room under the credit. Raise the polling estimate if you leave Tesla Fleet's polling on and the dashboard shows more. A longer refresh interval saves a little but plans on older battery levels.

**Hosting the key costs nothing.** Tesla reads the public key at registration and pairing, not through the Fleet API, so it is never billed ([key hosting](PLAN.md#phase-0-hosting-the-key-through-traefik)).

### Planning off and shadow mode

| | Planning on, shadow mode on | Planning on, shadow mode off | Planning off |
| --- | --- | --- | --- |
| Commands | none | to the car on the charger | none |
| Heartbeat | `shadow` | `active` | unavailable |
| Charging wanted | unknown | on or off | unknown |
| Charging until | unknown, or the end kept from the last command | the end of the free hours the Rivian's request belongs to | unknown |
| Tesla | as you left it | follows the plan | keeps its last current, up to its own limit |
| Rivian (schedule control pointed at the planner) | as you left it | follows the plan | by day the schedule control stops renewing at once and parks its window after 3 minutes, so charging stops; in the free hours the night's one window is not parked, so the car charges on free power until they end. The owner's schedule comes back at unplug or with **Restore owner schedule** |

- **Going back to shadow mode** sends nothing, so each car keeps its last command: the Tesla its current, and the Rivian its request, which Rivian schedule control keeps renewing while the heartbeat is up. In the free hours the Rivian's window runs to their end; after it the schedule control starts short leases for the same request, so the car charges on at full current into paid hours. To stop a car as well, pause it first (in its app, or for the Rivian with the schedule control's **Restore owner schedule**), or turn Planning off.
- **When the planner cannot plan** (mode `no_data`: a site reading missing, the free-hours schedule unreadable, an error), the heartbeat goes unavailable just as with Planning off. The reason sensor says why. A stale car (its own data unavailable) is not commanded, but the heartbeat stays up, so the Rivian keeps its last current until its data returns.

### Which car is on the charger

- The Wall Connector showing no vehicle rules out every car.
- Otherwise a car is a candidate while its own cable sensor is on and it is home (its tracker says `home`).
- The charger car override, when not `auto`, leaves only the car it names.
- Two candidates are ambiguous: the planner commands neither until the override names one.

### Freshness

An unchanged value keeps an old timestamp in Home Assistant (the battery level, the Wall Connector's power at 0), so no value is stale because of its age. The site counts as stale only when all of solar, grid, battery, Wall Connector power and the battery level have been silent for 30 minutes, or when an integration behind them is not loaded. A car is stale when its own values are unavailable or its integration is not loaded.

## The dashboard card

The planner ships its own card, in the Daylight look. Home Assistant loads it with the integration, so there is no Lovelace resource to add. After installing or updating the planner, restart Home Assistant and reload the browser once.

Add a dashboard (Settings → Dashboards → Add dashboard → New dashboard from scratch), switch its view to **Panel (single card)**, and give it this card:

```yaml
type: custom:solar-ev-planner-card
sky: auto      # auto follows sun.sun; day or night to fix the look
fill: true     # fill the window; use false in a sections or masonry view
```

It needs no entity IDs: it finds the planner's entities itself.

| Area | What it shows | What you can do |
| --- | --- | --- |
| Header | The mode, and whether the planner is off, in shadow mode or live | Turn Planning, Shadow and Daytime solar on or off. Turning shadow mode off, or the planner off while live, asks first |
| On the Charger | The car, its level and limit, what the planner sends (or would send) beside what the Wall Connector reads, the reason, the next change, the outlook and the plan. The other cars are listed underneath | |
| Sun / Night chart | **Sun (Today: Solar, House & Car):** today's daylight, actuals first: solar produced, house use (solar + grid − battery − car, as the planner reads it) and what the car drew. After now, the solar line is the planner's own expectation (Solcast corrected by today's production) with the planned car power. Solcast's median and range sit faintly behind for reference. Underneath: solar so far against what Solcast expected by now, house and car energy today, and the rest of today with Solcast's figure beside it. When solar runs 10% or more off the forecast, a line says how the planner is handling it. **Night (Tonight: Free Hours):** the evening and the free hours, when the car starts, tonight's planned charging and what the car drew, with a warning if the car would charge, or charged, on paid power after dark | Switch between Sun and Night. Point at the chart for the values at that time |
| Home Now | A flow hub: the house in the middle with a spoke each to solar, the grid, the battery and the charger. Dots run along a spoke the way the power flows, faster with more of it. The battery and the car on the charger show their level as the border of their disc; the grid says importing, exporting or free power, the battery charging or discharging | |
| Battery Outlook | Today and tonight: 24 hours centred on the middle of today's daylight. The measured battery level up to now, then the planner's paths: expected, cautious (at the caution level, with the planned car) and cautious with the car off. Also the floor, the daylight and free hours as bands, and the lowest point ahead | Point at the chart for the values |
| Health | A tile per part, coloured by state: the planner's heartbeat, the inputs, the Solcast forecast's age, the car on the charger, Rivian schedule control (dry run, writes, faults; or what is missing: a controller set up but its car not added to the planner, or the old standalone integration still installed), the free hours (or why there are none: no schedule mapped, the helper missing or unreadable), a second planner entry if one exists, and the Tesla API spend, with how many are OK | |
| Settings | | Caution, battery floor and battery at free start (steps of 5%), the free hours (every day the same; edits their Schedule helper, admin only), the night charging start, and which car is on the charger |

Right-click a value, or press and hold it on a touch screen, to open its entity in Home Assistant. The remaining settings (largest change, prices, the Tesla guard, each car's plan) stay on the devices' pages.

In shadow mode, compare the planned car area with the "Car charging" line: the gap is what the planner would have changed. The card gets a fresh snapshot after every planning cycle and every change of a setting (the `solar_ev_planner/subscribe` websocket command), and reads today's history from the recorder every 5 minutes.

## A starting dashboard

Built-in cards only, if you'd rather not use the card above; add it as a new dashboard (raw configuration editor) and adjust. It has not been tried in a real Home Assistant installation yet.

```yaml
title: SolarSage
views:
  - title: Charging
    cards:
      - type: markdown
        title: Planner
        content: >-
          **{{ states('sensor.solarsage_mode') }}**:
          {{ state_attr('sensor.solarsage_reason', 'full_reason') }}
      - type: entities
        title: Now
        entities:
          - sensor.solarsage_car_on_charger
          - sensor.solarsage_next_change
          - sensor.solarsage_battery_minimum
          - sensor.solarsage_battery_minimum_time
          - sensor.solarsage_forecast_correction
          - sensor.sol_ark_inverter_battery_state_of_charge
          - sensor.tesla_wall_connector_total_power
          - sensor.solarsage_heartbeat
      - type: entities
        title: R1S
        entities:
          - sensor.r1s_planner_planned_current
          - sensor.r1s_planner_charging_until
          - sensor.r1s_planner_outlook
          - sensor.r1s_battery_state_of_charge
          - number.r1s_planner_charge_limit
          - number.r1s_planner_plan_target
          - time.r1s_planner_plan_time
          - select.r1s_planner_plan_days
          - sensor.r1s_schedule_control_control_state
      - type: entities
        title: Tesla
        entities:
          - sensor.tesla_planner_planned_current
          - sensor.tesla_planner_outlook
          - sensor.tesla_battery_level
          - number.tesla_planner_charge_limit
          - number.tesla_planner_plan_target
          - time.tesla_planner_plan_time
          - select.tesla_planner_plan_days
      - type: history-graph
        title: Today
        hours_to_show: 24
        entities:
          - sensor.sol_ark_inverter_battery_state_of_charge
          - sensor.solarsage_battery_minimum
          - sensor.r1s_planner_planned_current
          - sensor.tesla_planner_planned_current
          - sensor.garage_tesla_wall_connector_vehicle_current
      - type: entities
        title: Settings
        entities:
          - switch.solarsage_planning
          - switch.solarsage_shadow_mode
          - switch.solarsage_daytime_solar_charging
          - select.solarsage_caution
          - number.solarsage_battery_floor
          - number.solarsage_battery_wanted_at_free_hours
          - number.solarsage_minimum_time_between_changes
          - number.solarsage_largest_change
          - number.solarsage_paid_grid_tolerance
          - number.solarsage_import_price
          - number.solarsage_solar_buyback
          - select.solarsage_charger_car_override
          - schedule.free_hours
```

## Diagnostics and logs

- **Diagnostics:** the planner's entry → ⋮ → Download diagnostics. It holds the mapping, settings, readings and the last decision; coordinates, VINs, emails and tokens are redacted.
- **Debug log:**

  ```yaml
  logger:
    logs:
      custom_components.solar_ev_planner: debug
  ```

## Known limits

- **Not run against the real system yet.** Real Tesla Fleet timing (its charge switch reads on only while the car charges), real Solcast attribute types and how often the Sol-Ark reports unchanged values are unchecked until [TESTING.md](TESTING.md) phase 1 and 3.
- **The caution level is optimistic.** In simulation (run with a 10 PM–6 AM night charging window and a battery that held its level in free hours) the 90% battery path held 76% of the time; the closed loop compensated. Shadow-mode data should recalibrate it ([PLANNER_DESIGN.md](PLANNER_DESIGN.md#11-known-limits)).
- **A pause is not capped.** The hourly cap never holds back a pause, because a pause is the safety stop. It cannot loop: once it succeeds the switch reads off, and failures go through the back-off and, for the Tesla, the stall.
- **The Wall Connector power sensor is required.** While it is unavailable the planner does not plan, and the heartbeat goes down.
- **The spending guard counts calendar months.** Tesla's billing cycle may start on another day; the $1 margin and the 70% step leave room for that, and for Tesla billing a call the guard does not count. It never sees calls the planner did not cause.
- **Only one planner and one charger.** Car ids other than `rivian` and `tesla` show their raw id in the override and car-on-charger states.
