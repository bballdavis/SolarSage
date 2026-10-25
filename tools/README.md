# Real-data test tools

Tools for testing the planner with your real Home Assistant data and connections. They run on any computer that can reach Home Assistant, not inside it.

| Tool | What it does | Writes to Home Assistant? |
| --- | --- | --- |
| [`check.py`](#checkpy-is-everything-ready) | Readiness check of every planner input ([plan phase 1](../docs/PLAN.md#phases)) | No |
| [`snapshot.py`](#snapshotpy-real-days-as-a-file) | Saves a date range of real data as a sanitized JSON file, and records Solcast forecasts as they are issued | No |
| [`replay.py`](#replaypy-what-would-the-planner-have-done) | Runs the planner over real days and draws what it would have done (CSV and HTML) | No |
| [`attended_test.py`](#attended_testpy-live-commands-at-the-car) | Sends real current commands while you stand at the car | **Yes**, each one confirmed |

**Safety.**

- Every tool except `attended_test.py` is read-only by construction: its client refuses service calls.
- The tools talk only to Home Assistant. They never contact Tesla, Rivian, Solcast or the Wall Connector directly.
- Your token stays in `tools/.env` or your environment. Snapshots, replays and logs go to `tools/out/`. Both paths are git-ignored, so never force-add them.
- Snapshots and logs drop coordinates, zone names, VINs, tokens, emails and user ids, and keep only `home` / `not_home` for a device tracker.

## Setup

1. **Python 3.14 with aiohttp.** Nothing else is needed (PyYAML only for a YAML config).

   ```sh
   python3.14 -m venv .venv && .venv/bin/pip install aiohttp
   ```

2. **A long-lived access token.** In Home Assistant, open your profile (your name, bottom left), then the **Security** tab. Under **Long-lived access tokens**, choose **Create token** and copy it; it is shown only once.
3. **Connection.** Copy `tools/.env.example` to `tools/.env` and fill in `HASS_URL` (for example `http://homeassistant.local:8123`) and `HASS_TOKEN`. Exporting `HASS_URL` and `HASS_TOKEN` in your shell also works, and the environment wins over the file. For HTTPS with your own certificate authority, set `HASS_CA_FILE` to its CA file.
4. **The entity mapping.** [`config.example.json`](config.example.json) already holds your entity ids, so the tools work without a config file. To change something, create `tools/config.json` (git-ignored) with only the keys that differ; it is merged over the example. Use `--config FILE` for another file. YAML works too if PyYAML is installed.

   | Section | What it holds |
   | --- | --- |
   | `site` | The Sol-Ark, Wall Connector, Solcast and free-hours entities |
   | `units` | The unit of each power sensor (`W` or `kW`). The Sol-Ark powers are W and the Wall Connector's total power is kW. |
   | `cars` | Per car: `kind` (`tesla` or `rivian`), `name`, and its entities. The Tesla's `legacy_*` keys are today's tesla_custom names; the check tells you if those are all it finds. The Rivian also maps the entities of [Rivian schedule control](../docs/RIVIAN_SCHEDULE_CONTROL.md) (the control state, charger status, lease renewal switch and buttons) and the car's own charger state sensor, `sensor.r1s_charger_state`, which the schedule control's `charger_state_entity` option should name. `charging_wanted` is the planner's sensor for the car. |
   | `planner` | The planner integration's heartbeat, Planning and Shadow mode switches, and its mode, reason and car-on-charger sensors. Until the planner is installed the check only notes that it is missing. |
   | `tariff` | `price`, `buyback`, and `free_hours_fallback`, used when no Schedule helper is readable |
   | `battery` | Home battery limits for replays (capacity, 9 kW each way, 92% efficiency, 10% reserve) |
   | `settings` | Planner settings for replays, for example `{"caution": "balanced"}` (names as in the planner's `Settings`) |
   | `plans` | Car plans for replays, for example `{"tesla": [{"target_pct": 80, "at": "07:30", "weekdays": [0, 1, 2, 3, 4]}]}` |
   | `freshness` | Thresholds of the check: how long the Sol-Ark may stay quiet, and how old a Solcast poll may be |

5. **The planner core** (only `replay.py` needs it) is found at `custom_components/solar_ev_planner/core/`. To use another copy, set `PLANNER_CORE_PATH` to its `planner_core` folder.

Run the tools from the repository root, as modules (`python -m tools.check`) or as scripts (`python tools/check.py`).

## check.py: is everything ready?

```sh
python -m tools.check            # table; exit 1 on any FAIL, 2 if it cannot connect
python -m tools.check --strict   # exit 1 on WARN too
python -m tools.check --json     # the rows as JSON
```

It checks:

- **Every mapped input** exists, is available, has the expected unit (W, kW, %, kWh, A) and a plausible value.
- **Signs.** From the last 24 hours of 5-minute statistics, it computes the house load (`pv + grid − battery − wall connector`) for each sign convention. Grid must be + import / − export and battery + charging. A reversed sign shows as a FAIL that names which one. The battery's power is also compared with its level going up and down.
- **Freshness, carefully.** An unchanged value keeps an old timestamp in Home Assistant, so no value is called stale for its age; the age is shown for information only. A value is stale only when it is unavailable or unknown, or when its integration has gone quiet: the Sol-Ark's powers never sit still for 15 minutes.
- **Solcast.**
  - `detailedForecast` is present for today and tomorrow, with `pv_estimate10` and `pv_estimate90`.
  - The age of the last poll.
  - The p10/p50/p90 of the energy remaining today.
- **The free-hours Schedule helper** and its weekly blocks.
- **Tesla Fleet.** The car's entities exist and come from the `tesla_fleet` integration, along with the current number's range. When only the tesla_custom names exist, the check fails and says so.
- **Rivian schedule control.** Its entities exist. It shows the control state, the dry-run flag, the standing request (`desired_charge`, `requested_amps`, `lease_amps`) and why a start is blocked. A fault is a FAIL and handed-back is a WARN.
- **Which car the Wall Connector sees.** The Wall Connector sees a vehicle, and exactly one car reports its cable in while at home, as the planner decides it. Both cars or neither is a WARN. So is a Rivian on the charger while the schedule control's charger status says `a`.
- **The planner** ([docs/PLANNER.md](../docs/PLANNER.md)), once installed: its heartbeat (`shadow` is OK; unavailable is a WARN: Planning is off or it cannot plan), Planning, Shadow mode, and its mode, car on the charger and reason.
- **The schedule control's controller options,** as it reads them itself (the control state's `observation`): `controller_liveness_entity` must follow the planner's heartbeat and `controller_charging_entity` the car's charging-wanted sensor. Unmapped, or mapped to something else, is a WARN.

## snapshot.py: real days as a file

```sh
python -m tools.snapshot pull --days 3                          # the last 3 days up to now
python -m tools.snapshot pull --start 2026-10-18 --end 2026-10-24 --out tools/out/week.json
python -m tools.snapshot record-forecasts                       # keep it running
```

A snapshot is one JSON file that replays and tests can use offline. It holds:

- the current state and the history over the range of every mapped entity;
- the free-hours schedule;
- recorder statistics of the four powers and the battery level. Statistics start 14 days before the range (`--load-days`), so a replay can build the house-load profile for its first day: 5-minute rows where the recorder still has them (about 10 days), and hourly rows for the rest;
- the current Solcast forecast;
- the forecast issues recorded in that range.

**Solcast's half hours are not in Home Assistant's history.** The Solcast integration ([BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar), checked at version 4.6.3) puts `detailedForecast`, `detailedHourly` and `analysis` of its forecast-day sensors in `_unrecorded_attributes`.

- **What the recorder keeps:** each day's totals, which are the state plus the `estimate`, `estimate10` and `estimate90` attributes, and the remaining-today values.
- **The past:** it cannot be recovered.
- **From now on:** keep `record-forecasts` running. It checks `api_last_polled` every 10 minutes (`--every`). Each new issue, today's and tomorrow's half hours, is appended to `tools/out/forecast_issues.jsonl`, and `pull` merges the issues in its range. Run it on any always-on machine, for example with `nohup python -m tools.snapshot record-forecasts &` or a systemd user service. `--once` checks once and exits, for cron.

## replay.py: what would the planner have done?

```sh
python -m tools.replay tools/out/week.json                     # shadow replay, every 5 min
python -m tools.replay tools/out/week.json --whatif            # the car obeys the planner
python -m tools.replay tools/out/week.json --set caution=balanced --set floor_pct=25
python -m tools.replay --live --days 2                         # pull from HA, then replay
python -m tools.replay week.json --from 2026-10-20T06:00 --to 2026-10-20T21:00 --step 15
```

At each step it builds the planner's input from the recorded data, as the integration does from live states. It then calls the planner in shadow mode and records the decision:

- the mode and the command;
- the planner's current and its reason;
- any safety rule that acted;
- the battery level it predicted `--horizon` hours (3) ahead.

It writes `tools/out/replay_<first>_<last>.csv` (one row per step) and a self-contained `.html` page with no external files. The page has a summary table and, per day, three charts on one time axis:

- **Power (kW):** actual solar, the forecast in force (p10–p90 band and p50), house, grid and the car's actual draw;
- **Home battery (%):** actual, the expected and cautious levels predicted 3 hours earlier, the floor, and the what-if level;
- **Car current (A):** the planner's current, the Wall Connector's measured current, and a dot at each change.

Hover over a chart, or focus it and use the arrow keys, to see every value with the planner's reason. Below each chart are the changes with their reasons, and a table of every step.

**Shadow or what-if.**

- **Shadow (the default)** feeds the planner what really happened, including whatever current the car really drew, as shadow mode will in Home Assistant.
- **What-if (`--whatif`)** closes the loop: the car draws what the planner commands. A simple Sol-Ark model turns the real solar and house load into the battery level, grid power and paid import the planner would have caused, and the planner sees that world. In the model, the sun serves the house first, and the battery takes the surplus and covers the deficit within 9 kW and its reserve. In free hours the battery charges from the grid at its full rate (9 kW) until it is full and never discharges, as the Sol-Ark's own settings do, and the grid takes the rest.

**Where the forecast comes from,** per step (the CSV's `forecast_source` column):

| Source | When | How |
| --- | --- | --- |
| `recorded` | An issue recorded by `record-forecasts` covers the step | The half hours as issued |
| `current` | Steps after the snapshot's own last poll | The current `detailedForecast` |
| `totals` | No recorded issue: the usual case for past days | **Approximation:** the day's recorded totals in force then (p10, p50, p90), spread over the half hours with a clear-sky shape. The shape is the envelope of the actual solar of the days within a week either side, normalised. The day's level, spread and timing are right, but not when clouds come; Solcast does not know that either, and the planner corrects the rest of the day from actual production. |
| `actual` | No forecast history at all | The day's own actual total with p10/p90 at 0.75×/1.1×. This is hindsight: it flatters the planner, and the replay says so in its notes. |

The house-load profile is rebuilt each replayed day from the 14 days before it. Past days' solar against the forecast in force at 07:00 feeds the planner's multi-day forecast bias.

## attended_test.py: live commands at the car

```sh
python -m tools.attended_test tesla --i-am-at-the-car
python -m tools.attended_test rivian --i-am-at-the-car [--only inventory,t1,t2,t3,t4,t5,t6,t7]
```

This is the only tool that sends commands.

- **It refuses to start** without `--i-am-at-the-car` and an interactive terminal.
- **Every command is shown and confirmed:** `y` sends it, `n` skips it, `q` stops.
- **It refuses** unless the car is plugged in at home, the Wall Connector sees a vehicle and the other car is not also plugged in at home.
- **The planner must not be commanding:** it refuses while the planner's heartbeat shows `active` (shadow mode off), so that you are the only writer. Shadow mode on is right.
- **Everything is logged:** every reading, command and answer goes to `tools/out/attended_<car>_<time>.jsonl`.

**Tesla** (Tesla Fleet entities; Tesla bills each command):

1. If the car is not charging, it turns charging on.
2. It sets the charge current 16 → 10 → 6 → 16 A (`--amps` to change), clipped to the current number's range.
3. After each step it watches the Wall Connector's measured current reach the new value, within 1.5 A, and records how long that took (up to `--settle`, 120 s).
4. At the end, also after `q` or an error, it offers to restore the current and the charge switch as they were (Enter = yes).
5. It refuses at the charge limit unless you confirm.

**Rivian** (through [Rivian schedule control](../docs/RIVIAN_SCHEDULE_CONTROL.md#attended-trial); it never writes the Rivian itself):

- **It refuses** while the schedule control is in dry run (turn dry run off in its options first), faulted, or handed back, and while it reads its controller as down: with the schedule control mapped to the planner, the heartbeat must show `shadow`.
- **It guides the trial's steps,** watching `control_state`, `lease_amps` and the Wall Connector:
  - **Inventory:** Read schedules.
  - **T1 write and readback:** with the charge limit set below the car's level, 16 A, on, off, then Restore owner schedule; then unplug and replug.
  - **T2:** 16 → 10 → 20 A mid-session.
  - **T2b continuity:** 26 minutes over two lease renewals.
  - **T3:** stop.
  - **T4 lease expiry:** lease renewal off, then wait for the window to end with no write.
  - **T5:** resume.
  - **T6:** replug while parked, and "Charge now".
  - **T7 exact restore:** the schedule count matches the inventory.
- **It asks for what only you can see,** such as what the Rivian app shows, and logs your answers.
- **At the end** it offers to turn lease renewal back on, stop charging and restore your schedule.

## Tests

The tool tests live in `tests/tools/` and run with the repository's tests:

```sh
python -m pytest tests -q
```

They run against a fake Home Assistant on 127.0.0.1 (`tests/tools/fake_hass.py`), with a synthetic site and emulated cars. They never touch a real system: Home Assistant's test plugin blocks every other network destination. The replay tests skip when no planner core is found.

## Known limits

- **Past forecasts are approximated** until `record-forecasts` has run for a while (see [snapshot.py](#snapshotpy-real-days-as-a-file)).
- **The what-if battery model is simple:** fixed limits and efficiency, no inverter losses or tapering near full. Treat its costs as indicative.
- **Times are local wall-clock times,** as in the planner. On the night the clocks go back, the repeated hour is merged.
- **The check cannot tell a quiet Wall Connector from a dead one** when no car is connected: its values legitimately stay unchanged for hours. Only "unavailable" counts against it.
