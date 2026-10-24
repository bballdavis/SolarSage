# Solar EV control: exploratory evidence and commissioning plan

Status: **software replay passes; live charging control unproved**. This
plan separates observed Home Assistant state, replay tests, and tests that require
an attended charging session. It applies to one Tesla Wall Connector shared by a
Tesla vehicle and a 2025 Gen 2 Rivian R1S. No vehicle or charger command was sent
while preparing the September 25, 2026 inventory.

## Control decision

| Approach | Control available here | Decision |
| --- | --- | --- |
| Command the current Wall Connector | Its installed Home Assistant integration exposes measurements and connection/contact feedback, with no writable charge or current entity. | Use it as a meter and physical feedback source. It cannot be the actuator for this build. |
| Command the Tesla vehicle | The installed `tesla_custom` integration exposes `switch.roxxo_charger` and `number.roxxo_charging_amps` (0–48 A, 1 A steps). Tesla's documented vehicle API has start, stop, and set-current commands. | First candidate for an attended Tesla-only commissioning run. Home Assistant entity presence is not yet command or physical proof. |
| Command the Rivian vehicle | The installed Rivian integration has telemetry but no charge switch. The Riviamigo seam under test supports schedule GET/PUT, including `enabled`, time window, and amperage; it is not an immediate start/stop command. Its upstream parser may receive 0, 1, or many schedules, while the cached GET currently exposes one row and the refresh path uses the last upstream entry. The live HA installation currently reports no Rivian schedule services. | Keep Rivian in shadow mode. Proceed only through the [schedule modulation trial](RIVIAN_SCHEDULE_MODULATION_TRIAL.md) after the exactly-one schedule gate passes. A schedule write/readback still cannot qualify as a physical stop. |
| Use a controllable EVSE for both vehicles | A local EVSE with explicit enable/disable, current limit, measured output, and a documented recovery policy would control the common charging point regardless of car. | Preferred route to unattended two-vehicle control if hardware change is acceptable. Test one controller/actuator owner at a time. |

Tesla's Wall Connector Dynamic Power Management regulates to a panel capacity
using an approved meter; it is not an exposed solar-surplus start/stop API. A
meter-emulation workaround is not a commissioning substitute. EVCC has mature
solar and home-battery policy, but its own vehicle guidance says it normally
controls through a capable wallbox; it cannot make this installed connector
writable. If a different EVSE is considered, evaluate its local API, electrical
approval, feedback cadence, offline state, and manual override before choosing
between EVCC and this custom controller. Do not run both as competing writers.
The installed `tesla_custom` integration is a separate command path from HA's
official Tesla Fleet integration; the Fleet API documentation establishes that
vehicle commands exist, not that this installed path has been commissioned.

Sources: [HA Wall Connector](https://www.home-assistant.io/integrations/tesla_wall_connector/),
[Tesla vehicle commands](https://developer.tesla.com/docs/fleet-api/endpoints/vehicle-commands),
[Tesla Power Management](https://www.tesla.com/support/charging/wall-connector/power-management),
[2025 R1S owner's guide](https://assets.rivian.com/2md5qhoeajym/7aH3Lwf2vJJvjhklXorrga/e52c521f954216eb3358824f5e6b12d7/r1s-og-my25-en-us-20240722.pdf),
[Rivian HA remote-control limitation](https://github.com/bretterer/home-assistant-rivian#remote-vehicle-control),
[EVCC vehicle control model](https://docs.evcc.io/en/features/vehicle/), and
[HA OpenEVSE capability example](https://www.home-assistant.io/integrations/openevse/).
The last link is an example of the required control surface, not a purchase choice.
The Rivian owner's guide also says a charge schedule applies at its saved location
and can retain its chosen amperage for future sessions. This is why a schedule
mutation is treated as persistent charging policy, not a temporary stop command.

## Current installation readback

Read-only `homeassistant-ai/ha-mcp` inspection at approximately 21:56 CDT on
September 25, 2026 reported `ha-mcp 8.5.0.dev2732`. A follow-up read-only HA MCP
diagnostic on September 26, 2026 confirmed that the `tesla_wall_connector`
integration entry is loaded, while the HA system log recorded 12 coordinator
fetch timeouts. The latest timeout was at `2026-09-25 11:52:43 CDT`. Wall
current/power were last reported at approximately 11:53 CDT, so H1 remains
blocked by a source connectivity/update issue. No connector address was
collected or retained. The direct HA tools were not
mounted in this task; the configured HASS-MCP project's protocol probe reached
the server. The active integrations are `rivian`, `tesla_custom`, and
`tesla_wall_connector`. `ha_list_services(domain="rivian")` returned zero
services, so the schedule services in **this checkout** are not installed in HA.

| Signal | Observed state and native unit | Last reported, CDT | Interpretation |
| --- | --- | --- | --- |
| Grid / PV / battery power | +1,440 / 13 / +40 W | Within seconds of read | Nighttime import; no solar test condition. Positive battery power means charging per the prior sign check. |
| House battery SOC | 52% | 20:25 | Stale under the current 300 s SOC gate. |
| Wall power / current | 0.0 kW / 0.0 A | 11:53 | Too old to prove present idle power or a future stop. |
| Wall grid voltage | 241.4 V | 21:59 | A fresh voltage report does not refresh power/current. |
| Wall connected / contactor | off / off | 11:53 | Old state; cannot prove current physical disconnection. |
| Tesla charge switch / current setting | off / 48 A | 13:28 | Writable entities exist, but their latest reports are too old for command feedback. |
| Rivian connection / charging | off / off | September 21, 08:25 | Too old for an unplugged safety precondition. |

The entity inventory found 20 Wall Connector entities, all sensors or binary
sensors. Its firmware readback was `26.26.10+g42855ec9b9cd1`. The observed
states are fixture inputs and diagnostic evidence, not a simulated assertion
that a vehicle actually obeyed a command. Preserve native units and
`last_reported`; do not stamp retained zero values with the time another sensor
updated. The observed capture excludes real VINs, account IDs, email addresses,
coordinates, and tokens. Synthetic schedule-validator cases use nonidentifying
coordinate placeholders because location is a required schedule field.

The pinned local HA Core implementation polls Wall Connector vitals every 30 s
and writes entity state on each successful coordinator update, including an
unchanged zero. It does not special-case zero current or power. If the deployed
integration matches that implementation, an old `last_reported` for both
signals is an unresolved source/runtime problem, not normal unchanged-value
behavior. H1 must recheck repeated power, current, connection, contactor, and
voltage reports while unplugged and retain the 75 s wall-power freshness gate.

### Riviamigo schedule cardinality gate

The schedule-only hypothesis assumes the vehicle has one main schedule. That is
an acceptance condition to measure, not an assumption to encode: the upstream
response may contain zero, one, or many entries; the current Riviamigo GET cache
holds one row and the refresh parser selects the last entry. Therefore the trial
must stop unless a fresh upstream read returns **exactly one** schedule and the
cached/readback representation matches it. Zero or multiple entries are a
cardinality failure and must not be “fixed” by choosing an arbitrary row.

The PUT contract can change `enabled`, the start/duration window, and amperage.
Those fields are sufficient to test modulation, but a successful response is not
physical evidence. Preserve the complete returned schedule and a hash locally,
verify ownership before every write, and restore only when the current schedule
still matches the trial's owned hash. No live Riviamigo call was made during this
inventory; the live HA installation also returned no Rivian schedule services.
The newly prepared direct-read route is
`GET /v1/vehicles/{id}/charging-schedule/upstream`, but it is local and
undeployed; it is not live Riviamigo evidence. R1 remains blocked until that
route is deployed, an authenticated session is available, and two fresh
authenticated reads agree.

### HA controller versus the attended Riviamigo protocol

The current HA controller is not the implementation of the R1–R3 experiment. It
builds a 10-minute, today-only schedule from HA coordinates, replaces the
schedule list, and cannot express the proposed daylight-wide modulation or an
outside-window pause. Its cleanup clears the journal after the restore write and
hash check; it does not perform the protocol's two independent fresh restore
GETs. Current replay coverage stubs the schedule builder and stop feedback, so
it proves coordinator journal sequencing only. R1–R3 are a separate, attended
Riviamigo-based experiment and must not be run through the HA controller as it
stands.

## Exploratory test layers

| ID | Test and evidence to retain | Pass condition | Current status |
| --- | --- | --- | --- |
| F1 | Replay the sanitized night snapshot through the real state-age helper and decision engine, then the HA input reader. | Stale wall/SOC/vehicle signals block actuation; fresh grid/voltage cannot make them fresh. | State-age, engine, and HA reader replays pass. An actual fresh charging trace remains. |
| F2 | Replay fresh Tesla-only, Rivian-only, both-connected, neither-connected, manual override, full battery, and lost-location timelines. | At most one selected vehicle; no command on ambiguous or missing identity; Rivian remains blocked without a bound physical stop. | Single/both/neither selection, dry-run manual preview, real non-dry-run Tesla readiness refusal without a commissioned stop binding, and Rivian missing/uncommissioned stop-binding cases pass; full-battery/location timelines remain. |
| F3 | Replay 120 s surplus entry, upward ramps, immediate power drops, short/repeated clouds, hard import, cooldown, and forecast-source expiry at different report cadences. | Existing bounds and hysteresis remain true independent of evaluation cadence. | Existing unit tests, active-to-stale transition, and a five-step ramp/cloud/import replay pass; repeated-cloud and cadence variants remain. |
| F4 | Replay accepted-but-ignored OFF, stale zero power, distinct post-command low-power reports, mismatched amp readback, and restart with a saved journal. | No command acknowledgement is mistaken for physical proof; cleanup remains recoverable. | Existing HA doubles and new stale-zero case pass locally. |
| F5 | Replay complete, empty, disabled, metadata-bearing, malformed, partial-error, and externally edited Rivian schedule responses. | Preserve the complete owner schedule; never overwrite an external edit; exact restore and hash readback before clearing the journal. | HA-side shape/full-list and ownership cases pass. Riviamigo's synthetic GraphQL GET/PUT/error envelope tests pass, but its production refresh discards all but one schedule; database round trip, partial-data errors, and live Gen 2 envelope remain blocked. |
| H1 | Read-only HA inventory and repeated fresh polling while each vehicle is unplugged. | Units, entity registry ownership, sign, report cadence, and actual disconnected state are independently current. | Initial inventory done; freshness unresolved. |
| H2 | Compare Sol-Ark grid power with an independent meter while a known load changes. | Sign and CT placement cover EV load; wall W/kW scaling and voltage phase configuration agree with measured current. | Pending attended measurement. |
| T1 | Tesla unplugged command/readback/restore with automation paused and original amps recorded. | Supported command reaches the intended car, original setting is restored, no charging begins, and errors/latency are recorded. | Pending car at home and fresh HA reports. |
| T2 | With the Tesla already charging and automation paused, command the commissioning OFF while the owner observes the charger. | Fresh wall power was above idle before the command; switch OFF and two distinct post-command low-power wall reports prove a physical stop. | Pending a connected, attended active session and H1/H2/T1. |
| T3 | After T2, supervise an 8 A start, current steps up/down, surplus-loss stop, and manual override. | Each change is visible in fresh wall A/kW; no excess import beyond policy limits; stopped power stays low after cleanup. | Pending daylight, sufficient surplus, and T2. |
| R1 | Fresh upstream schedule read and cardinality check through the prepared `GET /v1/vehicles/{id}/charging-schedule/upstream` route. | Exactly one schedule, with the required location/window/amperage/enabled fields; cached GET and readback identify the same row. | Blocked: route is local/undeployed; deployment, authenticated session access, and two agreeing fresh authenticated reads are required. |
| R2 | Unplugged snapshot → bounded write → readback → exact restore. | The original one schedule is restored at all control fields; ownership/hash conflicts retain a journal. | Pending R1 and an attended window. This proves schedule compatibility only. |
| R3 | [Attended Rivian schedule modulation trial](RIVIAN_SCHEDULE_MODULATION_TRIAL.md): a separate Riviamigo experiment, not the current HA controller, after two agreeing direct upstream reads and local timezone/weekday verification, using a short 20–30 minute window for connected 8 → 12 → 8 A, window pause/resume, restore, and failure handling. | Two matching restoration readbacks; each 8/12/8 A state settles within ±1 A for 3 consecutive fresh samples within 120 s with an approximately 4 A directional change; pause is <0.5 A and <200 W for 3 fresh samples; resume meets the 8 A criterion. | Not run; wall and Rivian reports are stale and no physical test has passed. |
| R4 | Only after a supported physical stop actuator exists, run active start/current/stop, power-loss, restart, and external schedule-edit recovery. | Fresh physical feedback and recovery pass for Gen 2; no recurring lease can resume unexpectedly after controller loss. | Blocked by current hardware/control capability. |

For live tests, record the command time, HA service response, car-reported state,
wall current/power and their report times, grid/battery power, actual observed
latency, original settings, restore readback, and any manual action. Distinguish
the state returned by an API from watts observed at the charger. Keep sensitive
vehicle/account/location fields out of the retained trace.

The latest worker full pinned runs after two additional replay tests are:
**130 passed** on Python 3.13 / HA 2025.1.4 and **130 passed** on Python 3.14 /
HA 2026.9.3. The frontend contract check passes. These are simulation and local
HA object tests; neither runtime drove the installed charger or either car. The
September 19 baseline remains separately recorded in [verification evidence](VERIFICATION.md)
as 105 passed on each pinned runtime. Full Rivian GraphQL response fixtures need
an authenticated, redacted live capture before they can be called representative
of this Gen 2 vehicle. Separately, three targeted Riviamigo Rust tests pass
against a synthetic local GraphQL server. They exercise its transport and
deserialization types, not the database upsert/refresh path or a Rivian server.

## Attended run sequence and aborts

1. Leave solar control disabled and dry-run enabled. Resolve the stale-source
   issue from H1 without relaxing freshness limits merely to pass a test. Verify
   the grid CT includes the charger and that the electrical phase setting makes
   wall power consistent with voltage and current.
2. Replay fixtures and the pinned Home Assistant test environments. Run shadow
   decisions against representative sunny/cloudy periods and compare proposed
   amps with measured available watts. This stage sends no charge commands.
3. Test Tesla T1, then T2 with the owner present, the vehicle below its normal
   limit, no conflicting car/wall schedule, and an accessible manual stop. Save
   original amps/limit/schedule before changing any setting. T2 begins with
   observed active wall draw and never restarts charging. Only after its physical
   stop proof passes, run T3 beginning at 8 A and wait for fresh wall reports
   at each step. Test one vehicle at a time.
4. Run R1, then the unplugged R2 round trip. If the fresh response is not exactly
   one schedule, stop and preserve the response for diagnosis. A successful R2
   does not unlock Rivian live mode; keep its normal schedule intact after exact
   readback and preserve any conflict for recovery.
5. Run the attended R3 protocol only with an owner present, fresh wall/Rivian
   reports, and a tested manual stop. Use a short 20–30 minute window well away
   from local midnight and DST transitions for the first connected experiment.
   Consider a daylight-wide window only after short-window expiry and fail-open
   behavior are measured. Full-day operation requires a physical stop or an
   explicitly accepted fail-open risk.
6. For two-vehicle unattended charging, obtain a supported stop/current actuator
   and test R3/T2 on that same charging point. Verify its behavior on network
   loss and Home Assistant restart before enabling automatic starts.

Abort an active run if identity becomes ambiguous, a report is stale, import or
battery discharge crosses the configured hard bounds, commanded OFF does not
produce fresh low wall power, a schedule restore differs, or the owner cannot
observe the charger. Stop the run through the tested physical/manual path and
preserve the journal and snapshots for diagnosis. A cloud controller cannot
guarantee a stop during host/network failure; the EVSE and installation retain
electrical safety responsibility.

## Release gate

The custom integration may be offered as a **Tesla-only attended beta** after
F1–F4, H1–H2, and T1–T3 pass with recorded evidence. Rivian may leave shadow
mode only after R1–R3 and a supported physical stop path pass. Two-vehicle
unattended use additionally needs conflict ownership, restart/network recovery,
manual override, and a single active actuator owner verified on the installed
hardware. Automated fixture success alone does not satisfy any live gate.
