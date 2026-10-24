# Source work and artifact index

Snapshot date: October 23, 2026. The copied reference files and original brief are present in this repository at `3eec08f` (`origin/main` matched locally when checked). This index records local repository state observed on that date. It is a navigation and provenance record, not a deployment or physical charging acceptance report. The copied files under `reference/` are snapshots; make future code changes in their owning repositories.

## Where the work lives

| Source | Observed revision and work | What is preserved here |
| --- | --- | --- |
| Separate HASS-Solar-Rivian implementation checkout | `main` at `6ab50e6051253d98c9b0bab5d319a1836264f7ce`; locally ahead of `origin/main` by two commits. The earlier combined Rivian/solar integration has committed controller and UI code. | [HA prototype documents](../reference/ha-prototype-docs/) and [exploratory test snapshots](../reference/ha-exploratory-tests/). The [complete source snapshot](../reference/ha-prototype-source/) includes the tracked integration, UI, tests, metadata, and eight local-only exploratory files; see revisions below. |
| Neighboring `home-assistant-rivian` checkout | `main` at `4279cc16903cc3adf571a74fe591296c0cf36825`, with modified `.gitignore` and an untracked `tools/` draft. Its configured `origin` is upstream `bretterer/home-assistant-rivian`, not a published fork branch. | [Standalone harness draft](../reference/standalone-harness-draft/) as a review-only snapshot. |
| Riviamigo | `dev` at `b38f2a1f2c04018e8db1925f8ed81821f07f93ce`, with matching `origin/dev` locally. It has schedule GET/PUT and ingestion code. A separate local worktree on `fix/upstream-charging-schedule-read` has uncommitted complete-list read and validation changes. | [Patch of those local changes](../reference/riviamigo-upstream-schedule-read.patch). The patch is not a merged or deployed feature. |
| HASS-MCP | Read-only HA inventory from September 2026. | [Sanitized solar EV inventory](../reference/ha-solar-ev-inventory.md). Historical device readings are not current state. |

The separate implementation checkout is `C:\Users\bball\Documents\Codex\2026-09-19\i-d-like-you-to-do\HASS-Solar-Rivian`. Its configured remote is `https://git.davisho.me/philipdavis/HASS-Solar-Rivian.git`. The two newest local commits were **not confirmed on that remote** during this inventory. The OneDrive repository containing this index is a distinct Git repository at `https://github.com/bballdavis/HASS-Solar-Rivian.git`.

## Committed prototype revisions

These revisions belong to the separate implementation checkout. They describe a combined integration prototype, while the later architectural decision is to keep the future solar policy/UI integration separate.

- `93e35cd` — first solar-surplus EV charging implementation.
- `037bbc5` — hardened feedback, forecast handling, and native HA controls.
- `6ab50e6` — live-entity forecast contracts and guided solar controls; local `main` head at snapshot time.
- `a7e29e9` — upstream charge-rate and charging-schedule control inherited in that checkout.

The [source snapshot](../reference/ha-prototype-source/) contains the `custom_components/rivian` runtime, solar controller/UI, tests, HACS metadata, license, and original repository files. It is stored below `reference/` so its historical release workflows cannot run from this repository. It is browseable and downloadable from GitHub after this repository is pushed. The copied [control design](../reference/ha-prototype-docs/CONTROL_DESIGN.md), [entity contract](../reference/ha-prototype-docs/ENTITY_CONTRACT.md), [card plan](../reference/ha-prototype-docs/CARD.md), and [verification notes](../reference/ha-prototype-docs/VERIFICATION.md) describe this prototype. Its tests and UI previews establish software behavior only; live Rivian/Wall Connector control was not proved.

## Local research and trial artifacts

The following were uncommitted in the implementation checkout when copied. They are preserved as independent review material:

- [Control feasibility](../reference/ha-prototype-docs/CONTROL_FEASIBILITY.md) — Rivian schedule, Wall Connector, EVCC, and hardware options.
- [Exploratory test plan](../reference/ha-prototype-docs/EXPLORATORY_TEST_PLAN.md) — HA state inventory, software replays, commissioning gates, and unresolved controls.
- [Rivian schedule modulation trial](../reference/ha-prototype-docs/RIVIAN_SCHEDULE_MODULATION_TRIAL.md) — attended amp, outside-window, and exact-restore protocol.
- [Exploratory tests and fixtures](../reference/ha-exploratory-tests/) — copied from the modified/untracked local files; the observed snapshot is sanitized and time-specific.

The CLI [draft script](../reference/standalone-harness-draft/test_charging_schedule.py) and [draft README](../reference/standalone-harness-draft/README-charging-schedule-test.md) were untracked in the neighboring checkout. They are **experimental**: the GraphQL operation shapes are guesses, their security and restore behavior need the audit in the [implementation brief](RIVIAN_SOLAR_CHARGING_RESEARCH_AND_HARNESS_BRIEF.md), and they have no live Gen 2 acceptance evidence. Do not run the archived copy as a charging controller.

## Riviamigo schedule seam

The observed `dev` branch has [schedule routes](https://github.com/bballdavis/Riviamigo/blob/b38f2a1f2c04018e8db1925f8ed81821f07f93ce/apps/api/src/routes/schedules.rs) and [Rivian schedule ingestion/mutation code](https://github.com/bballdavis/Riviamigo/blob/b38f2a1f2c04018e8db1925f8ed81821f07f93ce/apps/api/src/ingestion/rivian_poll.rs). This is source code, not evidence that the live API is deployed or suitable for a complete-list backup.

The `fix/upstream-charging-schedule-read` worktree is at base `f99f1e75d37f188f4a2a72c6514b1acb7d1e2731` and has uncommitted changes to `rivian_poll.rs`, `schedules.rs`, `auth.rs`, and `docs/api-access.md`. Its [captured patch](../reference/riviamigo-upstream-schedule-read.patch) adds the proposed fresh full-array read and stricter mutation handling. It is local-only and should be reviewed against the current `dev` branch before integration. Do not present it as a published branch feature.

## Decision and evidence boundary

- HA owns the automation loop and UI context. The future solar integration should consume stable HA entities/services and avoid private credential or client-object coupling.
- Schedule API acceptance, full readback, exact restoration, and measured physical current change are separate evidence gates.
- The installed Wall Connector was observed as telemetry only, and Gen 2 Rivian remote command support was uncertain. A schedule window is not a guaranteed physical stop.
- Historical software test counts and HA observations are recorded in the copied documents and [discussion history](CODEX_DISCUSSION_HISTORY.md). No current live state was queried for this archive.
- No token cache, password, OTP, exact provider schedule, VIN, coordinates, or raw API log was copied.

## Upstream research snapshot (October 24, 2026)

The [ecosystem survey](ECOSYSTEM_SURVEY.md#snapshot-of-sources) lists the upstream commits read for the [architecture decision](SOLAR_CHARGING_ARCHITECTURE.md). They include evcc, `ha-evcc`, `home-assistant-rivian` 2.0.0, `rivian-python-client` (head just after 2.3.1; the cited code is identical to 2.3.1), the HA core Tesla and OpenEVSE integrations, Tesla BLE projects, and Wall Connector meter-emulation projects. No upstream source was copied into this repository; citations are permalinks to those commits.


