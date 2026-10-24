# Rivian schedule modulation trial

Status: **protocol only; no physical test has passed**. This is an attended
compatibility experiment for the hypothesis that one Rivian charging schedule
can stay enabled while its current setting is adjusted to follow excess solar.
It does not certify unattended charging, a physical stop, or fail-closed
behavior.

The [2025 R1S owner's guide](https://assets.rivian.com/2md5qhoeajym/7aH3Lwf2vJJvjhklXorrga/e52c521f954216eb3358824f5e6b12d7/r1s-og-my25-en-us-20240722.pdf)
describes a saved-location charge schedule and its charging amperage. The
Riviamigo PUT under test accepts `enabled`, a start/duration window, and
amperage. Its GET/cache boundary is conditional: the upstream response may have
zero, one, or many schedules, while the current cache exposes one row and the
refresh path selects the last entry. The trial is allowed to write only when a
fresh upstream read returns exactly one schedule. The intended direct-read path
is `GET /v1/vehicles/{id}/charging-schedule/upstream`; it is currently prepared
locally but undeployed, so it is not live Riviamigo evidence. R1 remains blocked
until deployment, authenticated session access, and two agreeing fresh
authenticated reads are available.

This protocol must not be run through the current HA controller. That controller
builds a 10-minute, today-only schedule from HA coordinates, replaces the list,
and cannot perform the proposed daylight modulation or outside-window pause. Its
cleanup clears the journal after the restore write and hash check instead of the
two independent fresh restoration GETs required here. Current replay stubs the
schedule builder and stop feedback; those tests prove coordinator journal
sequencing only. R1–R3 therefore use the separate Riviamigo path and remain
attended compatibility evidence.

## Riviamigo field mapping

Use the provider's weekday enum unchanged. The cached GET is not authoritative;
after every PUT, perform a direct upstream GET and compare the controlled fields.

| Rivian schedule field | Riviamigo request/read field |
| --- | --- |
| `startTime` | `start_time_minutes` |
| `duration` | `duration_minutes` |
| `location.latitude` | `location_lat` |
| `location.longitude` | `location_lng` |
| `weekDays` | `week_days` |
| `enabled` | `enabled` |
| `amperage` | `amperage` |

Preserve the exact provider weekday values and restore every controlled field,
including the original weekday and location. A direct upstream readback is
required after the temporary PUT, each amperage/window PUT, and the restore PUT;
the two matching restore readbacks remain required before clearing the journal.

Before **every** PUT, perform a fresh direct upstream GET and compare the owned
controlled-field hash with the last accepted readback. Abort on any mismatch;
do not overwrite an edit made by the Rivian app or another client. Riviamigo's
current PUT has no atomic expected-hash or `If-Match` guard, so this preflight
narrowly reduces the race window but cannot eliminate an edit that lands between
the GET and PUT. Keep competing edits paused during the attended trial. Perform
manual rollback only after ownership is reconciled, and preserve the journal on
any unresolved conflict.

## Preconditions and safety

1. Use an owner-approved, attended window. Pause solar automation and competing
   vehicle/charger automations. Keep a manual Rivian stop and the electrical
   disconnect path accessible.
2. Confirm fresh, independent wall current/power and Rivian connection/charging
   reports. Current installation evidence is stale, so a retained zero is not an
   unplugged or stopped proof. Do not proceed on old timestamps.
3. Confirm the vehicle is at the schedule's saved location and the vehicle and
   wall are initially disconnected for the write/readback portion. Record the
   vehicle's normal charge limit separately; never use a real VIN, account ID,
   token, coordinates, or location in the retained worksheet.
4. Use a short 20–30 minute trial window for the first connected experiment.
   Choose a window well away from local midnight and daylight-saving transitions,
   and set its temporary weekday to include the verified local current weekday.
   A daylight-wide window is
   a later option, only after short-window expiry and fail-open behavior have
   been measured. A full-day window must be treated as fail-open: if the
   controller dies, the enabled schedule may continue to permit charging. Do not
   call that safe without a physical stop or explicit acceptance of the risk.

## Trial sequence

### 1. Fresh read and snapshot

Record timestamps in one timezone and retain the raw redacted response, parsed
schedule, and canonical hash.

| Check | Required result | Abort if |
| --- | --- | --- |
| Upstream GET | Two direct, fresh upstream reads agree on exactly one schedule | Timeout, auth error, malformed response, stale cache, or disagreement |
| Cardinality | Both reads contain exactly one schedule | Either read contains zero or more than one schedule |
| Local time | Start, duration, weekday, and saved location are interpreted in the home's local timezone; weekday is explicitly checked | UTC/local conversion, midnight crossing, DST ambiguity, or wrong weekday |
| Fields | `enabled`, start time, duration, location scope, and amperage are present | Any field is missing, ambiguous, or unexpectedly transformed |
| Identity | Cached GET/readback refers to the same schedule as both upstream reads | Cache selects a different row or only a last-entry heuristic can be shown |

Save the complete original schedule as `original_schedule`, including its exact
original weekday and location coordinates, plus its hash, both upstream read
timestamps, and the verified local timezone/weekday. This complete recovery
snapshot belongs in private, restricted local/HA storage only. The retained
worksheet and operational logs must exclude coordinates and other sensitive
location/account fields.

### 2. Unplugged bounded write/readback/restore

With the vehicle and wall confirmed disconnected, create a trial schedule from
the snapshot. Keep the original location, and set the temporary weekday to
include the verified local current weekday. Set `enabled=true`,
use the short 20–30 minute window containing the current local time, and retain
an amperage that is valid for the vehicle. Write once, then perform a fresh GET
and compare all controlled fields. Record request, response, readback, hashes,
and elapsed time. `enabled=false` is not assumed to be a stop and is not physical
evidence; do not use it as the sole pause proof.

Immediately restore `original_schedule`. Perform two separate fresh reads and
require both to match the controlled fields and hash before clearing the local
journal. If the write is rejected, readback differs, or another edit changes the
hash, do not retry blindly; preserve the journal and restore only after ownership
is re-established. A full hash can conservatively report a conflict when only
server-managed metadata drifted; record that as a hash conflict and do not clear
the journal until the controlled fields and ownership are explicitly reconciled.

### 3. Connected current modulation

Reconnect the vehicle only after the unplugged round trip passes. Start with
solar control paused and the owner watching the wall and vehicle. Use the same
enabled schedule and the short 20–30 minute window containing the current local
time. Do not begin this first experiment with a daylight-wide or full-day window.

| Phase | Action | Observe for at least |
| --- | --- | --- |
| Baseline | Begin at 8 A and confirm charging is active | Within 120 s, fresh wall current settles to 8 ±1 A for 3 consecutive samples |
| Increase | Write 12 A while keeping the same window and enabled state | Within 120 s, fresh wall current settles to 12 ±1 A for 3 consecutive samples, with an approximately 4 A increase from baseline |
| Decrease | Write 8 A with the same window and enabled state | Within 120 s, fresh wall current settles to 8 ±1 A for 3 consecutive samples, with an approximately 4 A decrease |
| Window pause | Move the end of the window before the current time, or otherwise set a window outside now, then read back | Within 120 s, fresh wall current is <0.5 A and power <200 W for 3 consecutive samples; `enabled=false` alone does not pass |
| Resume | Restore a window containing now, retain 8 A, and read back | Within 120 s, fresh wall current settles to 8 ±1 A for 3 consecutive samples |

For every step record commanded amperage, schedule readback, wall current and
power with their own report timestamps, vehicle charging state, grid and battery
power, command latency, and any manual intervention. Do not infer physical
behavior from a successful API response or a schedule readback. A 120 second
sample is the minimum observation window, not a promise that the car reacts in
that time.

### 4. Restore and failure behavior

After the connected sequence, use the manual Rivian stop, disconnect the vehicle,
and verify fresh wall power/current are low. Only then restore the exact original
schedule, including its original weekday and location, and perform two matching
fresh restoration readbacks before clearing the journal. Restoration itself is
never a physical stop. If the original schedule has `enabled=false`, restoring it
may permit ordinary immediate charging after reconnection; do not treat that
setting as a stop. Then, while attended and with the manual stop ready, exercise
one failure at a time:

- stop the controller after a temporary write and verify whether the enabled
  window continues charging;
- let a deliberately short trial window expire and observe whether charging
  pauses rather than resumes on the next recurring schedule boundary;
- interrupt before restore, restart the controller, and verify the journal
  drives recovery before any new write;
- make an external schedule edit and confirm the controller detects the hash
  conflict and does not overwrite it.

Abort immediately for unexpected current, hard grid import or battery discharge,
stale wall/Rivian reports, ambiguous vehicle identity, a failed readback, a
window transition that does not produce the expected observed behavior, loss of
manual stop access, or any restore mismatch. In an aborted run, use the manual
Rivian stop first, disconnect the vehicle, and verify fresh low wall
power/current. Then reconcile or restore only when the journal still establishes
ownership; preserve the journal and redacted evidence and leave automation
disabled. `enabled=false` is not a substitute for the manual stop or
disconnection.

## Evidence worksheet

| Field | Entry |
| --- | --- |
| Trial date/time and timezone | |
| Software/firmware versions | |
| Original schedule hash/read time | |
| Cardinality and identity result | |
| Temporary window and enabled state | |
| 8 A baseline: wall A/kW and report times | |
| 12 A step: wall A/kW and report times | |
| 8 A step: wall A/kW and report times | |
| Outside-window pause result | |
| Resume result | |
| Failure/expiry/restart result | |
| Restore request/readback/hash | |
| Abort or manual actions | |
| Evidence file references, redacted | |

## Acceptance

The trial passes only if two direct upstream reads agree on exactly one schedule,
the home-local timezone and weekday are verified, the unplugged write/readback/
restore round trip has two matching restoration reads, each 8 → 12 → 8 A change
settles within ±1 A for 3 consecutive fresh samples within 120 seconds with the
expected approximately 4 A directional change, pause produces <0.5 A and
<200 W for 3 fresh samples within 120 seconds, resume meets the same 8 A
criterion, failure behavior and journal recovery are recorded, and the original
schedule is restored. This remains attended compatibility evidence. It does not
prove unattended safety or remove the requirement for a supported physical stop
actuator; full-day schedule control carries accepted fail-open risk when that
actuator is absent.
