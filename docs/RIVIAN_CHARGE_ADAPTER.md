# Rivian charge adapter

**What it is.** The Rivian charge adapter (`custom_components/rivian_solar_adapter`) gives a controller a start/stop switch and a current setting for a Gen 2 Rivian, which has no usable remote charging command. It does this by writing the vehicle's **charging schedule** through Rivian's API. In the [working plan](PLAN.md) the controller is the solar planner: it sets the switch and the current, and provides two entities that tell the adapter it is running and whether it wants the car charging ([what the planner must provide](#driving-the-adapter-from-the-planner)). The planner itself is described in [PLANNER.md](PLANNER.md).

**Status: software-tested only.** Four rounds of independent adversarial review shaped the design, including closed-loop simulations against evcc's charging loop. Its tests run against Home Assistant 2026.10.3 with a simulated Rivian provider. It has not been run against a real account, vehicle, or Wall Connector. **It installs in dry-run mode, and nothing is written until you turn dry run off.**

## How it works

| The controller asks for | The adapter writes | Expected vehicle behavior (to be confirmed by the [trial](#attended-trial)) |
| --- | --- | --- |
| Charging on, at N A | A **lease**: one window from 5 minutes ago to `lease_minutes` + 5 minutes from now, at N A rounded down to an even 8–48 A. The 5-minute padding absorbs clock differences. | Charges at N A |
| A lower current | The same lease at the lower amperage, at the next allowed write | Current drops |
| A higher current | The same lease at N A rounded down to an even 8–48 A, once the increase interval has passed since the last current change. Two evcc-era rules are options, off by default: an **increase margin** writes increases that much below the request, and a **ceiling hold** keeps the margin at 48 A for a while after the current dropped from 48 to 44 or 46 A. evcc needed 3 A and 30 minutes, because it added up to 2 A to its requests and chased the surplus; the planner asks for exact currents and rate-limits itself. | Current rises |
| Charging off | A **parked window** at 8 A that ended 5 minutes ago, so no window covers now | Waits |
| Nothing new | Renews the lease when 8 minutes remain, but **only** while the car is known plugged in at home and the controller is alive | Keeps charging |
| Nothing new, and the lease cannot be extended (the controller looks down, the car's state unknown, or renewal off) | Nothing: the window ends on the vehicle by itself, `lease_minutes` + 5 minutes after it was written. That is the cloud-free stop. A minute later a parked window replaces it, so it cannot recur a week later. | Stops at the window end |
| Nothing, and the controller has looked down for 3 minutes | Its request from before the outage is **held** and the window is parked. One unavailable reading is not enough. Until the liveness entity has been seen available since Home Assistant started, the wait is 10 minutes. Any request the controller makes meanwhile, changed or not, shows it is running and confirms its request. | Stops |
| The controller is back after looking down | It may have restarted, and then it asks again if it wants charging. The **controller charging** entity tells: on means the controller still wants this car charging, so a held request resumes; off drops it. Without that entity, a held request is dropped. A shorter gap keeps the request. | Resumes, or waits for the controller |
| Nothing (Home Assistant itself down) | Nothing; the window ends on its own within `lease_minutes` + 5 minutes | Stops at the window end |

**Ownership rules:**

1. **Snapshot first.** The first command while the car is plugged in at home at the Wall Connector takes ownership. Before writing, the adapter reads and persists the owner's **complete** schedule list.
   - It refuses if Rivian holds a window this adapter itself wrote earlier. That protects against restoring the adapter's own window as if it were the owner's.
   - It does not take over while the liveness entity shows the controller down, so the owner's schedule is never replaced only to be parked.
   - A command for an unplugged car is discarded, not saved for a later plug-in. A command while the car is plugged in but away is kept.
2. **Persist, guard, then write.** Every write is recorded in the journal before anything is sent, and a failed save sends nothing. A fresh read then checks that Rivian still holds what the adapter last verified, or one of its own unverified writes. If it holds anything else, such as an edit in the Rivian app, the adapter **faults and writes nothing**.
3. **Readback decides.** A write counts only when a fresh read shows it.
   - **Settling:** a write that reads back unchanged is re-read 30 seconds later before it counts as not applied.
   - **Never counted:** writes Rivian definitely refused (rate limit, expired login) or never received are withdrawn without counting.
   - **Late landings:** unverified writes are remembered, so one that lands late is recognized as the adapter's own.
   - **Restore while writes are outstanding:** it is still sent and confirmed only after a settled re-read, and the owner's list is also archived in case an earlier write lands afterwards.
4. **Exact restore.**
   - **When it happens:** the owner's original list is written back on unplug (after `release_delay_s`), on away (after 10 minutes), on the **Restore owner schedule** button or service, when dry run is turned on, and when the integration is disabled or removed.
   - **How it is compared:** list order, weekday order and location rounding of a few metres don't count as differences.
   - **Hand-back:** a manual restore hands control back to the owner until the next unplug, so the controller can't immediately take the schedule over again.
   - **Away while still plugged in** is usually a location glitch. The owner's list is restored, but the controller's request is kept, so the adapter takes over again once the car shows home. A controller does not always repeat a request.
5. **Restarts resume.**
   - **The journal** is stored per vehicle at `.storage/rivian_solar_adapter.<hash of the vehicle id>`, so it survives a remove and re-add. It is written atomically and resumed after a restart.
   - **The controller's request after a restart** is unconfirmed, because the controller may have restarted too (a power cut). It keeps a lease that is still live running, but it never starts charging or takes over the owner's schedule. Any request from the controller confirms it, and so does the **controller charging** entity being on; off drops it. Without that entity, a request whose lease lapsed meanwhile is dropped 30 seconds after the controller is seen up, and the controller asks again if it wants charging.
   - **A failed journal save of a request** is reported to the caller, but the request is kept: saves queued behind it already hold it. Until a save succeeds, a repeated request is saved again rather than acknowledged.
   - **An interrupted write** is resolved by reading Rivian. A write is recorded as sent as soon as Rivian answers, so a restart before it settles still recognizes it if it lands late. A write that may or may not have been sent before a crash is treated the same way.
   - **If setup fails while the adapter owns the schedule**, for example because Rivian is unreachable, a persistent repair issue says so. Removing the integration restores the schedule.
   - **If a disable or removal cannot restore the owner's schedule**, a persistent repair issue says so and survives restarts. It goes away when the adapter is enabled again or added again for the vehicle, which resumes the restore.
   - **A change of Home Assistant's time zone** reloads the adapter, which then rewrites its window in the new local time. A change of the home location takes effect at the next reload or restart.

**While the adapter owns the schedule, the owner's own schedules are suspended.** Overnight charging then comes from the planner, which charges the car at full current in the free hours, not from the owner's Rivian schedule.

## Install and configure

1. Install upstream [`home-assistant-rivian`](https://github.com/bretterer/home-assistant-rivian) 2.x for telemetry, if you have not already. The adapter logs in to Rivian separately, because upstream offers no schedule services; only session tokens are stored.
2. Install this repository through HACS (custom repository, category integration), or copy `custom_components/rivian_solar_adapter` into `/config/custom_components/`. Restart Home Assistant. HACS installs only the adapter from this repository; the planner is copied by hand ([PLANNER.md, install](PLANNER.md#install)).
3. **Settings → Devices & services → Add integration → Rivian Solar Charge Adapter.**
   - **Login:** Rivian email and password, plus the one-time code if your account uses one.
   - **Vehicle:** pick the R1S.
   - **Entities:**

     | Option (key) | Map to (the owner's entity, where known) | Required |
     | --- | --- | --- |
     | Plugged in (`plugged_entity`) | The Rivian charger connection binary sensor: `binary_sensor.r1s_charger_connection` | Yes |
     | Location (`location_entity`) | The Rivian device tracker, not a person: `device_tracker.r1s_location` | Yes |
     | Charger state (`charger_state_entity`) | The Rivian integration's "Charger state" sensor, likely `sensor.r1s_charger_state` (`python -m tools.check` confirms it) | Recommended: without it the charger status can never be "charging" (`c`) |
     | Wall Connector vehicle connected (`wall_connector_entity`) | `binary_sensor.tesla_wall_connector_vehicle_connected` | Recommended: it ends a session when the Wall Connector sees no vehicle, and blocks control of a Rivian plugged in elsewhere |
     | Controller liveness (`controller_liveness_entity`) | The planner's heartbeat, `sensor.solar_ev_planner_heartbeat`: unavailable while the planner is off or down. Leave it empty until the planner is installed. | **Strongly recommended** once a controller runs: without it, a controller outage cannot stop renewals |
     | Controller wants charging (`controller_charging_entity`) | The planner's charging-wanted sensor for this car, `binary_sensor.r1s_planner_charging_wanted` | **Recommended** with liveness: it lets charging resume after an outage of the liveness entity, and after a long Home Assistant restart |
     | Vehicle override (`vehicle_override_entity`) | The planner's charger car override, `select.solar_ev_planner_charger_car_override` (`auto`, `tesla`, `rivian`) | Optional: when it names another car, the charger status reports `a` |

4. Options (**Configure**):

   | Option (key) | Default | Meaning |
   | --- | --- | --- |
   | Dry run (`dry_run`) | **on** | Write nothing, except restoring the owner's list if the adapter still owns it. Shows the would-be schedule. |
   | Lease minutes (`lease_minutes`) | 20 | How long a charging window lasts without renewal. This is the fail-safe horizon (plus the clock skew). |
   | Renew before, minutes (`renew_before_minutes`) | 8 | Renew when this little time remains |
   | Clock skew, minutes (`clock_skew_minutes`) | 5 | Padding at both ends of a lease, for a vehicle clock that differs from Home Assistant's |
   | Minimum write interval, s (`min_write_interval_s`) | 30 | Spacing between current changes and renewals. Start and stop use 5 s. |
   | Increase interval, s (`increase_interval_s`) | 120 | Minimum spacing before raising the current |
   | Increase margin, A (`increase_margin_amps`) | 0 | Write a higher current this much below the request, rounded down to an even value; 48 A or more is still written as 48 A. 0 writes the request exactly, which is what the planner needs. evcc used 3. |
   | Ceiling hold after a decrease, minutes (`ceiling_hold_minutes`) | 0 (off) | Only with a margin: after the current drops from 48 to 44 or 46 A, a request of 48 A keeps the margin for this long. evcc used 30. |
   | Release delay, s (`release_delay_s`) | 60 | How long unplugged must last before restoring the owner's schedule. A replug within it keeps the window parked until the controller asks again. |

5. **Upgrading from the evcc version** (config entry version 1.1). Home Assistant migrates the entry once, at the first start of this version:
   - `evcc_liveness_entity` becomes `controller_liveness_entity`, and `evcc_enabled_entity` becomes `controller_charging_entity`. The mapped entities stay; point them at the planner's entities once it is installed.
   - The evcc status sensor becomes the charger status sensor. Its registry entry is kept, so its history is too: `sensor.r1s_solar_adapter_evcc_status` becomes `sensor.r1s_solar_adapter_charger_status`, unless its entity ID was changed by hand, which is kept.
   - The increase margin and the ceiling hold are new and start off. To drive the adapter from evcc again, set them to 3 A and 30 minutes and map the two controller options to evcc's entities.
   - An earlier version of the adapter installed over this one no longer sees the two controller options.

## Entities

The examples assume the config entry is titled `R1S`; your entity IDs follow your vehicle's name.

| Entity | Purpose |
| --- | --- |
| `switch.r1s_solar_adapter_solar_charging` | Charging requested by the controller |
| `number.r1s_solar_adapter_solar_current` | Requested current. Accepts 0–80 and applies even 8–48 A. |
| `sensor.r1s_solar_adapter_charger_status` | Charger status: `a` (not known to be plugged in at home, known absent from the Wall Connector, or the override names another car), `b` (connected) or `c` (charging, from the charger state entity) |
| `sensor.r1s_solar_adapter_control_state` | One of the control states below. Its attributes show the lease (`lease_amps` is the current Rivian holds), last write, fingerprints (prefixes only), errors, observation, `start_blocked_by`, `request_held`, `request_unconfirmed` and the dry-run target. |
| `switch.r1s_solar_adapter_lease_renewal` | Turn off to let an active lease lapse (trial T4). The setting persists. |
| `button.r1s_solar_adapter_restore_owner_schedule` | Restore the owner's schedule and hand control back until unplug |
| `button.r1s_solar_adapter_clear_fault` | Resume after a fault, if Rivian holds a state the adapter knows |
| `button.r1s_solar_adapter_read_schedules` | Read Rivian's schedule list: days, times, amps, enabled. It also shows whether the list is one the adapter wrote; never coordinates. |

Control states: `idle`, `waiting_for_vehicle`, `handed_back`, `lease_active`, `lease_lapsed`, `paused`, `writing`, `fault`, `dry_run`.

Services:

| Service | What it does |
| --- | --- |
| `rivian_solar_adapter.release` | Same as the restore button |
| `.clear_fault` | Same as the clear-fault button |
| `.read_schedules` | Returns the list summary |
| `.force_restore` | **Faulted only.** Writes the snapshotted owner list whatever Rivian holds now, then verifies it. An archived list from an earlier abandon is used only while Rivian holds an adapter window, so it never replaces schedules the owner has set up since. Otherwise the call fails with `force_restore_archive_unsafe` and writes nothing. |
| `.abandon_ownership` | **Faulted only.** Forgets ownership without writing. The owner's list is archived, not deleted, and shown in a notification so it can be recreated or force-restored. |

## Driving the adapter from the planner

The planner ([PLANNER.md](PLANNER.md)) drives the adapter through Home Assistant entities only, following the rules below. A test runs both integrations together (`tests/planner/test_with_adapter.py`). With the entry titled `R1S`:

| The planner | Through | The adapter |
| --- | --- | --- |
| Starts or pauses charging | `switch.r1s_solar_adapter_solar_charging`: turn on, turn off | Records the request at once; the switch shows it. The schedule write follows in the background. |
| Sets the current | `number.r1s_solar_adapter_solar_current`: set value, 0–80 A | Records it at once; the number shows it. It applies the request rounded down to an even 8–48 A. |
| Reads what happens | `sensor.r1s_solar_adapter_control_state` and its attributes | `lease_amps` is the current Rivian holds; `start_blocked_by` says why charging cannot start or continue |
| Reads whether the Rivian is the car on the charger | `sensor.r1s_solar_adapter_charger_status` | `b` or `c` only while it is known plugged in at home and not known absent from the Wall Connector |

**What the planner provides**, mapped in the adapter's options:

- **A heartbeat** (`controller_liveness_entity`): the planner's `sensor.solar_ev_planner_heartbeat`. Any domain works. Its state must be known (not `unknown` or `unavailable`) while the planner runs, and unavailable or unknown while it does not. Use a dedicated entity: one that is unknown in normal use, such as "next change" with nothing planned, would read as the planner being down. The adapter does not check how recent the value is, so a planner that runs but has stopped deciding should make its heartbeat unavailable itself. Never map one of the adapter's own entities: they stay available.
- **Wants charging** (`controller_charging_entity`): the planner's `binary_sensor.r1s_planner_charging_wanted`. It must be a binary sensor, on while the planner wants this car charging, off while it wants it paused, unknown while it has no decision. It is not a command. The adapter reads it only to settle a request from before a Home Assistant restart or a planner outage; the planner still commands the switch. While the planner sends nothing (shadow mode), keep it unknown, so it neither confirms nor drops requests the planner did not make.

**Rules for the requests:**

1. **Command the switch explicitly.** While no switch request stands (after a plug-in, a restore or a dropped request), setting the current alone also starts charging, because evcc set the current first. To set a current without charging, turn the switch off first.
2. **Ask for even currents from 8 to 48 A.** Other values are rounded down to an even value in that range: 0–9 A charges at 8 A, 15 A at 14 A, 60 A at 48 A. Pause with the switch, not with 0 A.
3. **Changes take time.** Each write reads Rivian's schedule, writes it and reads it back, and the car takes a minute or two to follow. Judge a change by `lease_amps` and the Wall Connector, not by the number, which shows the request.
4. **The adapter paces writes too.** Writes are at least `min_write_interval_s` (30 s) apart, starts and pauses 5 s. An increase also waits until `increase_interval_s` (120 s) has passed since the last current change. A request made sooner is applied when its time comes.
5. **Repeating an unchanged request writes nothing** and shows the planner is running. Compare the standing request with the decision every cycle and send what differs. The switch shows off both for a pause and for no request at all; `desired_charge` in the control state's attributes tells them apart (`true`, `false`, or `null` for none), and `requested_amps` is the current.
6. **A session end forgets the request.** Unplugging, or the Wall Connector showing no vehicle, clears it and the switch turns off. After the next plug-in the planner must command again, even with an unchanged decision.
7. **After a restart or an outage the request may be dropped.** After Home Assistant or the adapter restarts, and after the heartbeat looks down, the earlier request is unconfirmed: any request from the planner confirms it, and so does the wants-charging entity being on. That entity being off drops it, so keep it in step with the switch. Without the entity, a request is dropped after a restart unless its charging window is still live, and after the heartbeat looked down for 3 minutes or more (10 until it has been seen up since Home Assistant started). A dropped request turns the switch off; the planner then sends its decision again.
8. **Some states ignore requests.** `handed_back` (the owner pressed Restore owner schedule) ignores them until the car is unplugged; `fault` writes nothing until the owner clears it; `dry_run` writes nothing. The planner reports these and does not retry harder.
9. **The adapter cannot tell which car the Wall Connector sees.** It acts whenever the Rivian is plugged in at home and the Wall Connector is not known to be empty; the vehicle override changes only the charger status. Command the Rivian only once the planner has decided that the Rivian is the car on the Wall Connector. Any first command, a switch off too, takes over the owner's schedule: a pause is a parked window, not the owner's own schedule. To leave the owner's schedule in charge, send nothing.
10. **The adapter renews the lease on its own** while the car is plugged in at home and the heartbeat is up; the planner does not repeat itself to keep charging. While the heartbeat looks down, nothing is started or renewed, and after 3 minutes (10 until it has been seen up since Home Assistant started) the window is parked, so charging stops. With Home Assistant itself down, the window ends on its own within `lease_minutes` + clock skew (25 minutes by default).

## Attended trial

The adapter is also the safest tool for the attended Gen 2 trial ([plan phase 5](PLAN.md#phases)). Every step is journaled and restorable, unlike editing schedule entities by hand. Keep the planner in shadow mode during the trial, so that you are the only writer; its heartbeat stays up in shadow mode, so the adapter is not blocked. `python -m tools.attended_test rivian --i-am-at-the-car` walks through the steps below and logs them ([TESTING.md, stage 5](TESTING.md#5-the-attended-rivian-trial)).

**Prerequisites:**

- Owner present.
- Other schedule writers paused; don't edit in the Rivian app during the test.
- Wall Connector readings fresh.
- Home Assistant's time zone set to the vehicle's.
- Dry run **off**.

**Recording.** For each step record the time, the action and the `control_state` attributes. Also record the Wall Connector current/power and the Rivian app's view of the schedule.

| Test | Steps with the adapter | Pass condition |
| --- | --- | --- |
| Inventory | Press **Read schedules**. | Note the count and fields. Rivian can return entries that it then refuses to accept back as one list ([upstream PR #308](https://github.com/bretterer/home-assistant-rivian/pull/308)), so T1 restores your actual list before any charging test. |
| T1 Write and readback (no charging possible) | The adapter only acts while the car is plugged in at home. First set the Rivian's charge limit at or below its current SoC so no session can start. Set current 16, turn **Solar charging** on, then off, then press **Restore owner schedule**. | `control_state` goes `lease_active` → `paused` → `handed_back` with no fault. The Rivian app shows each change, and its displayed times match Home Assistant's local time. **Read schedules** shows the original list. Restore the charge limit afterwards. |
| T2 Amps mid-session | While charging under a lease: 16 → 10 A, then 10 → 20 A. The increase is written once the increase interval (2 minutes) has passed since the decrease, at exactly 20 A. | Wall Connector current follows each step. Record latency. |
| T2b Continuity | Keep charging through at least two renewals (about every 12 minutes) and two current changes. | The Wall Connector contactor stays closed and the current never drops to 0 during a rewrite |
| T3 Window off now | Turn **Solar charging** off. | Charging stops. Record latency. |
| T4 Lease expiry | Turn **Solar charging** on, and once charging, turn **Lease renewal** off. Wait until `lease_minutes` + clock skew after the last write (`lease_written_at`). | Charging stops at the window end with no write: `control_state` is `lease_lapsed` and `last_write_at` is unchanged. About a minute later the adapter parks the lapsed window (`control_state` `paused`). |
| T5 Window back on now | Turn **Lease renewal** back on. **Solar charging** is still on, so the adapter writes a new lease covering now. | Charging resumes. Record latency. |
| T6 Plug in while parked | With the window parked, unplug and replug within the release delay. Then try "charge now" in the Rivian app. | Car waits after replugging. Record what "charge now" does, and whether it changes the schedule, which would make the adapter fault with `external_edit`. |
| T7 Exact restore | **Restore owner schedule** (or unplug for the release delay). | **Read schedules** matches the inventory |
| T8 Midnight | Optional, if convenient: keep a lease active across local midnight. | Charging continues past midnight until the lease ends |

If T1–T5 and T7 pass with acceptable latency, continue with [plan phase 6](PLAN.md#phases). If the car does not follow schedule changes reliably, the plan's phase 5 says what to decide.

## Faults and recovery

Entering a fault posts a Home Assistant notification. While faulted, the adapter writes nothing on its own, and any active lease lapses. **A parked or lapsed window recurs once a week until the owner's schedule is restored**, so clear faults promptly.

| Fault code | Meaning | What to do |
| --- | --- | --- |
| `external_edit` | The schedule changed outside the adapter; nothing was overwritten | Check the Rivian app. Once the owner's schedule is back, **Clear fault**. Or use `force_restore` to write the snapshot over whatever is there. |
| `conflict_after_write` | A readback matched neither the write nor the previous state | Same as above |
| `write_not_applied` | Rivian accepted writes but showed no change, repeatedly | Check the vehicle and account, then **Clear fault** |
| `write_rejected` | Rivian rejected the adapter's window, repeatedly | Check the account and the Rivian app's schedule screen, then **Clear fault** |
| `restore_rejected` | Rivian refused the owner's list when it was written back. Some accounts return entries that cannot be written back together ([upstream PR #308](https://github.com/bretterer/home-assistant-rivian/pull/308)). The snapshot is kept. | Recreate the owner's schedule in the Rivian app, then **Clear fault** |
| `adapter_window_present` | Rivian holds a window this adapter wrote, or one shaped like it, so it will not snapshot it as the owner's | Recreate the owner's schedule in the Rivian app, or `force_restore` the archived snapshot |
| `unsupported_schedule` | Rivian returned a schedule shape the adapter does not understand | Nothing was written. Report the redacted shape from **Read schedules**. |
| `force_restore_unverified` | A forced restore could not be confirmed by a readback | Check the Rivian app |

**Other conditions:**

- **Rate limits** pause writes for 5 minutes, and an account lock for 30.
- **Other errors** back off from 15 seconds up to 15 minutes.
- **An expired login** starts Home Assistant's re-authentication flow.
- **A missing mapped entity** for more than 5 minutes raises a repair issue. A missing liveness entity counts as the controller being down.
- **Controller down** blocks starts and renewals at once; `start_blocked_by` shows `controller_unavailable`. The `request_held` and `request_unconfirmed` attributes show where the controller's request stands. See [How it works](#how-it-works).

## Known limits

- **Vehicle behavior is unproven on a Gen 2.** The technique is in use: two open-source projects ([`rchav/rivian_solar_charge_ha`](https://github.com/rchav/rivian_solar_charge_ha), [`ostap-korkuna/rivian-charging-automation`](https://github.com/ostap-korkuna/rivian-charging-automation), "tested on R1S") modulate charging through the schedule amperage, and an owner reports the app's schedule amperage changing the rate mid-charge ([upstream issue #244](https://github.com/bretterer/home-assistant-rivian/issues/244)). None of them states a Gen 2 vehicle or documents how reliably a moved or lapsed window stops a session. The trial establishes that for this car.
- **Owner schedules are suspended while owned.** While owned, the owner's schedules are replaced by a single lease or parked window. A parked window recurs weekly while the adapter cannot write.
- **Lease lapse is the only cloud-free stop.** A cloud or network outage cannot stop a lease that is already active; it lapses on its own after at most `lease_minutes` + clock skew. The trial (T4) must show that a lapsing window stops a session.
- **The vehicle's time zone must match Home Assistant's.** Windows are built in Home Assistant's local time, and the setup warns if that is UTC.
- **Currents are even, from 8 to 48 A.** A request is rounded down to an even value; below 8 A the car charges at 8 A. Only the switch pauses.
- **Liveness sees a stopped controller, not a stuck one.** A controller that runs but stops deciding goes unnoticed unless its liveness entity reflects that.
- **Without the controller charging entity, some stops last until the controller asks again.** These are a liveness outage of 3 minutes or more while the controller keeps running, and a Home Assistant restart long enough for the lease to lapse.
- **A hand-edited adapter window can be taken for the owner's.** After abandon ownership, a window the adapter wrote and the owner then edited to another length is no longer recognized. The next takeover would snapshot it as the owner's list, and a second abandon would replace the archived original with it. Recreate the owner's schedule in the Rivian app before the controller takes over again.
