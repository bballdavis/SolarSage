# HASS Solar Rivian

Seamless "charge on solar" in Home Assistant for **both** household EVs, a Tesla and a 2025 Gen 2 Rivian R1S. Both share one Tesla Wall Connector Gen 3 and run on a Sol-Ark solar and battery system. Despite the name, this is an orchestration project between Home Assistant, the Tesla, and the Rivian.

**Direction (October 2026):** build on existing projects instead of a custom controller.

- **[evcc](https://github.com/evcc-io/evcc)** is the solar-charging brain.
- **Home Assistant** supplies the measurements and the vehicle controls.
- **This repository** owns only what nobody else solves: a small Rivian charge adapter, the HA glue configuration, and the commissioning evidence.
- **Hardware fallback.** If the Gen 2 Rivian cannot be controlled reliably through its charging schedule, replace the Wall Connector with a locally controllable NACS EVSE. evcc then controls both cars at the charger.

Start here:

- [Architecture decision](docs/SOLAR_CHARGING_ARCHITECTURE.md): the chosen design, what we build versus reuse, the Rivian adapter contract, evcc pitfalls, the safety model, and open questions for the owner.
- [Roadmap](docs/ROADMAP.md): phased plan with evidence gates. Tesla first, then an attended Rivian trial using the adapter, then wiring the adapter into evcc or taking the hardware path.
- [Ecosystem survey](docs/ECOSYSTEM_SURVEY.md): what evcc, the Tesla integrations, the Rivian integration and client, charger-side options, and other orchestrators actually do, with source permalinks.
- [Draft evcc config](config/evcc/evcc.example.yaml) and [draft HA glue package](config/homeassistant/packages/ev_solar_glue.example.yaml): starting points with placeholders, untested against the live installation.

Background and history:

- [Research and harness brief](docs/RIVIAN_SOLAR_CHARGING_RESEARCH_AND_HARNESS_BRIEF.md): earlier decisions and the acceptance contract for Rivian control evidence.
- [Source work and artifact index](docs/SOURCE_WORK_INDEX.md): copied snapshots, source repositories, revisions, and boundaries.
- [Complete HA prototype source snapshot](reference/ha-prototype-source/): the earlier combined Rivian/solar integration. Its Rivian schedule-safety code is the basis for the adapter; its control engine and panel are superseded by evcc.
- [Codex discussion history](docs/CODEX_DISCUSSION_HISTORY.md): relevant chat IDs and decisions.

The `reference/` tree is an archival snapshot of earlier work. Its Python harness and HA tests are not installed or run by this repository. No credentials, raw Rivian responses, or live charging proof are included. Nothing in `config/` has been applied to the live Home Assistant instance.
