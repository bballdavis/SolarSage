# HASS Solar Rivian

This repository is the planning and evidence home for Home Assistant solar charging with a Rivian. The [research and standalone harness brief](docs/RIVIAN_SOLAR_CHARGING_RESEARCH_AND_HARNESS_BRIEF.md) records the current direction: Home Assistant owns automation, a separate solar integration is the intended product, and Rivian schedule control must first be proved with an attended standalone CLI test.

Start here:

- [Source work and artifact index](docs/SOURCE_WORK_INDEX.md) — copied snapshots, source repositories, revisions, and current boundaries.
- [Complete HA prototype source snapshot](reference/ha-prototype-source/) — integration, solar controller and UI, tests, and build metadata from the separate checkout.
- [Codex discussion history](docs/CODEX_DISCUSSION_HISTORY.md) — relevant chat IDs and decisions.
- [Research and harness implementation brief](docs/RIVIAN_SOLAR_CHARGING_RESEARCH_AND_HARNESS_BRIEF.md) — requirements and acceptance gates.

The `reference/` tree is an archival snapshot of earlier work. Its Python harness and HA tests are not installed or run by this repository. No credentials, raw Rivian responses, or live charging proof are included.

