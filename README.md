# HASS Solar Rivian

Smart solar charging in Home Assistant for **both** household EVs, a Tesla and a 2025 Gen 2 Rivian R1S. They share one Tesla Wall Connector Gen 3 and run on a Sol-Ark solar and battery system. Despite the name, this project covers both cars.

**Start with the [working plan](docs/PLAN.md).** It records the decisions, how the system works, the planner, electricity plans, the dashboard, the phases and what is still needed.

In short:

- **Existing hardware only:** Home Assistant OS, the Sol-Ark, the Tesla Wall Connector (measurement only) and the two cars.
- **The cars through their own APIs:** the Tesla through HA core Tesla Fleet, the Rivian through this repository's charge adapter, which writes its charging schedule.
- **A planner in Home Assistant** decides each car's current: full current in the free tariff hours up to the car's charge limit, and smooth daytime solar charging planned from the Solcast forecast and recent usage, with the home battery absorbing swings.
- **One dashboard** in Home Assistant to see and steer it.

## What is here

| Path | What it is |
| --- | --- |
| [`docs/PLAN.md`](docs/PLAN.md) | The working plan: decisions, phases, open items, information needed |
| [`docs/PLANNER.md`](docs/PLANNER.md) | Guide to the planner: install, configure, entities, settings, shadow mode, pointing the Rivian adapter at it |
| [`docs/PLANNER_DESIGN.md`](docs/PLANNER_DESIGN.md) | How the planner decides, and how an earlier version compared in simulation |
| [`docs/TESTING.md`](docs/TESTING.md) | The step-by-step path to test with real data and real connections (plan phases 1–6) |
| [`docs/RIVIAN_SCHEDULE_CONTROL.md`](docs/RIVIAN_SCHEDULE_CONTROL.md) | Guide to the Rivian schedule control: how it works, install, entities, the attended trial, faults |
| [`custom_components/solar_ev_planner/`](custom_components/solar_ev_planner/) | The Solar EV planner (HA integration; copy it into Home Assistant) |
| [`custom_components/rivian_schedule_control/`](custom_components/rivian_schedule_control/) | The Rivian schedule control (HA integration, installable through HACS) |
| [`tools/`](tools/README.md) | Real-data tools, run from a computer: a read-only check, snapshots, a replay of the planner, and the attended car tests |
| [`tests/`](tests/) | Tests of the adapter (`tests/`), the planner (`tests/planner/`) and the tools (`tests/tools/`), against fakes only |

Earlier designs, research notes and prototype snapshots are in git history.

**Nothing here has been run against the real cars, charger or Home Assistant instance yet.** The planner installs in shadow mode and sends nothing until shadow mode is turned off; the adapter installs in dry run and writes nothing until dry run is turned off. [TESTING.md](docs/TESTING.md) is the way in.

## Installing

HACS installs one integration per repository: from this one, the Rivian adapter ([`hacs.json`](hacs.json)). The planner is copied into `/config/custom_components/` by hand; [PLANNER.md](docs/PLANNER.md#install) gives the commands, and the same copy works for the adapter.

## Checks

There is no CI yet. Before committing, run these locally in a Python 3.14 environment with `pytest-homeassistant-custom-component` and `rivian-python-client` installed:

```sh
ruff check custom_components tests tools
ruff format --check custom_components tests tools
python -m pytest tests -q
```

The tests never reach a real system: Home Assistant's test plugin allows only local connections, and the cars, Rivian's API and Home Assistant itself (for the tools) are fakes.
