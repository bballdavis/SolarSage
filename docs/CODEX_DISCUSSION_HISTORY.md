# Codex discussion history

This file preserves project decisions and evidence from relevant conversations. It summarizes the work; it is not a transcript. Thread IDs are included so the discussions can be reopened in Codex or ChatGPT. Assertions about installation state and test results were made at the time of those conversations and require fresh verification before a live trial.

## 1. Original HA solar charging investigation

Codex thread ID: `01a0ba7a-218f-7f41-8443-aa26b1c77824`  
Workspace at the time: separate HASS-Solar-Rivian implementation checkout.  
Topic: existing “charge on sunshine” projects, actual HA entities, Rivian schedule and Tesla Wall Connector feasibility, then prototype controller/UI.

Decisions and outcomes:

- HA was the natural place for the charging loop because it already owns solar, grid, battery, forecasts, and departure context.
- The Rivian schedule API was the best software-only lead to test. A future schedule window was explicitly not treated as a zero-current stop.
- The installed Wall Connector supplied telemetry; its normal HA integration did not expose writable current or start/stop. Meter-emulation and controllable EVSE options were researched, with firmware and fallback concerns.
- A combined-integration commissioning beta added forecast logic, conservative feedback handling, guided UI, sidebar/card work, and local tests. Local commits `93e35cd`, `037bbc5`, and `6ab50e6` cover that code path.
- The conversation explicitly left physical Gen 2 and Wall Connector control unproved and did not claim deployment.

Primary outputs copied here: [control feasibility](../reference/ha-prototype-docs/CONTROL_FEASIBILITY.md), [control design](../reference/ha-prototype-docs/CONTROL_DESIGN.md), [entity contract](../reference/ha-prototype-docs/ENTITY_CONTRACT.md), and [verification notes](../reference/ha-prototype-docs/VERIFICATION.md).

## 2. Exploratory fixtures and schedule modulation trial

Codex thread ID: `01a0dba1-3fd6-7562-a6ea-c79e9aa804b3`  
Workspace at the time: HASS-Solar-Rivian and Riviamigo.  
Topic: test whether one Rivian home schedule can supply an active window and adjustable amperage; build fixtures and a safe attended protocol.

Decisions and outcomes:

- The owner's “one main schedule” observation became a **cardinality condition to measure**, not permission to select an arbitrary entry from a provider array.
- The attended plan proposed a bounded 8→12→8 A trial, separate outside-window pause/resume observation, complete pre-change snapshot, conflict detection, and exact restoration.
- A Riviamigo full upstream-array read and stricter mutation result handling were prepared as local, undeployed changes. The patch is [archived here](../reference/riviamigo-upstream-schedule-read.patch). The normal cached schedule path was not sufficient for guaranteed full-list backup.
- HA exploratory replays and synthetic schedule fixtures passed in the conversation. Test counts are historical software evidence, not physical charging proof.
- HA then had no Rivian schedule service installed. Wall Connector power/current reports were stale, and no vehicle or charger command was sent. Live test and recovery gates remained open.

Primary outputs copied here: [trial protocol](../reference/ha-prototype-docs/RIVIAN_SCHEDULE_MODULATION_TRIAL.md), [exploratory plan](../reference/ha-prototype-docs/EXPLORATORY_TEST_PLAN.md), [HA inventory](../reference/ha-solar-ev-inventory.md), and [tests/fixtures](../reference/ha-exploratory-tests/).

## 3. Architecture and standalone harness discussion

ChatGPT conversation ID: `6a1b29ed-4730-83e8-abde-5e7bb57529fb`  
Title: “Rivian Solar Charging Integration”.  
Follow-on Codex task ID: `01a16c03-90ec-7962-932f-f421d4e5a25e` (“Document Rivian charging harness”).

Later choices refined the prototype direction:

- HA owns automation; Riviamigo may become the analytics/history layer.
- A **separate future solar integration** owns algorithms, forecast/weather adaptation, safety state, configuration, and visualizations. The existing Rivian integration owns Rivian authentication, vehicles, and exposed controls. Avoid reaching into its private `hass.data` client or copying HA credentials.
- Use the existing Rivian HA fork for a **standalone** schedule/amperage CLI proof first. Because it runs outside HA, first-run interactive login must collect email/password and MFA/OTP, then securely cache and refresh tokens.
- The CLI must offer read, dry-run, explicit apply, fresh verification, exact restore, redacted logs, and Gen 2 safeguards. The draft in [reference](../reference/standalone-harness-draft/) is not validated proof; the [implementation brief](RIVIAN_SOLAR_CHARGING_RESEARCH_AND_HARNESS_BRIEF.md) is the acceptance contract.
- The user requested that all research and Codex discussion context be consolidated in this separate HASS-Solar-Rivian repository.

## Open questions carried forward

1. Can the account read a complete, stable schedule collection and identify the intended home schedule unambiguously?
2. Does a bounded schedule amperage mutation change actual charge current on this Gen 2 R1S while plugged in?
3. Does moving the active window outside the present time physically pause, and what happens after a process/network failure?
4. Can every test restore the owner's exact original schedule without overwriting a concurrent edit?
5. Are fresh physical meter/Wall Connector readings and an independently verified stop path available for unattended operation?
