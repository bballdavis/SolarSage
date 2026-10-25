# Solar EV planner

**What it is.** The Solar EV planner (`custom_components/solar_ev_planner`) is the Home Assistant integration that decides each car's charging current. It plans from the Solcast forecast, the house's recent use and the home battery's predicted level, and commands the car on the Wall Connector: the Tesla through HA core Tesla Fleet, the Rivian through this repository's [charge adapter](RIVIAN_CHARGE_ADAPTER.md). How it decides, and how an earlier version did in simulation, is in [PLANNER_DESIGN.md](PLANNER_DESIGN.md). Testing it with real data, step by step, is in [TESTING.md](TESTING.md).

**Status: software-tested only.** Its tests run against Home Assistant 2026.10.3, with made-up states, a real recorder and Schedule helper, and the real Rivian adapter over a simulated Rivian account. It has not been run against the owner's Home Assistant. **It starts in shadow mode: it plans and explains, but sends nothing until you turn shadow mode off.**

## What it does

- **Every minute** it reads the Sol-Ark, the Wall Connector, Solcast, the free-hours schedule and both cars. House use is `solar + grid − battery − Wall Connector` (there is no house sensor).
- **Every 15 minutes** (and at once on a plug-in, a new forecast or a mode change) it plans one steady current for the car on the charger: excess solar first, then the home battery within the floor rule (with free hours ahead, what it would still hold above the wanted level when they begin), then free hours, and paid grid power only when a plan cannot be met otherwise.
- **In free hours** the car on the charger charges at its full current (the car's maximum, capped by the Wall Connector) from the moment they begin until its charge limit. The planner counts on the Sol-Ark's own settings to charge the home battery from the grid meanwhile, and predicts it charging to full.
- **Every minute** its safety rules check for paid import and the battery at the floor.
- **It commands only the car it sees on the charger**, and only what differs from what the car already holds. A pause is the charge switch, never 0 A.
- **It explains itself**: a mode, a one-sentence reason, the next planned change, the predicted battery path and each car's outlook.

## Install

### Both integrations come from this repository

HACS installs one integration per repository: the first folder under `custom_components/`, which here is the Rivian adapter. [`hacs.json`](../hacs.json) names the adapter for that reason. So:

| Integration | Install with |
| --- | --- |
| Rivian charge adapter (`rivian_solar_adapter`) | HACS (custom repository `https://github.com/bballdavis/HASS-Solar-Rivian`, category Integration), or the copy below |
| Solar EV planner (`solar_ev_planner`) | **The copy below.** HACS does not install it. |

**Copying an integration into Home Assistant OS.** Use a terminal add-on (Terminal & SSH, or Advanced SSH & Web Terminal):

```sh
cd /tmp
git clone --depth 1 https://github.com/bballdavis/HASS-Solar-Rivian.git
mkdir -p /config/custom_components
rm -rf /config/custom_components/solar_ev_planner
cp -r HASS-Solar-Rivian/custom_components/solar_ev_planner /config/custom_components/
# Only if the adapter is not installed through HACS:
# rm -rf /config/custom_components/rivian_solar_adapter
# cp -r HASS-Solar-Rivian/custom_components/rivian_solar_adapter /config/custom_components/
rm -rf HASS-Solar-Rivian
```

`git clone` takes the default branch (`main`). While the planner is only on another branch, add `--branch <that branch>`.

Without a terminal, download the repository as a ZIP from GitHub and copy the `custom_components/solar_ev_planner` folder into `config/custom_components/` through the Samba share add-on. Then restart Home Assistant.

**Updating** is the same copy followed by a restart. The planner's settings and memory are in Home Assistant's `.storage` (`solar_ev_planner.<entry id>`), not in the folder, so they survive an update. Removing the integration deletes them.

**Requirements:** Home Assistant 2026.10 or later, the recorder (on by default) and the Schedule helper (built in).

### Before adding it

1. **Solcast** ([BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar)) with its forecast-today and forecast-tomorrow sensors. Their `detailedForecast` attribute gives the half hours with `pv_estimate10`, `pv_estimate` and `pv_estimate90`.
2. **A Schedule helper for the free hours.** Settings → Devices & services → Helpers → Create helper → Schedule. Name it "Free hours" (it becomes `schedule.free_hours`) and paint 00:00–06:00 and 20:00–24:00 on every day. Repaint it when the electricity plan changes. Leave it out for a flat or time-of-use plan.
3. **Tesla Fleet** set up for the Tesla, with the virtual key approved in the car. Until then the planner can map today's tesla_custom entities instead.
4. **The Rivian adapter** installed and configured ([guide](RIVIAN_CHARGE_ADAPTER.md#install-and-configure)). It can stay in dry run until the attended trial.

## Configure

Settings → Devices & services → Add integration → **Solar EV Planner**. There is one planner per Home Assistant.

### The site: three steps

The form is prefilled with the owner's entity IDs wherever they exist. Change any of them later with **Configure** on the planner's entry; saving reloads it and keeps its memory.

| Step | Field | Owner's entity or default |
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

| Field | Rivian (R1S) | Tesla (Roxxo, Tesla Fleet) |
| --- | --- | --- |
| Name | `R1S` | `Roxxo` |
| Charge switch | `switch.r1s_solar_adapter_solar_charging` (the adapter) | `switch.roxxo_charge` (tesla_custom: `switch.roxxo_charger`) |
| Charge current | `number.r1s_solar_adapter_solar_current` (the adapter) | `number.roxxo_charge_current` (tesla_custom: `number.roxxo_charging_amps`) |
| Battery level | `sensor.r1s_battery_state_of_charge` | `sensor.roxxo_battery_level` |
| Charge limit | `sensor.r1s_battery_state_of_charge_limit` | `number.roxxo_charge_limit` |
| Charge cable connected | `binary_sensor.r1s_charger_connection` | `binary_sensor.roxxo_charge_cable` |
| Location | `device_tracker.r1s_location` | `device_tracker.roxxo_location` |
| Adapter control state | `sensor.r1s_solar_adapter_control_state` (recommended: it tells a pause from no request) | — |
| Minimum, maximum, step | 8 A, 48 A, 2 A | 6 A, 48 A, 1 A |
| Usable pack energy | 135 kWh | 75 kWh |

- **The name sets the car's entity IDs:** R1S gives `…r1s_planner_…`, Roxxo gives `…roxxo_planner_…`.
- **The car's own current number caps the maximum:** Tesla Fleet's says what the car accepts on this charger.
- **Car ids** are `rivian` and `tesla` (a second car of a kind becomes `rivian_2`). The charger car override uses them, and the Rivian adapter's vehicle override treats `rivian` as its own car.
- **Never map the planner's own entities** as a car's switch or current; the form refuses them.

### Electricity plans

The free hours are the main entry's Schedule helper, at $0. In them, by night or by day, the car on the charger charges at its full current to its limit. Every other price level is a **price window**: on the planner's entry choose **Add price window**, pick a Schedule helper painted for that level, a name and a price (and optionally an export price). The price is then a number entity, `number.solar_ev_planner_<name>_price`, on its own device. The free hours apply first, then the price windows in the order they were added; outside all of them the planner's **Import price** applies, and the **Solar buyback** for export.

| Plan | Set up |
| --- | --- |
| Free nights (today: 8 PM–6 AM free, $0.32/kWh otherwise, $0.00 buyback) | Free hours schedule painted 20:00–06:00; Import price 0.32; Solar buyback 0 |
| Time of use | No free hours. A price window per level (for example "Peak" and "Off peak"); Import price for the rest |
| Flat | No free hours, no price windows; Import price |
| Free weekends | Free hours schedule painted over the weekend (and any free nights) |

## Pointing the Rivian adapter at the planner

The adapter parks the Rivian if the planner stops, and settles its requests after a restart, through two entities the planner provides. In the adapter's **Configure**:

| Adapter option | Set it to | What the planner's entity does |
| --- | --- | --- |
| Controller liveness (`controller_liveness_entity`) | `sensor.solar_ev_planner_heartbeat` | `shadow` or `active` while the planner is on and has decided within the last 3 minutes; unavailable while Planning is off, while it cannot plan (missing site readings, an unreadable schedule, an error) or once it has stopped deciding. It re-checks itself every 30 seconds. |
| Controller wants charging (`controller_charging_entity`) | `binary_sensor.r1s_planner_charging_wanted` | On while the last command the car accepted was a current, off while it was a pause; unknown in shadow mode, while Planning is off, and before the first command of a plug-in. It survives restarts. |
| Wall Connector vehicle override (`vehicle_override_entity`) | `select.solar_ev_planner_charger_car_override` | `auto`, `rivian` or `tesla`; the adapter treats `auto` and `rivian` as no override |
| Wall Connector vehicle connected (`wall_connector_entity`) | `binary_sensor.tesla_wall_connector_vehicle_connected` | The same sensor the planner reads |

- **Leave the increase margin and the ceiling hold at 0**, their defaults; the planner asks for exact currents and paces itself.
- **The heartbeat stays up in shadow mode,** so mapping it does not block the adapter's attended trial.
- **To check the mapping,** run `python -m tools.check` ([TESTING.md](TESTING.md)): the rows "rivian controller liveness" and "rivian controller charging" read what the adapter itself sees.

What the planner sends the adapter, and the adapter's rules for requests, are in [Driving the adapter from the planner](RIVIAN_CHARGE_ADAPTER.md#driving-the-adapter-from-the-planner).

## Entities

### The planner (device "Solar EV planner")

| Entity | What it shows |
| --- | --- |
| `sensor.solar_ev_planner_mode` | `day` (solar charging), `night` (shown as "Free hours": the car charges at its full current), `full` (car at its limit), `stale` (car data stale), `empty` (charger empty), `off`, or `no_data` (cannot plan). Attributes: shadow, commanding, the car, the safety rule that acted. |
| `sensor.solar_ev_planner_reason` | The decision in one sentence; `full_reason` holds all of it |
| `sensor.solar_ev_planner_next_change` | When the current is expected to change next; attributes: the current and why |
| `sensor.solar_ev_planner_battery_minimum` | The home battery's predicted lowest level (at the caution level) until tomorrow's free hours (tomorrow's sunset when there are none). Attributes: when, the expected-day and no-car minimums, the floor, the level when free hours start, and the half-hourly path (not recorded). |
| `sensor.solar_ev_planner_battery_minimum_time` | When that minimum is reached |
| `sensor.solar_ev_planner_forecast_correction` | The factor today's production so far puts on the rest of today's forecast; attributes: the evidence weight, the multi-day bias, the forecast's age, and p10/p50/p90 left today |
| `sensor.solar_ev_planner_car_on_charger` | `none`, `rivian` or `tesla`, with why, the candidates, the Wall Connector readings and `tonight_car`: the car that should be on the charger in the coming free hours |
| `sensor.solar_ev_planner_heartbeat` | The adapter's liveness entity (above) |

### Each car (device "R1S planner", "Roxxo planner")

| Entity | What it shows or sets |
| --- | --- |
| `binary_sensor.r1s_planner_charging_wanted` | The adapter's controller charging entity (above) |
| `sensor.r1s_planner_planned_current` | The current the plan has the car at now (in shadow mode, the virtual car's); 0 off the charger. Attributes: the wanted and commanded current, what the car or adapter holds, the adapter state, why a command waits, the last error. |
| `sensor.r1s_planner_outlook` | Where the car's energy will come from, in words ("covered by sun", "about 7 kWh in free hours", "about 3 kWh paid") |
| `sensor.r1s_planner_energy_from_sun`, `_energy_in_free_hours`, `_energy_paid` | The same in kWh |
| `number.r1s_planner_charge_limit` | The limit the planner charges to; 0 uses the car's own (default) |
| `number.r1s_planner_plan_target`, `time.r1s_planner_plan_time`, `select.r1s_planner_plan_days` | A plan: "target % by time", on `off` (default), `once`, `weekdays`, `daily` or `weekends`. `once` is for the next such time and turns itself off afterwards. |
| `number.roxxo_planner_restore_current_at_unplug` | Tesla only: the current set back when the Tesla is unplugged at home after the planner changed it (default 48 A; 0 turns it off). The Rivian adapter restores the owner's schedule itself. |

## Settings

All settings are entities on the planner's device. They are kept in the planner's store and survive restarts and updates. Every change re-plans at once.

| Entity | Default | Range | Meaning |
| --- | --- | --- | --- |
| `switch.solar_ev_planner_planning` | on | | Off: no plans and nothing sent; see [Planning off and shadow mode](#planning-off-and-shadow-mode) |
| `switch.solar_ev_planner_shadow_mode` | **on** | | Plan and explain, send nothing |
| `switch.solar_ev_planner_daytime_solar_charging` | on | | Off: a car plugged in by day waits for the free hours |
| `select.solar_ev_planner_caution` | conservative | near-certain, conservative, balanced, typical day | How much of the forecast to count on (97%, 90%, 75%, 50%) |
| `number.solar_ev_planner_battery_floor` | 30% | 5–95% | The predicted battery must stay above it |
| `number.solar_ev_planner_battery_wanted_at_free_hours` | 30% | 0–100% | What the battery should still hold when free hours start (at sunset without free hours). With free hours ahead, the car may use the battery down to it by day, since the Sol-Ark refills it from the grid in them; raise it to keep more in the battery |
| `number.solar_ev_planner_minimum_time_between_changes` | 15 min | 1–120 min | |
| `number.solar_ev_planner_largest_change` | 6 A | 1–48 A | |
| `number.solar_ev_planner_paid_grid_tolerance` | 5 min | 0–60 min | How long paid import is accepted before the safety rule cuts the car |
| `number.solar_ev_planner_import_price` | 0.32 | 0–5 per kWh | Price outside the free hours and price windows |
| `number.solar_ev_planner_solar_buyback` | 0.00 | 0–5 per kWh | What exported solar earns |
| `select.solar_ev_planner_charger_car_override` | auto | auto, rivian, tesla | Names the car on the charger when both report being plugged in at home |

## Shadow mode and commanding

### Shadow mode (the default)

- The planner plans every cycle **as if it had been obeyed**: a virtual car follows its plan, and the dashboard shows that plan.
- **It calls no service.** The only call it makes is reading the Schedule helpers' weekly configuration (`schedule.get_schedule`), which changes nothing.
- The heartbeat shows `shadow`, and charging-wanted stays unknown, so the Rivian adapter neither starts nor drops anything because of the planner.
- The cars charge as they do today, from their own schedules or apps. The planner's reasons describe what it would do; while a car draws on its own, the measured import and battery differ from the plan.

### Turning shadow mode off

Turn `switch.solar_ev_planner_shadow_mode` off. From the next cycle:

1. The planner starts a fresh session and sends its first decision at once to the car on the charger, and only to that car.
2. **Tesla:** the charge current, then the charge switch. Every command is billed by Tesla's API, so a command is never repeated on the planner's own initiative.
3. **Rivian:** the adapter's current, then its switch. If the adapter drops the request (after a restart, an outage or a replug), the planner sends it again, except while the adapter is handed back, faulted or waiting for the vehicle. With the adapter still in dry run, the requests are recorded but nothing is written to Rivian.
4. **Limits:** the planner's own (15 minutes between changes, 6 A at a time; in free hours the car goes straight to its full current), and on top at most one command per car per minute and 12 an hour (a pause is never held back), with a back-off after a failure.
5. **At unplug** the Tesla's current is set back to its restore current, if the planner changed it during that plug-in.

### Planning off and shadow mode

| | Planning on, shadow mode on | Planning on, shadow mode off | Planning off |
| --- | --- | --- | --- |
| Commands | none | to the car on the charger | none |
| Heartbeat | `shadow` | `active` | unavailable |
| Charging wanted | unknown | on or off | unknown |
| Tesla | as you left it | follows the plan | keeps its last current, up to its own limit |
| Rivian (adapter mapped) | as you left it | follows the plan | the adapter stops renewing at once and parks its window after 3 minutes, so charging stops; the owner's schedule comes back at unplug or with **Restore owner schedule** |

- **Going back to shadow mode** sends nothing, so each car keeps its last command: the Tesla its current, and the Rivian its request, which the adapter keeps renewing while the heartbeat is up. To stop a car as well, pause it first (in its app, or with the adapter's **Restore owner schedule** for the Rivian), or turn Planning off.
- **When the planner cannot plan** (mode `no_data`: a site reading missing, the free-hours schedule unreadable, an error), the heartbeat goes unavailable just as with Planning off. The reason sensor says why. A stale car (its own data unavailable) is not commanded, but the heartbeat stays up, so the Rivian keeps its last current until its data returns.

### Which car is on the charger

- The Wall Connector showing no vehicle rules out every car.
- Otherwise a car is a candidate while its own cable sensor is on and it is home (its tracker says `home`).
- The charger car override, when not `auto`, leaves only the car it names.
- Two candidates are ambiguous: the planner commands neither until the override names one.

### Freshness

An unchanged value keeps an old timestamp in Home Assistant (the battery level, the Wall Connector's power at 0), so no value is stale because of its age. The site counts as stale only when all of solar, grid, battery, Wall Connector power and the battery level have been silent for 30 minutes, or when an integration behind them is not loaded. A car is stale when its own values are unavailable or its integration is not loaded.

## A starting dashboard

Built-in cards only; add it as a new dashboard (raw configuration editor) and adjust. It has not been tried in the owner's Home Assistant yet.

```yaml
title: Solar EV
views:
  - title: Charging
    cards:
      - type: markdown
        title: Planner
        content: >-
          **{{ states('sensor.solar_ev_planner_mode') }}**:
          {{ state_attr('sensor.solar_ev_planner_reason', 'full_reason') }}
      - type: entities
        title: Now
        entities:
          - sensor.solar_ev_planner_car_on_charger
          - sensor.solar_ev_planner_next_change
          - sensor.solar_ev_planner_battery_minimum
          - sensor.solar_ev_planner_battery_minimum_time
          - sensor.solar_ev_planner_forecast_correction
          - sensor.sol_ark_inverter_battery_state_of_charge
          - sensor.tesla_wall_connector_total_power
          - sensor.solar_ev_planner_heartbeat
      - type: entities
        title: R1S
        entities:
          - sensor.r1s_planner_planned_current
          - sensor.r1s_planner_outlook
          - sensor.r1s_battery_state_of_charge
          - number.r1s_planner_charge_limit
          - number.r1s_planner_plan_target
          - time.r1s_planner_plan_time
          - select.r1s_planner_plan_days
          - sensor.r1s_solar_adapter_control_state
      - type: entities
        title: Roxxo
        entities:
          - sensor.roxxo_planner_planned_current
          - sensor.roxxo_planner_outlook
          - sensor.roxxo_battery_level
          - number.roxxo_planner_charge_limit
          - number.roxxo_planner_plan_target
          - time.roxxo_planner_plan_time
          - select.roxxo_planner_plan_days
      - type: history-graph
        title: Today
        hours_to_show: 24
        entities:
          - sensor.sol_ark_inverter_battery_state_of_charge
          - sensor.solar_ev_planner_battery_minimum
          - sensor.r1s_planner_planned_current
          - sensor.roxxo_planner_planned_current
          - sensor.garage_tesla_wall_connector_vehicle_current
      - type: entities
        title: Settings
        entities:
          - switch.solar_ev_planner_planning
          - switch.solar_ev_planner_shadow_mode
          - switch.solar_ev_planner_daytime_solar_charging
          - select.solar_ev_planner_caution
          - number.solar_ev_planner_battery_floor
          - number.solar_ev_planner_battery_wanted_at_free_hours
          - number.solar_ev_planner_minimum_time_between_changes
          - number.solar_ev_planner_largest_change
          - number.solar_ev_planner_paid_grid_tolerance
          - number.solar_ev_planner_import_price
          - number.solar_ev_planner_solar_buyback
          - select.solar_ev_planner_charger_car_override
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
- **The hourly command cap resets on restart.** The planner's own rate limits are saved; the executor's count of commands in the last hour is not.
- **The Wall Connector power sensor is required.** While it is unavailable the planner does not plan, and the heartbeat goes down.
- **Only one planner and one charger.** Car ids other than `rivian` and `tesla` show their raw id in the override and car-on-charger states.
