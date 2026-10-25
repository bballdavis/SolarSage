# HASS Solar Rivian

Smart solar charging in Home Assistant for **both** household EVs, a Tesla and a 2025 Gen 2 Rivian R1S. They share one Tesla Wall Connector Gen 3 and run on a Sol-Ark solar and battery system. Despite the name, this project covers both cars.

**Start with the [working plan](docs/PLAN.md).** It records the decisions, how the system works, the planner, electricity plans, the dashboard, the phases and what is still needed.

In short:

- **Existing hardware only:** Home Assistant OS, the Sol-Ark, the Tesla Wall Connector (measurement only) and the two cars.
- **The cars through their own APIs:** the Tesla through HA core Tesla Fleet, the Rivian through this repository's charge adapter, which writes its charging schedule.
- **A planner in Home Assistant** decides each car's current: night charging in the free tariff hours by default, and smooth daytime solar charging planned from the Solcast forecast and recent usage, with the home battery absorbing swings.
- **One dashboard** in Home Assistant to see and steer it.

## What is here

| Path | What it is |
| --- | --- |
| [`docs/PLAN.md`](docs/PLAN.md) | The working plan |
| [`docs/RIVIAN_CHARGE_ADAPTER.md`](docs/RIVIAN_CHARGE_ADAPTER.md) | Guide to the Rivian charge adapter: how it works, install, entities, the attended trial, faults |
| [`custom_components/rivian_solar_adapter/`](custom_components/rivian_solar_adapter/) | The Rivian charge adapter (HA integration, installable through HACS) |
| [`tests/`](tests/) | The adapter's tests |

The planner integration and the dashboard are next ([plan phases](docs/PLAN.md#phases)). Earlier designs, research notes and prototype snapshots are in git history.

Nothing here has been run against the real cars, charger or Home Assistant instance yet. The adapter installs in dry run and writes nothing until dry run is turned off.

## Checks

There is no CI yet. Before committing, run these locally in a Python 3.14 environment with `pytest-homeassistant-custom-component` and `rivian-python-client` installed:

```sh
ruff check custom_components tests
ruff format --check custom_components tests
python -m pytest tests -q
```
