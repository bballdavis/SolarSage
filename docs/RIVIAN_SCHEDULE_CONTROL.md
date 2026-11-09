---
sidebar_label: Rivian control
---

# Rivian schedule control: setting the Rivian's charging from Home Assistant

**What it is.** Rivian schedule control is software, not a device: a Home Assistant integration in this repository (`custom_components/solar_ev_planner/rivian`). Nothing is added at the charger or the car. The Rivian plugs into the same Tesla Wall Connector as the Tesla, with its usual cable, and the Wall Connector is only read, never controlled.

No Rivian API command sets the charging current, on any generation, as of October 31, 2026. Start/stop and the charge limit need a phone key, which on a 2025 Gen 2 can be paired only from a Mac through an unofficial proof of concept ([upstream issue #181](https://github.com/bretterer/home-assistant-rivian/issues/181)); that is out of scope here and would still not set the current. Rivian schedule control does it the one way Rivian's API allows: it rewrites the car's **charging schedule**, the same schedules the Rivian app shows. The schedule's amperage is the current. In Home Assistant this appears as a start/stop switch and a current setting for the car. In the [working plan](PLAN.md) the solar planner uses them: it sets the switch and the current, and provides three entities that tell the schedule control whether the planner is running, whether it wants the car charging and, in the free hours, until when ([what the planner must provide](#driving-schedule-control-from-the-planner)). The planner itself is described in [PLANNER.md](PLANNER.md).

**Status: software-tested only.** Four rounds of independent adversarial review shaped the design, including closed-loop simulations against evcc's charging loop. Its tests run against Home Assistant 2026.10.3 with a simulated Rivian provider. It has not been run against a real account, vehicle, or Wall Connector. **It installs in dry-run mode, and nothing is written until you turn dry run off.**

## How it works

The table shows the default mode. The [community pattern](#community-pattern-fallback-mode) option replaces the daytime leases and parked windows.

| The controller asks for | Schedule control writes | Expected vehicle behavior (to be confirmed by the [trial](#attended-trial)) |
| --- | --- | --- |
| Charging on, at N A | A **lease**: one window from 5 minutes ago to `lease_minutes` + 5 minutes from now, at N A rounded down to an even 8–48 A. The 5-minute padding absorbs clock differences. | Charges at N A |
| Charging on, at N A, with an until time ahead (the end of the free hours, from the **controller charging until** entity) | A **fixed window**: one window from 5 minutes ago to exactly that time, at N A, written once. There is no padding at the end, so it never runs past that time into paid hours. A window lasts at most a day: a later time gets a day now and the rest when it ends. | Charges at N A until that time |
| A lower current | The same lease at the lower amperage, at the next allowed write. A fixed window keeps its end. | Current drops |
| A higher current | The same lease at N A rounded down to an even 8–48 A, once the increase interval has passed since the last current change. Two evcc-era rules are options, off by default: an **increase margin** writes increases that much below the request, and a **ceiling hold** keeps the margin at 48 A for a while after the current dropped from 48 to 44 or 46 A. evcc needed 3 A and 30 minutes, because it added up to 2 A to its requests and chased the surplus; the planner asks for exact currents and rate-limits itself. A fixed window keeps its end. | Current rises |
| Charging off | A **parked window** at 8 A that ended 5 minutes ago, so no window covers now. A live fixed window is parked too. | Waits |
| Nothing new | Renews the lease when 8 minutes remain, but **only** while the car is known plugged in at home and the controller is alive. A fixed window is never renewed. | Keeps charging |
| Nothing new, and the lease cannot be extended (the controller looks down, the car's state unknown, or renewal off) | Nothing: the window ends on the vehicle by itself, `lease_minutes` + 5 minutes after it was written. That is the cloud-free stop. A minute later a parked window replaces it, so it cannot recur a week later. | Stops at the window end |
| Nothing new, and a fixed window reaches its end | Nothing: the vehicle stops by itself, and the control state shows `paused`. While the until entity still gives that end, nothing starts again. Once it gives another time, or none, a standing request starts again: a fixed window to the new time, or a lease. An ended fixed window would recur a week later, so it is parked if nothing else has been written six days after it. | Stops at the window end |
| Nothing, and the controller has looked down for 3 minutes | Its request from before the outage is **held** and the window is parked. One unavailable reading is not enough. Until the liveness entity has been seen available since Home Assistant started, the wait is 10 minutes. Any request the controller makes meanwhile, changed or not, shows it is running and confirms its request. A fixed window is not parked: it runs to its end. | Stops (a fixed window at its end) |
| The controller is back after looking down | It may have restarted, and then it asks again if it wants charging. The **controller charging** entity tells: on means the controller still wants this car charging, so a held request resumes; off drops it. Without that entity, a held request is dropped. A shorter gap keeps the request. | Resumes, or waits for the controller |
| Nothing (Home Assistant itself down) | Nothing; the window ends on its own within `lease_minutes` + 5 minutes, a fixed window at its end | Stops at the window end |

**Where the window is.** A Rivian follows a schedule only near the schedule's location pin (about 300 ft). Every window schedule control writes uses one location, chosen when it takes over and kept in its journal:

1. the car's own position, from the mapped device tracker's latitude and longitude, if the car is plugged in at home;
2. otherwise the nearest owner schedule within 150 m of Home Assistant's home;
3. otherwise Home Assistant's home.

A location from 2 or 3 is replaced by the car's position once the tracker gives one while the car is plugged in at home. An invalid location is never written. If the car, plugged in at home, is more than 50 m from the window's location, a warning is logged once, with the distance only. The control state shows `window_location_source` (`vehicle`, `owner_schedule` or `home`) and `window_location_distance_m`; the coordinates themselves are never shown.

**Ownership rules:**

1. **Snapshot first.** The first command while the car is plugged in at home at the Wall Connector takes ownership. Before writing, the schedule control reads and persists the owner's **complete** schedule list.
   - It refuses if Rivian holds a window that the schedule control itself wrote earlier. That protects against restoring its own window as if it were the owner's.
   - It does not take over while the liveness entity shows the controller down, so the owner's schedule is never replaced only to be parked.
   - A command for an unplugged car is discarded, not saved for a later plug-in. A command while the car is plugged in but away is kept.
   - **Owner schedules that share weekdays,** disabled ones included, may be refused when written back ([upstream PR #308](https://github.com/bretterer/home-assistant-rivian/pull/308)). Schedule control logs a warning and raises the repair issue "Owner's Rivian schedules share weekdays" before its first write, which waits one tick (about 15 seconds) for it. The issue goes away when the overlap is gone or the entry unloads. The control state shows the days in `owner_schedules_overlap_days`.
2. **Persist, guard, then write.** Every write is recorded in the journal before anything is sent, and a failed save sends nothing. A fresh read then checks that Rivian still holds what the schedule control last verified, or one of its own unverified writes. If it holds anything else, such as an edit in the Rivian app, the schedule control **faults and writes nothing**.
3. **Readback decides.** A write counts only when a fresh read shows it.
   - **Settling:** a write that reads back unchanged is re-read 30 seconds later before it counts as not applied. In between nothing is read, so a write costs at most 3 reads: the guard read, one right after sending and one once settled.
   - **Pace:** the control state shows `reads_last_hour` and `writes_last_hour`. Rivian publishes no limits, and rate-limited another project at about 2 reads a minute; keep to about 1 a minute or less.
   - **Never counted:** writes Rivian definitely refused (rate limit, expired login) or never received are withdrawn without counting.
   - **Late landings:** unverified writes are remembered, so one that lands late is recognized as the schedule control's own.
   - **Restore while writes are outstanding:** it is still sent and confirmed only after a settled re-read, and the owner's list is also archived in case an earlier write lands afterwards.
4. **Exact restore.**
   - **When it happens:** the owner's original list is written back on unplug (after `release_delay_s`), on away (after 10 minutes), on the **Restore owner schedule** button or service, when dry run is turned on, and when the integration is disabled or removed.
   - **How it is compared:** list order, weekday order and location rounding of a few metres don't count as differences.
   - **If Rivian refuses the list** as invalid, the fault is `restore_rejected`. With the **enabled-only restore** option on, and disabled entries in the list, schedule control instead archives the full list and, after the error back-off, writes back only the enabled entries. The control state then shows `restore_enabled_only_active`. The disabled schedules have to be recreated in the Rivian app if they are wanted. If the enabled entries are refused too, it faults as before.
   - **Hand-back:** a manual restore hands control back to the owner until the next unplug, so the controller can't immediately take the schedule over again.
   - **Away while still plugged in** is usually a location glitch. The owner's list is restored, but the controller's request is kept, so the schedule control takes over again once the car shows home. A controller does not always repeat a request.
5. **Restarts resume.**
   - **The journal** is stored per vehicle at `.storage/rivian_schedule_control.<hash of the vehicle id>`, so it survives a remove and re-add. It is written atomically and resumed after a restart.
   - **The controller's request after a restart** is unconfirmed, because the controller may have restarted too (a power cut). It keeps a lease that is still live running, but it never starts charging or takes over the owner's schedule. Any request from the controller confirms it, and so does the **controller charging** entity being on; off drops it. Without that entity, a request whose lease lapsed meanwhile is dropped 30 seconds after the controller is seen up, and the controller asks again if it wants charging.
   - **A failed journal save of a request** is reported to the caller, but the request is kept: saves queued behind it already hold it. Until a save succeeds, a repeated request is saved again rather than acknowledged.
   - **An interrupted write** is resolved by reading Rivian. A write is recorded as sent as soon as Rivian answers, so a restart before it settles still recognizes it if it lands late. A write that may or may not have been sent before a crash is treated the same way.
   - **If setup fails while the schedule control owns the schedule**, for example because Rivian is unreachable, a persistent repair issue says so. Removing the integration restores the schedule.
   - **If a disable or removal cannot restore the owner's schedule**, a persistent repair issue says so and survives restarts. It goes away when Rivian schedule control is enabled again or added again for the vehicle, which resumes the restore.
   - **A change of Home Assistant's time zone** reloads the integration, which then rewrites its window in the new local time. A change of the home location takes effect at the next reload or restart.

**While the schedule control owns the schedule, the owner's own schedules are suspended.** Overnight charging then comes from the planner, which charges the car at full current in the free hours, as one fixed window to their end, not from the owner's Rivian schedule.

### Community pattern (fallback mode)

The **Community pattern** option, off by default, is the fallback if the [attended trial](#attended-trial) shows that the car does not start on a short window that began a few minutes ago, or does not stop when one ends. It copies what the community projects run ([known limits](#known-limits)).

| The controller asks for | Schedule control writes |
| --- | --- |
| Charging on, at N A | One window covering every day, all day (start 00:00, 1,440 minutes), at N A. It is never renewed. It is rewritten only when the current changes: a decrease at once, an increase after the increase interval. |
| Charging off | One 60-minute window at 8 A, starting 12 hours from now. When its start comes within 2 hours, it is moved again. `enabled: false` is never used. |
| Charging on, with an until time ahead | As without the option: one fixed window to that time, also while the planner looks down. |

The all-week window is parked when charging is no longer wanted, when the request is held, and when it could not have been renewed for `lease_minutes` (the car's state unknown, the controller down, or lease renewal off). Turning the option off replaces the community windows with the usual ones.

**Trade-off:** the all-week window never ends by itself. If Home Assistant or the network goes down, the car keeps charging at the last current until schedule control parks the window or restores the owner's schedule. With the usual leases it stops within `lease_minutes` + clock skew.

## Install and configure

1. **Prepare the Rivian app** ([plan, phase 0](PLAN.md#phase-0-the-rivian-app)): Smart Charging and its schedule recommendation off, one home schedule with no shared weekdays, and its pin on the house.
2. Install upstream [`home-assistant-rivian`](https://github.com/bretterer/home-assistant-rivian) 2.x (2.0.0 needs Home Assistant 2026.9 or later) for telemetry, if you have not already. Rivian schedule control logs in to Rivian separately, because upstream offers no schedule services; only session tokens are stored. Upstream's README describes an invited-driver account as an option; whether one can write schedules is not known.
   - **Disable upstream's four schedule entities:** `number.r1s_charging_schedule_amperage`, `switch.r1s_charging_schedule_enabled`, `time.r1s_charging_schedule_start` and `time.r1s_charging_schedule_end` (each entity's settings → Enabled off). A change to any of them rewrites the car's schedule list as one entry, which faults schedule control with `external_edit` or overwrites its parked window. Keep them out of dashboards and automations. `tools/check.py` warns while they are enabled.
   - **Note the tracker's ID:** `device_tracker.r1s_location` on installs made before October 12, 2026, `device_tracker.r1s` on fresh 2.0.0 installs.
   - **Keep coordinates out of the log.** The Rivian client logs whole error responses, coordinates included, at warning level. Add to `configuration.yaml`:

     ```yaml
     logger:
       logs:
         rivian: error
     ```

3. Install SolarSage using the [installation guide](installation.md). Rivian schedule control is bundled in the same integration.
4. **Settings → Devices & services → Add integration → SolarSage → Rivian schedule control.**
   - **Login:** Rivian email and password, plus the one-time code if your account uses one.
   - **Vehicle:** pick the R1S.
   - **Entities:**

     | Option (key) | Map to (example entity; use your actual IDs) | Required |
     | --- | --- | --- |
     | Plugged in (`plugged_entity`) | The Rivian charger connection binary sensor: `binary_sensor.r1s_charger_connection` | Yes |
     | Location (`location_entity`) | The Rivian device tracker, not a person: `device_tracker.r1s_location`, or `device_tracker.r1s` on a fresh upstream 2.0.0 install. Its latitude and longitude also give the windows their location. | Yes |
     | Charger state (`charger_state_entity`) | The Rivian integration's "Charger state" sensor, likely `sensor.r1s_charger_state` (`python -m tools.check` confirms it) | Recommended: without it the charger status can never be "charging" (`c`) |
     | Wall Connector vehicle connected (`wall_connector_entity`) | `binary_sensor.tesla_wall_connector_vehicle_connected` | Recommended: it ends a session when the Wall Connector sees no vehicle, and blocks control of a Rivian plugged in elsewhere |
     | Controller liveness (`controller_liveness_entity`) | The planner's heartbeat, `sensor.solarsage_heartbeat`: unavailable while the planner is off or down. Leave it empty until the planner is installed. | **Strongly recommended** once a controller runs: without it, a controller outage cannot stop renewals |
     | Controller wants charging (`controller_charging_entity`) | The planner's charging-wanted sensor for this car, `binary_sensor.r1s_planner_charging_wanted` | **Recommended** with liveness: it lets charging resume after an outage of the liveness entity, and after a long Home Assistant restart |
     | Controller charging until (`controller_until_entity`) | The planner's charging-until sensor for this car, `sensor.r1s_planner_charging_until`: a timestamp, the end of the free hours while the planner charges the car through them | **Recommended** with the planner: it gives the free hours one fixed window, written once. Without it they run on leases renewed all night. |
     | Vehicle override (`vehicle_override_entity`) | The planner's charger car override, `select.solarsage_charger_car_override` (`auto`, `tesla`, `rivian`) | Optional: when it names another car, the charger status reports `a` |

5. Options (**Configure**):

   | Option (key) | Default | Meaning |
   | --- | --- | --- |
   | Dry run (`dry_run`) | **on** | Write nothing, except restoring the owner's list if the schedule control still owns it. Shows the would-be schedule. |
   | Lease minutes (`lease_minutes`) | 20 | How long a charging window lasts without renewal. This is the fail-safe horizon (plus the clock skew). A fixed window ends at the controller's until time instead. |
   | Renew before, minutes (`renew_before_minutes`) | 8 | Renew when this little time remains. A fixed window is never renewed. |
   | Clock skew, minutes (`clock_skew_minutes`) | 5 | Padding at both ends of a lease, for a vehicle clock that differs from Home Assistant's. A fixed window has it only at the start. |
   | Minimum write interval, s (`min_write_interval_s`) | 30 | Spacing between current changes and renewals. Start and stop use 5 s. |
   | Increase interval, s (`increase_interval_s`) | 120 | Minimum spacing before raising the current |
   | Increase margin, A (`increase_margin_amps`) | 0 | Write a higher current this much below the request, rounded down to an even value; 48 A or more is still written as 48 A. 0 writes the request exactly, which is what the planner needs. evcc used 3. |
   | Ceiling hold after a decrease, minutes (`ceiling_hold_minutes`) | 0 (off) | Only with a margin: after the current drops from 48 to 44 or 46 A, a request of 48 A keeps the margin for this long. evcc used 30. |
   | Release delay, s (`release_delay_s`) | 60 | How long unplugged must last before restoring the owner's schedule. A replug within it keeps the window parked until the controller asks again. |
   | Restore enabled schedules only if Rivian rejects the full list (`restore_enabled_only`) | off | If Rivian refuses the owner's full list on a restore, write only its enabled schedules instead of faulting; the full list stays archived ([exact restore](#how-it-works)). |
   | Community pattern (`community_pattern`) | off | The [fallback mode](#community-pattern-fallback-mode): one all-week window while charging, one window parked 12 hours away while not. Turn it on only if the trial fails with the usual leases. |

   To drive the schedule control from evcc instead of the planner ([plan decision 8](PLAN.md#phase-0-the-rivian-app)), set the increase margin to 3 A and the ceiling hold to 30 minutes, map the liveness and charging options to evcc's entities, and leave the until option empty.

## Entities

The examples assume the config entry is titled `R1S`; your entity IDs follow your vehicle's name.

| Entity | Purpose |
| --- | --- |
| `switch.r1s_schedule_control_solar_charging` | Charging requested by the controller |
| `number.r1s_schedule_control_solar_current` | Requested current. Accepts 0–80 and applies even 8–48 A. |
| `sensor.r1s_schedule_control_charger_status` | Charger status: `a` (not known to be plugged in at home, known absent from the Wall Connector, or the override names another car), `b` (connected) or `c` (charging, from the charger state entity) |
| `sensor.r1s_schedule_control_control_state` | One of the control states below. Its attributes show the lease (`lease_amps` is the current Rivian holds, `lease_expires_at` its end, `lease_fixed` true for a fixed window), last write, fingerprints (prefixes only), errors, observation (without coordinates), `start_blocked_by`, `request_held`, `request_unconfirmed`, the dry-run target, `window_location_source`, `window_location_distance_m`, `owner_schedules_overlap_days`, `restore_enabled_only`, `restore_enabled_only_active`, `reads_last_hour` and `writes_last_hour`. Diagnostics hold the same, with the window location redacted. |
| `switch.r1s_schedule_control_lease_renewal` | Turn off to let an active lease lapse (trial T4). The setting persists. |
| `button.r1s_schedule_control_restore_owner_schedule` | Restore the owner's schedule and hand control back until unplug |
| `button.r1s_schedule_control_clear_fault` | Resume after a fault, if Rivian holds a state the schedule control knows |
| `button.r1s_schedule_control_read_schedules` | Read Rivian's schedule list: days, times, amps, enabled. It also shows whether the list is one the schedule control wrote; never coordinates. |

Control states: `idle`, `waiting_for_vehicle`, `handed_back`, `lease_active`, `lease_lapsed`, `paused`, `writing`, `fault`, `dry_run`. A fixed window that has ended shows `paused`; `lease_lapsed` is a lease that ended for want of renewal.

Services:

| Service | What it does |
| --- | --- |
| `solar_ev_planner.release` | Same as the restore button |
| `.clear_fault` | Same as the clear-fault button |
| `.read_schedules` | Returns the list summary |
| `.force_restore` | **Faulted only.** Writes the snapshotted owner list whatever Rivian holds now, then verifies it. An archived list from an earlier abandon is used only while Rivian holds a window the schedule control wrote, so it never replaces schedules the owner has set up since. Otherwise the call fails with `force_restore_archive_unsafe` and writes nothing. |
| `.abandon_ownership` | **Faulted only.** Forgets ownership without writing. The owner's list is archived, not deleted, and shown in a notification so it can be recreated or force-restored. |

## Driving schedule control from the planner

The planner ([PLANNER.md](PLANNER.md)) drives Rivian schedule control through Home Assistant entities only, following the rules below. A test runs both integrations together (`tests/planner/test_with_schedule_control.py`). With the entry titled `R1S`:

| The planner | Through | Schedule control |
| --- | --- | --- |
| Starts or pauses charging | `switch.r1s_schedule_control_solar_charging`: turn on, turn off | Records the request at once; the switch shows it. The schedule write follows in the background. |
| Sets the current | `number.r1s_schedule_control_solar_current`: set value, 0–80 A | Records it at once; the number shows it. It applies the request rounded down to an even 8–48 A. |
| Reads what happens | `sensor.r1s_schedule_control_control_state` and its attributes | `lease_amps` is the current Rivian holds; `lease_fixed` is true for a fixed window, and `lease_expires_at` is its end; `start_blocked_by` says why charging cannot start or continue |
| Reads whether the Rivian is the car on the charger | `sensor.r1s_schedule_control_charger_status` | `b` or `c` only while it is known plugged in at home and not known absent from the Wall Connector |

**What the planner provides**, mapped in the schedule control's options:

- **A heartbeat** (`controller_liveness_entity`): the planner's `sensor.solarsage_heartbeat`. Any domain works. Its state must be known (not `unknown` or `unavailable`) while the planner runs, and unavailable or unknown while it does not. Use a dedicated entity: one that is unknown in normal use, such as "next change" with nothing planned, would read as the planner being down. The schedule control does not check how recent the value is, so a planner that runs but has stopped deciding should make its heartbeat unavailable itself. Never map one of the schedule control's own entities: they stay available.
- **Wants charging** (`controller_charging_entity`): the planner's `binary_sensor.r1s_planner_charging_wanted`. It must be a binary sensor, on while the planner wants this car charging, off while it wants it paused, unknown while it has no decision. It is not a command. The schedule control reads it only to settle a request from before a Home Assistant restart or a planner outage; the planner still commands the switch. While the planner sends nothing (shadow mode), keep it unknown, so it neither confirms nor drops requests the planner did not make.
- **Charging until** (`controller_until_entity`): the planner's `sensor.r1s_planner_charging_until`. A timestamp sensor: the end of the free hours the car's standing request belongs to. The planner sets it before it sends the command it belongs to, so that command is written as one fixed window to that time, and it keeps the time, even once past, until a daytime command has replaced the request, so an ended window is not started again on the night's request. It is unknown before the first free-hours charge of a plug-in, after unplug, and while Planning is off. A live fixed window keeps its end whatever the entity shows later. Unknown, or a time already past, gives the usual leases.

**Rules for the requests:**

1. **Command the switch explicitly.** While no switch request stands (after a plug-in, a restore or a dropped request), setting the current alone also starts charging, because evcc set the current first. To set a current without charging, turn the switch off first.
2. **Ask for even currents from 8 to 48 A.** Other values are rounded down to an even value in that range: 0–9 A charges at 8 A, 15 A at 14 A, 60 A at 48 A. Pause with the switch, not with 0 A.
3. **Changes take time.** Each write reads Rivian's schedule, writes it and reads it back, and the car takes a minute or two to follow. Judge a change by `lease_amps` and the Wall Connector, not by the number, which shows the request.
4. **The schedule control paces writes too.** Writes are at least `min_write_interval_s` (30 s) apart, starts and pauses 5 s. An increase also waits until `increase_interval_s` (120 s) has passed since the last current change. A request made sooner is applied when its time comes.
5. **Repeating an unchanged request writes nothing** and shows the planner is running. Compare the standing request with the decision every cycle and send what differs. The switch shows off both for a pause and for no request at all; `desired_charge` in the control state's attributes tells them apart (`true`, `false`, or `null` for none), and `requested_amps` is the current.
6. **A session end forgets the request.** Unplugging, or the Wall Connector showing no vehicle, clears it and the switch turns off. After the next plug-in the planner must command again, even with an unchanged decision.
7. **After a restart or an outage the request may be dropped.** After Home Assistant or the schedule control restarts, and after the heartbeat looks down, the earlier request is unconfirmed: any request from the planner confirms it, and so does the wants-charging entity being on. That entity being off drops it, so keep it in step with the switch. Without the entity, a request is dropped after a restart unless its charging window is still live, and after the heartbeat looked down for 3 minutes or more (10 until it has been seen up since Home Assistant started). A dropped request turns the switch off; the planner then sends its decision again.
8. **Some states ignore requests.** `handed_back` (the owner pressed Restore owner schedule) ignores them until the car is unplugged; `fault` writes nothing until the owner clears it; `dry_run` writes nothing. The planner reports these and does not retry harder.
9. **The schedule control cannot tell which car the Wall Connector sees.** It acts whenever the Rivian is plugged in at home and the Wall Connector is not known to be empty; the vehicle override changes only the charger status. Command the Rivian only once the planner has decided that the Rivian is the car on the Wall Connector. Any first command, a switch off too, takes over the owner's schedule: a pause is a parked window, not the owner's own schedule. To leave the owner's schedule in charge, send nothing.
10. **The schedule control renews the lease on its own** while the car is plugged in at home and the heartbeat is up; the planner does not repeat itself to keep charging. While the heartbeat looks down, nothing is started or renewed, and after 3 minutes (10 until it has been seen up since Home Assistant started) the window is parked, so charging stops. With Home Assistant itself down, the window ends on its own within `lease_minutes` + clock skew (25 minutes by default).
11. **The free hours get one write.** With the until entity mapped, the planner's command at the start of the free hours (or at a plug-in during them) is written once, as a fixed window at full current to their end. It is not renewed, and it is not parked while the heartbeat looks down: Planning off, or the planner down, leaves the car charging on free power until the window ends. Only a pause, an unplug, leaving home, a restore or dry run ends it sooner. A current change, or the night the clocks change, rewrites it with the same end. At its end the car stops by itself and nothing is written; the control state shows `paused` until the planner's next command.

## Attended trial

Rivian schedule control is also the safest tool for the attended Gen 2 trial ([plan phase 5](PLAN.md#phases)). Every step is journaled and restorable, unlike editing schedule entities by hand. Keep the planner in shadow mode during the trial, so that you are the only writer; its heartbeat stays up in shadow mode, so the schedule control is not blocked. Its charging-until sensor stays unknown in shadow mode, so every trial write is a lease. `python -m tools.attended_test rivian --i-am-at-the-car` walks through the steps below and logs them ([TESTING.md, stage 5](TESTING.md#5-the-attended-rivian-trial)).

**The trial is the gate.** All of T0–T8 must pass before the Rivian goes under the planner. The Wall Connector's measured current is the ground truth.

**Prerequisites:**

- Owner present, including one night from 23:30 to 00:15 for T8.
- Smart Charging and its schedule recommendation off in the Rivian app, one home schedule with its pin on the house ([plan, phase 0](PLAN.md#phase-0-the-rivian-app)).
- Upstream's four schedule entities disabled ([install](#install-and-configure), step 2).
- Other schedule writers paused; don't edit in the Rivian app during the test.
- Wall Connector readings fresh.
- Home Assistant's time zone set to the vehicle's.
- Dry run **off**.

**Recording.** For each step record the time, the action and the `control_state` attributes. Also record the Wall Connector current/power and the Rivian app's view of the schedule. The tool records the car's software version first, and for each command the time until the Wall Connector current moves. Throughout, `reads_last_hour` and `writes_last_hour` should stay at about 60 or less, and no `external_edit` fault should come from upstream's entities or Smart Charging.

| Test | Steps with schedule control | Pass condition |
| --- | --- | --- |
| Inventory | Press **Read schedules**. | Note the count and fields; the tool keeps the list's fingerprint for T7. Rivian can return entries that it then refuses to accept back as one list ([upstream PR #308](https://github.com/bretterer/home-assistant-rivian/pull/308)), so T1 restores your actual list before any charging test. |
| T1 Write and readback (no charging possible) | The schedule control only acts while the car is plugged in at home. First set the Rivian's charge limit at or below its current SoC so no session can start. Set current 16, turn **Solar charging** on, then off, then press **Restore owner schedule**. Then unplug and replug. | `control_state` goes `lease_active` → `paused` → `handed_back` with no fault. The Rivian app shows each change at "Home", and its displayed times match Home Assistant's local time. **Read schedules** shows the original list. Restore the charge limit afterwards. |
| T0 Start on an idle car | Leave the car plugged in and not charging for 30 minutes or more. Set 16 A and turn **Solar charging** on: the lease starts 5 minutes in the past. | Charging starts. Record the time until the lease is written and until the car draws current. This is the central Gen 2 unknown: the community projects use all-day windows. |
| T2 Amps mid-session | While charging under a lease: 16 → 10 → 20 → 8 → 48 A. Each increase is written once the increase interval (2 minutes) has passed since the last change, at exactly the requested current. | Wall Connector current follows each step, 8 A and 48 A included. Record latency. |
| T2b Continuity | Keep charging through at least two renewals (about every 12 minutes) and two current changes. | The Wall Connector contactor stays closed and the current never drops to 0 during a rewrite |
| T3 Window off now | Turn **Solar charging** off. | Charging stops. Record latency. |
| T4 Lease expiry | Turn **Solar charging** on, and once charging, turn **Lease renewal** off. Wait until `lease_minutes` + clock skew after the last write (`lease_written_at`). | Charging stops at the window end with no write: `control_state` is `lease_lapsed` and `last_write_at` is unchanged. About a minute later the schedule control parks the lapsed window (`control_state` `paused`). |
| T5 Window back on now | Turn **Lease renewal** back on. **Solar charging** is still on, so the schedule control writes a new lease covering now. | Charging resumes. Record latency. |
| T6 Plug in while parked | With the window parked, unplug and replug within the release delay. Then press "Charge now" in the Rivian app. | Car waits after replugging. Record the Wall Connector current during "Charge now" (an upstream report suggests it charges at the parked window's 8 A), and whether it changes the schedule, which would make the schedule control fault with `external_edit`. |
| T7 Exact restore | **Restore owner schedule** (or unplug for the release delay). | **Read schedules** matches the inventory by fingerprint, not only by count |
| T8 Midnight | **Mandatory.** Start it between 23:30 and 23:59 (`--only t8`): a 16 A lease, renewed across local midnight. When schedule control holds a free-hours window, the tool also watches that window end. Under the planner each free night's fixed window crosses midnight too; the first night of [TESTING.md, stage 6](TESTING.md#6-the-rivian-under-the-planner) checks it again. | Charging continues without a gap past midnight, renewed, until 00:15 |

If every step passes with acceptable latency, continue with [plan phase 6](PLAN.md#phases). Re-run T0 and T2 after every Rivian software update.

**If T0, T3, T4 or T5 fail:** turn on the [community pattern](#community-pattern-fallback-mode) and repeat T0–T8. In that mode T4 ends with a write, not a lapse: the all-week window is parked once it could not be renewed for `lease_minutes`, so judge T4 by the Wall Connector, not by `last_write_at`. T3 and T5 test the parked window and the all-week window. If that fails too, the plan's [fallbacks](PLAN.md#if-the-rivian-trial-fails) say what is left.

## Faults and recovery

Entering a fault posts a Home Assistant notification. While faulted, the schedule control writes nothing on its own, and any active lease lapses. **A parked, lapsed or ended window recurs once a week until the owner's schedule is restored** (an ended fixed window as the same night's window), so clear faults promptly.

| Fault code | Meaning | What to do |
| --- | --- | --- |
| `external_edit` | The schedule changed outside Rivian schedule control; nothing was overwritten | Check the Rivian app. Once the owner's schedule is back, **Clear fault**. Or use `force_restore` to write the snapshot over whatever is there. |
| `conflict_after_write` | A readback matched neither the write nor the previous state | Same as above |
| `write_not_applied` | Rivian accepted writes but showed no change, repeatedly | Check the vehicle and account, then **Clear fault** |
| `write_rejected` | Rivian rejected the schedule control's window, repeatedly | Check the account and the Rivian app's schedule screen, then **Clear fault** |
| `restore_rejected` | Rivian refused the owner's list when it was written back. Some accounts return entries that cannot be written back together ([upstream PR #308](https://github.com/bretterer/home-assistant-rivian/pull/308)). The snapshot is kept. With the enabled-only restore on, this fault comes only if the enabled entries are refused too, or the list has no disabled entries. | Recreate the owner's schedule in the Rivian app, then **Clear fault** |
| `own_window_present` | Rivian holds a window that Rivian schedule control wrote, or one shaped like it, so it is not snapshotted as the owner's | Recreate the owner's schedule in the Rivian app, or `force_restore` the archived snapshot |
| `unsupported_schedule` | Rivian returned a schedule shape the schedule control does not understand | Nothing was written. Report the redacted shape from **Read schedules**. |
| `force_restore_unverified` | A forced restore could not be confirmed by a readback | Check the Rivian app |

**Other conditions:**

- **Rate limits** pause writes for 5 minutes, and an account lock for 30.
- **Other errors** back off from 15 seconds up to 15 minutes.
- **An expired login** starts Home Assistant's re-authentication flow.
- **Owner schedules that share weekdays** raise the repair issue "Owner's Rivian schedules share weekdays" before the first write ([ownership rules](#how-it-works)). It names the days. Reduce the schedules in the Rivian app to one set without shared weekdays, or turn on the enabled-only restore.
- **The car far from the window's location:** a warning in the log, once, above 50 m. Check the schedule's pin in the car.
- **A missing mapped entity** for more than 5 minutes raises a repair issue. A missing liveness entity counts as the controller being down. A missing until entity gives leases instead of fixed windows.
- **Controller down** blocks starts and renewals at once; `start_blocked_by` shows `controller_unavailable`. A live fixed window runs on to its end. The `request_held` and `request_unconfirmed` attributes show where the controller's request stands. See [How it works](#how-it-works).

## Known limits

- **Vehicle behavior is unproven on a Gen 2.** The technique is in use: four open-source projects ([`rchav/rivian_solar_charge_ha`](https://github.com/rchav/rivian_solar_charge_ha), [`ostap-korkuna/rivian-charging-automation`](https://github.com/ostap-korkuna/rivian-charging-automation), "tested on R1S", [`peterchester/rivian-tesla-solar-charging`](https://github.com/peterchester/rivian-tesla-solar-charging) and [`rjpease/RivianEVCC`](https://github.com/rjpease/RivianEVCC), an R1S on a Wall Connector Gen 3 like this one) modulate charging through the schedule amperage, and an owner reports the app's schedule amperage changing the rate mid-charge ([upstream issue #244](https://github.com/bretterer/home-assistant-rivian/issues/244)). They all use one all-week window, not short windows. None of them states a Gen 2 vehicle or documents how reliably a moved or lapsed window stops a session. The trial establishes that for this car.
- **The readback is the cloud's view.** It shows that Rivian stored the list, not that the car received it. The Wall Connector shows the result. Rivian's Parallax feed reports the car's own window, but schedule control does not read it, and no charging write over Parallax has been shown to work, so none is used.
- **Whether a write replaces the whole list** is inferred, not shown. A merge would show up as a fault at the readback; trial T1 tests it.
- **An empty owner list** is restored as an empty list. The car may then charge at full power at every plug-in. Keep one home schedule.
- **The overlap repair issue is not kept across restarts.** After a restart it comes back only while schedule control still holds an owner list, or at the next takeover.
- **Owner schedules are suspended while owned.** While owned, the owner's schedules are replaced by a single lease, fixed window or parked window. A parked or ended window recurs weekly while the schedule control cannot write.
- **Lease lapse is the only cloud-free stop.** A cloud or network outage cannot stop a lease that is already active; it lapses on its own after at most `lease_minutes` + clock skew. A fixed window runs to its end, the end of the free hours. The trial (T4) must show that a lapsing window stops a session. The community pattern has no cloud-free stop at all.
- **The vehicle's time zone must match Home Assistant's.** Windows are built in Home Assistant's local time, and the setup warns if that is UTC.
- **Currents are even, from 8 to 48 A.** A request is rounded down to an even value; below 8 A the car charges at 8 A. Only the switch pauses.
- **Liveness sees a stopped controller, not a stuck one.** A controller that runs but stops deciding goes unnoticed unless its liveness entity reflects that.
- **Without the controller charging entity, some stops last until the controller asks again.** These are a liveness outage of 3 minutes or more while the controller keeps running, and a Home Assistant restart long enough for the lease to lapse.
- **A hand-edited window can be taken for the owner's.** After abandon ownership, a window the schedule control wrote and the owner then edited to another length is no longer recognized. The next takeover would snapshot it as the owner's list, and a second abandon would replace the archived original with it. Recreate the owner's schedule in the Rivian app before the controller takes over again.
