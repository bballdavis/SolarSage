---
sidebar_label: Commissioning
---

# Testing with real data and real connections

This is the path from "the software passes its tests" to "the planner charges both cars unattended". Each stage maps to a phase of the [plan](PLAN.md#phases) and ends with a check that must pass before the next. Nothing in the repository has touched the real Home Assistant, cars, charger or Solcast yet; every stage below is done by the owner.

| Stage | Plan phase | What changes in the house | Commands sent |
| --- | --- | --- | --- |
| [0. Set up the tools](#0-set-up-the-tools) | 0 | Tesla Fleet set up and tesla_custom removed; the Rivian app prepared ([plan, phase 0](PLAN.md#phase-0-the-tesla-checklist)) | None |
| [1. Read everything for a week](#1-read-everything-for-a-week) | 1 | Upstream Rivian (schedule entities disabled), Rivian schedule control (dry run) and the free-hours schedule are added | None |
| [2. Replay real days](#2-replay-real-days) | 2 | Nothing | None |
| [3. Shadow mode for a week](#3-shadow-mode-for-a-week) | 3 | The planner is installed in shadow mode | None |
| [4. The Tesla under the planner](#4-the-tesla-under-the-planner) | 4 | Attended Tesla test, then shadow mode off with Rivian schedule control in dry run | Tesla only |
| [5. The attended Rivian trial](#5-the-attended-rivian-trial) | 5 | Rivian schedule control's dry run off, shadow mode on; the community pattern only if the trial fails | Rivian, by you |
| [6. The Rivian under the planner](#6-the-rivian-under-the-planner) | 6 | Shadow mode off with Rivian schedule control live | Both cars |

[Turning shadow mode off](#turning-shadow-mode-off) and [stopping quickly](#stopping-quickly) apply from stage 4 on.

## 0. Set up the tools

First do the plan's phase 0 checklists, for [the Tesla](PLAN.md#phase-0-the-tesla-checklist) and [the Rivian app](PLAN.md#phase-0-the-rivian-app), and make the [owner's decisions](PLAN.md#information-needed). Stage 1 needs Tesla Fleet working.

The [tools](TOOLS.md) run on any computer that reaches Home Assistant (a laptop, or an always-on machine for the forecast recorder), not inside Home Assistant.

1. Clone this repository and make a Python 3.14 environment with aiohttp:

   ```sh
   git clone https://github.com/bballdavis/SolarSage.git && cd SolarSage
   python3.14 -m venv .venv && .venv/bin/pip install aiohttp
   ```

   The default branch contains the current source.

2. In Home Assistant, create a long-lived access token: your profile → Security → Long-lived access tokens → Create token.
3. Copy `tools/.env.example` to `tools/.env` and fill in `HASS_URL` and `HASS_TOKEN`. The file is git-ignored; never commit it.
4. The entity mapping is already in `tools/config.example.json`. Put any entity ID that differs in `tools/config.json` (also git-ignored).

Run every tool from the repository root, for example `.venv/bin/python -m tools.check`. Snapshots, replays and logs go to `tools/out/` (git-ignored).

**Done when** `python -m tools.check` connects and prints its table. It may still FAIL on things stage 1 adds.

## 1. Read everything for a week

Plan phase 1: every planner input is present and fresh, with no control yet.

1. **Tesla Fleet, and no tesla_custom.** Add the HA core Tesla Fleet integration with your Tesla developer app, host its public key at home ([plan, key hosting](PLAN.md#phase-0-hosting-the-key-through-traefik)), and approve its virtual key in the car ([plan, the Tesla checklist](PLAN.md#phase-0-the-tesla-checklist)). From outside your network, `curl -sSI https://tesla-key.example.com/.well-known/appspecific/com.tesla.3p.public-key.pem` answers 200 and `https://tesla-key.example.com/` answers 404. In Tesla's developer dashboard, check there is no payment method and the billing limit is $0 ([plan, free credit](PLAN.md#phase-0-staying-inside-the-free-credit)). Leave Tesla Fleet's polling on for now, so this stage sees the car's data. Confirm its entities work, then remove the tesla_custom integration and its tesla-http-proxy add-on, and rename any `*_2` entities. The Tesla's entities should become `switch.tesla_charge`, `number.tesla_charge_current`, `number.tesla_charge_limit`, `sensor.tesla_battery_level`, `binary_sensor.tesla_charge_cable`, `device_tracker.tesla_location`, `sensor.tesla_charging` and `binary_sensor.tesla_status`; if they are named differently, put the real names in `tools/config.json`. Turn Sentry Mode off at home.
2. **The Rivian app and upstream Rivian.** Turn off Smart Charging and its schedule recommendation, keep one home schedule with no shared weekdays, and check its pin in the car ([plan, the Rivian app](PLAN.md#phase-0-the-rivian-app)). In upstream Rivian, disable its four schedule entities, and set the `rivian` logger to `error` ([guide, step 2](RIVIAN_SCHEDULE_CONTROL.md#install-and-configure)).
3. **Rivian schedule control, in dry run.** Install and configure it ([guide](RIVIAN_SCHEDULE_CONTROL.md#install-and-configure)). Map the plug, location and Wall Connector entities; leave the three controller options empty until stage 3. Keep **dry run on**. Press **Read schedules** to inventory the owner's list: one schedule, no shared weekdays, and no "Owner's Rivian schedules share weekdays" repair issue later.
4. **The free-hours Schedule helper.** Settings → Devices & services → Helpers → Create helper → Schedule, named "Free hours" (`schedule.free_hours`), painted 00:00–06:00 and 20:00–24:00 every day.
5. **Start recording forecasts.** Home Assistant's history does not keep Solcast's half hours, so start the recorder now on an always-on machine and leave it running:

   ```sh
   nohup .venv/bin/python -m tools.snapshot record-forecasts &
   ```

6. **Run the check** and fix every FAIL:

   ```sh
   python -m tools.check
   ```

   What it must show:
   - every Sol-Ark, Wall Connector, Solcast and car entity OK, with the expected units;
   - **signs (24 h)** OK: grid + import / − export, battery + charging. A FAIL names the reversed one; the planner's config flow then needs **Invert** for it;
   - **Solcast** `detailedForecast` with 48 half hours today and tomorrow, and a recent poll;
   - **free hours weekly** showing your blocks;
   - **the Tesla's platform** OK: Tesla Fleet (or Teslemetry or Tessie), for every mapped entity including the limit and location; a tesla_custom entity under those IDs is a FAIL, and tesla_custom's own names still present a WARN;
   - **the Rivian tracker** under either ID (`device_tracker.r1s_location` or `device_tracker.r1s`), with a WARN to map the one that exists;
   - **upstream Rivian's schedule entities** disabled (enabled is a WARN);
   - **rivian control state** `dry_run` (INFO);
   - **car on the charger** naming the right car each time you plug one in, and `none` when the charger is empty;
   - **planner** "not installed" (INFO) for now.

7. **Every day for a week**, run the check once with a car plugged in and once without. Plug each car in at least once.
8. At the end of the week, save it:

   ```sh
   python -m tools.snapshot pull --days 7
   ```

**Done when** a week of checks shows no FAIL, both cars were identified correctly on the charger, and the snapshot holds a week of data. Answer the open [information needed](PLAN.md#information-needed) items now. Also look at the Sol-Ark battery level in the week's history: it should be back at 100% by the end of each night's free hours, because the planner counts on that refill (PLAN.md decision 14). If it is not, fix that in the Sol-Ark's settings: the planner leaves charging the battery from the grid to the inverter.

## 2. Replay real days

Plan phase 2: the owner agrees with the behaviour. [PLANNER_DESIGN.md](PLANNER_DESIGN.md) describes it; its simulated days come from an earlier version. The replay shows it on yours.

```sh
python -m tools.replay tools/out/snapshot_<first>_<last>.json             # what it would have planned
python -m tools.replay tools/out/snapshot_<first>_<last>.json --whatif    # if the car had obeyed
python -m tools.replay tools/out/snapshot_<first>_<last>.json --set caution=balanced
```

Open the `.html` page it writes in `tools/out/`. For each day, look at:

- **Car current:** a smooth daytime current, a few changes a day, each with a reason that makes sense, and the car's full current from the start of the free hours (or from plug-in) until it reaches its limit;
- **Home battery:** in `--whatif`, it stays above the floor (30%) by day, nears the wanted level (30% by default) at the free start only on days a car took the battery's spare energy, and refills to 100% overnight;
- **Paid import** in the summary: near zero for the car on the free-nights tariff;
- **The forecast source** in the notes: `totals` days are an approximation until `record-forecasts` has run for a while (the [tools guide](TOOLS.md#replaypy-what-would-the-planner-have-done) explains).

**Done when** the replayed days look sensible to you. Try other settings with `--set` before you decide on them.

## 3. Shadow mode for a week

Plan phase 3: the planner runs in Home Assistant, plans and explains, and sends nothing.

1. **Install the planner** by copying its folder ([PLANNER.md, install](PLANNER.md#install)) and restart Home Assistant.
2. **Add it:** Settings → Devices & services → Add integration → SolarSage. Check the prefilled entities in the three steps.
3. **Add both cars** (Add car → Rivian, then Add car → Tesla) with the entities in [PLANNER.md, cars](PLANNER.md#cars).
4. **Check that it is in shadow mode:** `switch.solarsage_shadow_mode` on, and `sensor.solarsage_heartbeat` showing `shadow`.
5. **Point Rivian schedule control at the planner:** in its Configure, set controller liveness to `sensor.solarsage_heartbeat`, controller wants charging to `binary_sensor.r1s_planner_charging_wanted`, controller charging until to `sensor.r1s_planner_charging_until` and the vehicle override to `select.solarsage_charger_car_override`. Keep its dry run on. The until option gives the Rivian one charging window per free night once the planner commands it (stage 6).
6. **Add the dashboard** from [PLANNER.md](PLANNER.md#a-starting-dashboard), if you like.
7. **Run the check:** the planner rows show the heartbeat `shadow` and shadow mode on, and **rivian controller liveness** is OK. **rivian controller charging** stays INFO ("unknown") in shadow mode. `sensor.r1s_planner_charging_until` stays unknown in shadow mode too; the check has no row for it.
8. **For a week, every day:**
   - Look at the mode, the reason and the planned current of the car on the charger. In shadow mode the planned current is the planner's virtual car, not what the car draws.
   - Nothing is written to the Rivian this week: the planner sends nothing, and Rivian schedule control is in dry run. The one write per free night starts in [stage 6](#6-the-rivian-under-the-planner).
   - Each evening, replay the day to see it charted against what happened: `python -m tools.replay --live --days 1`.
   - Note the predicted battery when the free hours start (`at_free_hours_start` on `sensor.solarsage_battery_minimum`, recorded in its history) against the real level at 20:00. These pairs recalibrate the caution level later ([PLANNER_DESIGN.md, known limits](PLANNER_DESIGN.md#11-known-limits)).
   - If something looks wrong, download the planner's diagnostics (its entry → ⋮ → Download diagnostics; coordinates and identifiers are redacted).
9. **Try the settings:** change the caution, the floor or the battery wanted at free hours and watch the reason change at once.

**Done when** a week of shadow plans looks sensible to you: smooth currents, a few changes a day, the right car identified, and no `no_data` periods you cannot explain.

## 4. The Tesla under the planner

Plan phase 4.

### 4a. The attended Tesla test

With the Tesla plugged in at home and charging allowed, the Rivian not plugged in, and the planner still in shadow mode:

```sh
python -m tools.attended_test tesla --i-am-at-the-car
```

It confirms every command before sending it (Tesla bills each one). It sets 16 → 10 → 6 → 16 A, watches the Wall Connector follow each step, then offers to put the current and the charge switch back. It refuses while the planner's heartbeat shows `active`, so you are the only writer.

**Pass:** the Wall Connector reaches each current (within 1.5 A) and you know the latency, typically under 2 minutes. If it does not, stop here: the planner's Tesla control would not work either.

Also confirm, in Tesla Fleet's diagnostics or entities, that the car needs signed commands (`command_signing` required) and accepts 48 A (the current number's maximum).

### 4b. Shadow mode off, the Rivian kept out

1. Make sure **Rivian schedule control is in dry run**: the planner may then send the Rivian requests, but the schedule control writes nothing to Rivian. While plugged in, the Rivian charges from its own schedule. Its control state's `dry_run_target` shows what it would write: a short window by day, and in the free hours the one window to their end.
2. Run `python -m tools.check --strict` and fix any WARN.
3. **Turn Tesla Fleet's polling off:** Settings → Devices & services → Tesla Fleet → ⋮ → System options → "Enable polling for updates" off. The planner refreshes the Tesla itself from now on ([plan, free credit](PLAN.md#phase-0-staying-inside-the-free-credit)). Check Sentry Mode is off at home.
4. Turn shadow mode off ([what happens](#turning-shadow-mode-off)).

### 4c. A week with the Tesla

Every day with the Tesla on the charger, look at:

- **Paid import:** the Energy dashboard's grid import during paid hours while the Tesla charges should be near zero; `python -m tools.replay --live --days 1` shows grid power with the car's current.
- **Changes:** a few a day in the history of `number.tesla_charge_current`.
- **Each change confirmed:** the Wall Connector current follows the planned current (`sensor.tesla_planner_planned_current`, attributes `car_current` and `waiting`).
- **Plans met**, if you set one (`sensor.tesla_planner_outlook`).
- **Tesla's developer dashboard:** check it weekly. The billed commands, data requests and wakes should stay well inside the $10 monthly credit, still with no payment method and a $0 limit. Compare it with `sensor.solarsage_tesla_api_spend`: the dashboard also counts the Tesla app, other automations and Tesla Fleet's own polling, so it reads higher. If Tesla emails about 80% of the limit, or the sensor's `level` leaves `normal` early in the month, find the extra calls (Sentry Mode on at home, polling back on, a Wake button in an automation).
- **The spending guard** ([PLANNER.md](PLANNER.md#the-tesla-spending-guard)), once: Tesla Fleet's polling is off (its System options), the spend sensor's `tesla_fleet_polling` attribute shows `off` for the Tesla and its `cap` is $5, `refreshes` grows about once an hour while the Tesla is plugged in and not at all while it is away, and the count carries over a restart.
- **The Tesla guard** ([PLANNER.md](PLANNER.md#the-tesla-guard)), once: with the Tesla plugged in at its charge limit, the planner sends no repeated "on" and causes no wakes; the car's planned-current sensor says why a command waits. Restart Home Assistant once and check that the hourly command count carries over (the planner's diagnostics).
- **At unplug,** the Tesla's current goes back to 48 A (the restore current).

**Done when** a week shows no unexpected paid import, a few changes a day, plans met, and the Wall Connector confirming each change.

## 5. The attended Rivian trial

Plan phase 5: does the Gen 2 Rivian follow schedule changes? The trial steps are in the [Rivian schedule control guide](RIVIAN_SCHEDULE_CONTROL.md#attended-trial); the tool walks you through them. **The trial is the gate:** every step, T0 to T8, must pass before stage 6.

Before you start: Smart Charging is still off in the Rivian app, upstream's schedule entities are still disabled, and you can be at the car one night from 23:30 to 00:15 for T8.

1. **Turn shadow mode on** (the tool refuses while the planner commands). The Tesla keeps its last current, so unplug it.
2. **Turn Rivian schedule control's dry run off** (its Configure).
3. Plug the Rivian in at home. The check should show **car on the charger** `rivian`, **rivian controller liveness** OK and the heartbeat `shadow`.
4. Run the trial:

   ```sh
   python -m tools.attended_test rivian --i-am-at-the-car
   ```

   It refuses while the schedule control is in dry run, faulted or handed back, or while the schedule control sees its controller as down. It asks before every command, and asks you for what only you can see, such as the Rivian app's schedule screen. It records the car's software version first, and runs inventory, T1, T0, T2, T2b, T3, T4, T5, T6, T7 and T8 in that order.

5. **Run T8 on its own** between 23:30 and 23:59, if the first run was at another time (it fails as not run then):

   ```sh
   python -m tools.attended_test rivian --i-am-at-the-car --only t8
   ```

6. **Watch the pace and the location** in the control state's attributes: `reads_last_hour` and `writes_last_hour` stay at about 60 or less, `window_location_source` is `vehicle`, and `window_location_distance_m` is under 50.

In shadow mode the planner's charging-until sensor stays unknown, so every trial write is a short lease; the one window per free night is checked in [stage 6](#6-the-rivian-under-the-planner).

**Pass:** every step from T0 to T8 passes with acceptable latency, and **Read schedules** at the end matches the inventory by fingerprint.

**If T0, T3, T4 or T5 fail** (the car does not start on a short window, or does not stop when one ends):

1. Restore the owner's schedule, then turn on **Community pattern** in Rivian schedule control's Configure ([how it works](RIVIAN_SCHEDULE_CONTROL.md#community-pattern-fallback-mode)).
2. Repeat the whole trial, T0 to T8.
3. If it passes, continue with stage 6 in that mode. Its trade-off: if Home Assistant goes down, the car charges on at the last current until Home Assistant is back and parks the window.
4. If it fails too, the Rivian charges in the free hours only ([plan, fallbacks](PLAN.md#if-the-rivian-trial-fails)).

Re-run T0 and T2 (`--only t0,t2`) after every Rivian software update.

## 6. The Rivian under the planner

Plan phase 6.

1. Rivian schedule control's dry run stays **off**, and its controller options point at the planner (stage 3).
2. Run `python -m tools.check --strict` and fix any WARN.
3. Turn shadow mode off. The planner now commands whichever car is on the charger.
4. **Once, by day, while the Rivian charges under the planner,** check the stop path: turn Planning off. About 3 minutes later `sensor.r1s_schedule_control_control_state` turns `paused`, and the Wall Connector current drops to 0 a minute or two after that. Turn Planning on again, and charging resumes at the planned current. In the free hours Planning off does not stop the Rivian: its window runs on to their end.
5. **The first free night,** check the one write. With the Rivian on the charger when the free hours begin (or plugged in during them):
   - `sensor.r1s_planner_charging_until` shows the end of the free hours (06:00);
   - the control state is `lease_active`, with `lease_fixed` true, `lease_amps` 48 and `lease_expires_at` at 06:00;
   - the Rivian app shows one schedule window ending at 06:00, and the Wall Connector shows about 48 A, also past midnight;
   - `lease_written_at` and `last_write_at` stay at that first write all night, and are still unchanged after 06:00, when the window has ended on the car by itself and the control state shows `paused`. The next write is the planner's first command by day. On the night the clocks change, one more write moves the window to the new local time, with the same end.
6. **Once,** unplug the Rivian and press **Read schedules** a few minutes later: the owner's schedule is back exactly.
7. For a week, the same checks as stage 4c, with the Rivian's `lease_amps` (control state attribute) and the Wall Connector following the planned current, and one schedule write per free night.
8. **Every week from now on,** look at Rivian schedule control's faults (`restore_rejected`, `external_edit`) and its `reads_last_hour` and `writes_last_hour`.

**Done when** a week passes the stage 4 checks and the owner's schedule is restored at every unplug. Then plan phase 7, unattended running, starts.

## Turning shadow mode off

Before you do it:

- `python -m tools.check --strict` passes;
- the car you want commanded is plugged in, or you are happy for whichever car is plugged in to be commanded;
- Rivian schedule control is in dry run (stage 4) or has passed the trial (stage 6).

Then turn `switch.solarsage_shadow_mode` off. The heartbeat changes to `active`, and the planner sends its first decision to the car on the charger at once. From then on it sends only changes, at most one per car per minute and a few an hour.

**Going back to shadow mode** sends nothing: each car keeps its last command, and Rivian schedule control keeps renewing it while the heartbeat is up. In the free hours the Rivian's window runs to their end, and then short leases continue the same request into paid hours. Stop the car as well if you want it to stop (below).

## Stopping quickly

| To stop | Do this | What happens |
| --- | --- | --- |
| The Rivian | Rivian schedule control's **Restore owner schedule** button | The owner's schedule is written back and the schedule control ignores the planner until the next unplug |
| The Rivian | Turn Planning off | By day, Rivian schedule control stops renewing at once and parks the window after 3 minutes. In the free hours it does not: the night's window runs on to their end. |
| The Tesla | Turn shadow mode on (or Planning off), then stop charging in the Tesla app | The planner sends nothing more; the Tesla keeps what the app set |
| Everything | Turn Planning off, and unplug | No commands; the Rivian's own schedule returns after unplug |

If Home Assistant itself is down, the Rivian's charging window lapses on its own within about 25 minutes by day, and at the end of the free hours at night. In community-pattern mode it does not: the car charges on at the last current until you unplug it. The Tesla keeps its last current up to its own limit.

## The software tests

The repository's own tests (Rivian schedule control, planner and tools, against fakes only) are run before any of this; the commands are in the [README](https://github.com/bballdavis/SolarSage/blob/main/README.md#checks).
