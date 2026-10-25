# Rivian charge adapter

**What it is.** The Rivian charge adapter (`custom_components/rivian_solar_adapter`) gives a controller a start/stop switch and a current setting for a Gen 2 Rivian, which has no usable remote charging command. It does this by writing the vehicle's **charging schedule** through Rivian's API. In the [working plan](PLAN.md) the controller is the solar planner. The adapter was built while evcc was the planned controller, so some option and entity names still say `evcc`; the plan lists what changes when the adapter is adapted to the planner.

**Status: software-tested only.** Four rounds of independent adversarial review shaped the design, including closed-loop simulations against evcc's charging loop. Its tests run against Home Assistant 2026.10.3 with a simulated Rivian provider. It has not been run against a real account, vehicle, or Wall Connector. **It installs in dry-run mode, and nothing is written until you turn dry run off.**

## How it works

| The controller asks for | The adapter writes | Expected vehicle behavior (to be confirmed by the [trial](#attended-trial)) |
| --- | --- | --- |
| Charging on, at N A | A **lease**: one window from 5 minutes ago to `lease_minutes` + 5 minutes from now, at N A rounded down to an even 8–48 A. The 5-minute padding absorbs clock differences. | Charges at N A |
| A lower current | The same lease at the lower amperage, at the next allowed write | Current drops |
| A higher current | Only once N − 3 A reaches the next even step, and at most once per increase interval after the last current change. A request of 48 A goes straight to 48, except for 30 minutes after the current dropped from 48 to 44 or 46 A. Both rules were tuned for evcc, which adds up to 2 A to its requests and chases surplus; the planner asks for exact currents, so both are to be switched off for it ([plan](PLAN.md#repository-changes)). | Current rises |
| Charging off | A **parked window** at 8 A that ended 5 minutes ago, so no window covers now | Waits |
| Nothing new | Renews the lease when 8 minutes remain, but **only** while the car is known plugged in at home and the controller is alive | Keeps charging |
| Nothing new, and the lease cannot be extended (the controller looks down, the car's state unknown, or renewal off) | Nothing: the window ends on the vehicle by itself, `lease_minutes` + 5 minutes after it was written. That is the cloud-free stop. A minute later a parked window replaces it, so it cannot recur a week later. | Stops at the window end |
| Nothing, and the controller has looked down for 3 minutes | Its request from before the outage is **held** and the window is parked. One unavailable reading is not enough. Until the liveness entity has been seen available since Home Assistant started, the wait is 10 minutes. Any request the controller makes meanwhile, changed or not, shows it is running and confirms its request. | Stops |
| The controller is back after looking down | It may have restarted, and then it asks again if it wants charging. The **charging enabled** entity tells: on means the controller still wants this car charging, so a held request resumes; off drops it. Without that entity, a held request is dropped. A shorter gap keeps the request. | Resumes, or waits for the controller |
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
   - **The controller's request after a restart** is unconfirmed, because the controller may have restarted too (a power cut). It keeps a lease that is still live running, but it never starts charging or takes over the owner's schedule. Any request from the controller confirms it, and so does the **charging enabled** entity being on; off drops it. Without that entity, a request whose lease lapsed meanwhile is dropped 30 seconds after the controller is seen up, and the controller asks again if it wants charging.
   - **A failed journal save of a request** is reported to the caller, but the request is kept: saves queued behind it already hold it. Until a save succeeds, a repeated request is saved again rather than acknowledged.
   - **An interrupted write** is resolved by reading Rivian. A write is recorded as sent as soon as Rivian answers, so a restart before it settles still recognizes it if it lands late. A write that may or may not have been sent before a crash is treated the same way.
   - **If setup fails while the adapter owns the schedule**, for example because Rivian is unreachable, a persistent repair issue says so. Removing the integration restores the schedule.
   - **If a disable or removal cannot restore the owner's schedule**, a persistent repair issue says so and survives restarts. It goes away when the adapter is enabled again or added again for the vehicle, which resumes the restore.
   - **A change of Home Assistant's time zone** reloads the adapter, which then rewrites its window in the new local time. A change of the home location takes effect at the next reload or restart.

**While the adapter owns the schedule, the owner's own schedules are suspended.** Overnight charging then comes from the planner's night charging and plans, not from the owner's Rivian schedule.

## Install and configure

1. Install upstream [`home-assistant-rivian`](https://github.com/bretterer/home-assistant-rivian) 2.x for telemetry, if you have not already. The adapter logs in to Rivian separately, because upstream offers no schedule services; only session tokens are stored.
2. Install this repository through HACS (custom repository, category integration), or copy `custom_components/rivian_solar_adapter` into `/config/custom_components/`. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → Rivian Solar Charge Adapter.**
   - **Login:** Rivian email and password, plus the one-time code if your account uses one.
   - **Vehicle:** pick the R1S.
   - **Entities:**

     | Option | Map to | Required |
     | --- | --- | --- |
     | Plugged in | The Rivian charger connection binary sensor | Yes |
     | Location | The Rivian device tracker (not a person) | Yes |
     | Charger state | The Rivian charger state sensor | Recommended: without it the on-charger status can never be "charging" (`c`) |
     | Wall Connector vehicle connected | `binary_sensor.tesla_wall_connector_vehicle_connected` | Recommended: it ends a session when the Wall Connector sees no vehicle, and blocks control of a Rivian plugged in elsewhere |
     | Controller liveness (`evcc_liveness_entity`) | An entity that is unavailable while the controller is down. The planner will provide one; leave it empty until then. | **Strongly recommended** once a controller runs: without it, a controller outage cannot stop renewals |
     | Controller charging enabled (`evcc_enabled_entity`) | A binary sensor that is on while the controller wants this car charging. The planner will provide one. | **Recommended** with liveness: it lets charging resume after an outage of the liveness entity, and after a long Home Assistant restart |
     | Vehicle override | An input select naming the car on the Wall Connector (`auto`, `tesla`, `rivian`) | Optional: when it names another car, the on-charger status reports `a` |

4. Options (**Configure**):

   | Option | Default | Meaning |
   | --- | --- | --- |
   | Dry run | **on** | Write nothing, except restoring the owner's list if the adapter still owns it. Shows the would-be schedule. |
   | Lease minutes | 20 | How long a charging window lasts without renewal. This is the fail-safe horizon (plus the clock skew). |
   | Renew before (minutes) | 8 | Renew when this little time remains |
   | Clock skew (minutes) | 5 | Padding at both ends of a lease, for a vehicle clock that differs from Home Assistant's |
   | Minimum write interval (s) | 30 | Spacing between current changes and renewals. Start and stop use 5 s. |
   | Increase interval (s) | 120 | Minimum spacing before raising the current |
   | Release delay (s) | 60 | How long unplugged must last before restoring the owner's schedule. A replug within it keeps the window parked until the controller asks again. |

## Entities

The examples assume the config entry is titled `R1S`; your entity IDs follow your vehicle's name.

| Entity | Purpose |
| --- | --- |
| `switch.r1s_solar_adapter_solar_charging` | Charging requested by the controller |
| `number.r1s_solar_adapter_solar_current` | Requested current. Accepts 0–80 and applies even 8–48 A. |
| `sensor.r1s_solar_adapter_evcc_status` | On-charger status: `a` (not at the Wall Connector), `b` (connected) or `c` (charging) |
| `sensor.r1s_solar_adapter_control_state` | One of the control states below. Its attributes show the lease, last write, fingerprints (prefixes only), errors, observation and dry-run target. |
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

## Attended trial

The adapter is also the safest tool for the attended Gen 2 trial ([plan phase 5](PLAN.md#phases)). Every step is journaled and restorable, unlike editing schedule entities by hand. Keep the planner from commanding the Rivian during the trial, so that you are the only writer.

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
| T2 Amps mid-session | While charging under a lease: 16 → 10 A, then 10 → 20 A. Because of the 3 A margin, a 20 A request yields a 16 A lease. | Wall Connector current follows each step. Record latency. |
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
- **Controller down** blocks starts and renewals at once; `start_blocked_by` shows `evcc_unavailable`. The `request_held` and `request_unconfirmed` attributes show where the controller's request stands. See [How it works](#how-it-works).

## Known limits

- **Vehicle behavior is unproven on a Gen 2.** The technique is in use: two open-source projects ([`rchav/rivian_solar_charge_ha`](https://github.com/rchav/rivian_solar_charge_ha), [`ostap-korkuna/rivian-charging-automation`](https://github.com/ostap-korkuna/rivian-charging-automation), "tested on R1S") modulate charging through the schedule amperage, and an owner reports the app's schedule amperage changing the rate mid-charge ([upstream issue #244](https://github.com/bretterer/home-assistant-rivian/issues/244)). None of them states a Gen 2 vehicle or documents how reliably a moved or lapsed window stops a session. The trial establishes that for this car.
- **Owner schedules are suspended while owned.** While owned, the owner's schedules are replaced by a single lease or parked window. A parked window recurs weekly while the adapter cannot write.
- **Lease lapse is the only cloud-free stop.** A cloud or network outage cannot stop a lease that is already active; it lapses on its own after at most `lease_minutes` + clock skew. The trial (T4) must show that a lapsing window stops a session.
- **The vehicle's time zone must match Home Assistant's.** Windows are built in Home Assistant's local time, and the setup warns if that is UTC.
- **Increases land 3 A below the request** (below 48 A) until the evcc-era margin is switched off for the planner.
- **Liveness sees a stopped controller, not a stuck one.** A controller that runs but stops deciding goes unnoticed unless its liveness entity reflects that.
- **Without the charging enabled entity, some stops last until the controller asks again.** These are a liveness outage of 3 minutes or more while the controller keeps running, and a Home Assistant restart long enough for the lease to lapse.
- **A hand-edited adapter window can be taken for the owner's.** After abandon ownership, a window the adapter wrote and the owner then edited to another length is no longer recognized. The next takeover would snapshot it as the owner's list, and a second abandon would replace the archived original with it. Recreate the owner's schedule in the Rivian app before the controller takes over again.
