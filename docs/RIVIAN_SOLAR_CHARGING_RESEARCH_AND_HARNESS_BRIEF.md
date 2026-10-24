# Rivian solar charging: research and standalone schedule harness brief

> **Update, October 24, 2026.** The [solar charging architecture decision](SOLAR_CHARGING_ARCHITECTURE.md) makes evcc the policy engine for both vehicles. It changes decision 2 below: evcc, not a separate custom integration, owns solar policy and the primary UI. It relaxes decision 4: the attended trial now runs through the project's [Rivian charge adapter](RIVIAN_CHARGE_ADAPTER.md), which journals every write and restores the owner's schedule exactly; this standalone CLI brief remains the fallback. This brief remains the acceptance contract for Rivian control evidence; see [roadmap phase 3](ROADMAP.md#phase-3--rivian-attended-control-trial).

Status: planning and proof of control. Updated October 23, 2026. This document preserves the decisions from the “Rivian Solar Charging Integration” discussion and the local feasibility work. Historical observations below need a fresh check before any live test.

## Decisions and ownership

1. **Home Assistant owns the automation loop.** It already has solar generation, home consumption, grid flow, house battery, forecasts, tariffs, and departure context. Riviamigo may consume history or analytics later; it is not the charging controller.
2. **A separate future solar integration owns policy and UI.** Its scope is surplus and forecast algorithms, smoothing, charge windows, departure target, manual override, safety state, and visualization. Prefer ordinary Home Assistant entities and services as the interface to vehicle control. Do not copy another integration's config-entry tokens or depend on its private `hass.data` layout.
3. **The existing `bretterer/home-assistant-rivian` fork is the proof-of-concept location.** First establish whether Rivian charging schedules and their amperage can be read and controlled from a standalone CLI. Reuse its client and authentication patterns where suitable. Do not build the final solar integration or enable unattended automation as part of this probe.
4. **The CLI authenticates independently.** It does not run inside Home Assistant, so its first run must prompt for email, password, and MFA/OTP if requested, then store and refresh tokens securely. Home Assistant credentials must not be exported to it.
5. **Gen 2 capability is an open question.** GraphQL acceptance or schedule readback does not prove that a Gen 2 R1S changes physical current or stops charging. A schedule-based pause is a future time window, not a guaranteed zero-current stop. If a verified physical stop is required for unattended operation, use a separately proven stop-capable actuator.

This repository currently contains a workspace wrapper. A separate local HASS-Solar-Rivian implementation checkout contains an earlier combined-integration prototype and feasibility documents. That prototype is not the agreed final separation. Its September 2026 software replay and HA observations are historical evidence, not live control proof.

## Research snapshot and limits

The September 2026 control feasibility assessment and exploratory test plan in the separate local implementation checkout (`C:\Users\bball\Documents\Codex\2026-09-19\i-d-like-you-to-do\HASS-Solar-Rivian\docs`) found:

- The inspected 2025 R1S was treated as Gen 2. The installed Rivian HA integration exposed telemetry but no usable charge/current control entity. Its upstream documentation describes Gen 2 secure pairing as unsupported for remote vehicle commands.
- The installed Tesla Wall Connector supplied telemetry, with no writable current or start/stop entity. A different controllable EVSE remains an architectural option, but none was selected or proven here.
- Third-party projects use authenticated charging-schedule queries and mutations to change amperage, commonly proposing 8–48 A in 2 A steps. Those bounds and mutation semantics require verification against this account, vehicle, and API version. Other projects do not prove this vehicle's physical behavior.
- Schedule replacement can overwrite owner settings or continue charging after a controller fails. Schedule cardinality, location scope, local time zone, other writers, and exact restore matter.
- An older HA prototype and a Riviamigo upstream schedule seam had software checks, but neither establishes a live Gen 2 physical charge response. Stale Wall Connector power/current readings must not be interpreted as a verified stop.

Source leads recorded in the feasibility research: [Rivian HA integration](https://github.com/bretterer/home-assistant-rivian#remote-vehicle-control), [rchav schedule client](https://github.com/rchav/rivian_solar_charge_ha/blob/main/custom_components/rivian_solar_charging/rivian_client.py), [another schedule client](https://github.com/ostap-korkuna/rivian-charging-automation/blob/master/charging_automation/RivianAPI.py), and [HA Wall Connector integration](https://www.home-assistant.io/integrations/tesla_wall_connector/). Recheck current schemas and capabilities before treating any example as authoritative.

## Standalone CLI implementation brief

Implement or revise `tools/test_charging_schedule.py` and `tools/README-charging-schedule-test.md` in the neighboring `home-assistant-rivian` fork. That fork already has **untracked draft files** with these names as of this review. Audit and improve them in place rather than replacing them blindly. Their GraphQL operations are experimental guesses; the draft has not supplied live Gen 2 proof. Do not copy tokens or saved payloads into this repository.

### Commands and behavior

| Command | Required behavior |
| --- | --- |
| `login` | Interactive first-run email/password login using the fork's supported Rivian client flow; handle MFA/OTP, report clear failure without echoing credentials. |
| `refresh-token` | Reuse/refresh saved tokens, save rotated values atomically, and fall back to an explicit interactive re-login when necessary. |
| `whoami`, `vehicles` | Show redacted account summary and list vehicles; support explicit VIN or vehicle ID selection. An index or first vehicle is acceptable for reads only. |
| `get-schedule` | Read the provider's complete schedule set with operation/schema diagnostics, location and time-zone context, and redacted output. Distinguish zero, one, and multiple schedules. |
| `set-amps --amps N` | Default to dry-run; show a structural diff and exact proposed payload without sending a mutation. Reject an unsupported value rather than silently clamp it. |
| `set-amps --amps N --apply ...` | Require explicit vehicle identity and an acknowledgement flag, a fresh read, a local encrypted or access-restricted exact snapshot, a single bounded mutation, fresh readback, and a result classified as accepted, rejected, ambiguous, or unverified. |
| `restore --from-file ...` | Dry-run by default. For apply, require the same acknowledgement and vehicle match, compare fresh provider state to the probe-owned post-write state, restore the exact original provider payload only if safe, and verify with another fresh read. |

Use named CLI options and document exact invocation examples. Never invoke apply from login, read, or dry-run paths. Give distinct nonzero exit statuses for auth, schema, safety/precondition, mutation, verification, and restore failures. Do not interpret HTTP success alone as mutation success.

### Authentication and secret handling

- Credential precedence: explicit local cache, then `RIVIAN_ACCESS_TOKEN`, `RIVIAN_REFRESH_TOKEN`, and `RIVIAN_USER_SESSION_TOKEN`, then interactive `login`. Document the chosen source. Do not print or persist the Rivian password or OTP.
- The default cache may be `.rivian-test-auth.json`, but keep it outside tracked files and create it with user-only file permissions where the platform supports them. Use atomic replacement on refresh. On Windows, document and verify the effective ACL; do not claim POSIX mode bits secure it. Provide a `logout`/cache-delete command or instructions.
- Add `.rivian-test-auth.json`, temporary snapshots, raw responses, and local log directories to the fork's ignore rules. Refuse to write sensitive artifacts into tracked paths. No plaintext secrets in tests, fixtures, docs, command history, exceptions, or debug output.
- Redaction must be mandatory for stdout and shareable logs. Cover access/refresh/session tokens, cookies, authorization headers, email, VIN, vehicle/account IDs, coordinates, and home location names. If an exact unredacted schedule snapshot is necessary for restoration, store it in a separate access-restricted local artifact and label it sensitive. A redacted snapshot is insufficient for exact restore.
- Do not provide a casual `--no-redact` switch. If private raw capture is needed to diagnose an unknown schema, require a deliberate local-only mode, protect the file, and display a warning without exposing its contents.

### Schedule discovery and write preconditions

1. Inspect the pinned `rivian-python-client` API and the fork's HA config flow. Confirm actual GraphQL operation names, arguments, response shape, token refresh behavior, and error fields from read-only calls. Keep speculative candidate operations clearly labeled until observed.
2. Read the **complete upstream schedule collection** twice. Preserve ordering and unknown fields. Reject truncated, null, incomplete, or ambiguous entries; never select the last or first schedule for a write by convenience.
3. Identify the intended home schedule using provider IDs/location data and an explicit user confirmation that avoids printing precise coordinates. Validate local day, time zone, DST boundary, and vehicle identity. If cardinality or ownership is unclear, stop.
4. Save an exact pre-change snapshot plus a digest and run metadata before mutation. Compare another fresh upstream read to that digest immediately before writing. Pause competing automations and manual editors during the attended test.
5. Change only a known amperage field for one target while preserving all other schedules and fields. Validate against **observed** vehicle/EVSE/circuit limits; no forced override of an unknown maximum. Never use schedule disablement as a presumed stop command.
6. Treat a timeout after sending a mutation as ambiguous: re-read before retrying. Do not send repeated writes or restore over an external edit. Use request correlation where supported.
7. After mutation, compare the full returned schedule collection with the intended diff. Mark API/readback success separately from measured charging-current behavior. Restore the exact original state only while the current provider digest still matches the probe-owned state; otherwise stop and report a conflict for manual resolution.

### Attended test sequence

1. **Read only:** verify login, refresh, vehicle selection, schedule schema, full collection, and two consistent reads. Save redacted diagnostics.
2. **Unplugged round trip:** with the vehicle owner present, apply one small reversible amperage change and restore immediately. Verify the complete schedule collection after both writes. Abort if restoration fails.
3. **Plugged-in physical check:** with current schedule backed up, fresh Wall Connector or independent meter readings, and a normal manual stop available, test a conservative downward and upward amp step. Record request, API response, readback, physical current/power and timestamps. Test any schedule-based pause/resume only as a separate attended experiment; never call it a physical stop from API readback alone.
4. **Failure checks:** reject wrong vehicle, multiple schedules, unknown limits, stale telemetry, expired auth, mutation timeout, manual schedule conflict, and failed restore. Inspect what happens when the future schedule window arrives or the controller stops.
5. **Closeout:** restore exact original settings, verify them through fresh reads, confirm physical charging state, remove sensitive artifacts when no longer needed, and record unresolved differences.

Before a physical test, check circuit and vehicle constraints, disable other writers, choose an abort threshold, and ensure the owner can stop charging normally. Do not run unattended control from this harness.

### Deliverables and acceptance

- A documented standalone CLI in the existing fork, with isolated auth/cache logic and an injectable client or transport for tests.
- Fixture tests for login/MFA/refresh, missing or rotated tokens, redaction, complete schedule parsing, zero/one/multiple schedules, dry-run with zero writes, safe apply, ambiguous timeout, exact restore, and conflict refusal. Tests use synthetic account and location data.
- A local run log template with redacted request shape, HTTP/GraphQL status, operation name, timestamp, selected vehicle pseudonym, schedule digest, changed field, and verification classification. Keep private exact snapshots separate.
- A final evidence note that says which of authentication, schedule read, write acceptance, full readback, exact restoration, and **measured physical current control** passed. Record Gen 2/software/firmware versions and any unsupported behavior without credentials or identifying data.

Success for the proof of concept requires an observed, repeatable physical current change and safe exact restoration. If only API reads/writes work, record that narrower result. The future Home Assistant solar integration design should begin only after this control boundary and its failure behavior are understood.

