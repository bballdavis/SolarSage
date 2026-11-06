"""Solar EV planner.

Plans each car's charging current on the Wall Connector from the Solcast forecast, recent
house use (recorder statistics) and the home battery's predicted level, and commands the car
on the charger: the Tesla through Tesla Fleet, the Rivian through this repository's Rivian
schedule control. Shadow mode (on by default) plans and explains but never calls a service. The
algorithm is the pure-Python package in ``core`` (docs/PLANNER_DESIGN.md); installing and
configuring it is in docs/PLANNER.md.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from . import websocket_api
from .const import DOMAIN, PLATFORMS, STORAGE_VERSION
from .coordinator import PlannerConfigEntry, PlannerCoordinator
from .frontend import async_register_card

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the dashboard card and its websocket commands (docs/PLANNER.md, "The
    dashboard card")."""
    websocket_api.async_register(hass)
    await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PlannerConfigEntry) -> bool:
    """Start the planner: restore its memory, read the history, run the first cycle."""
    coordinator = PlannerCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Options and car or price-window subentries changed: start again with the new mapping.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: PlannerConfigEntry) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PlannerConfigEntry) -> bool:
    """Stop the planner; the coordinator saves its memory as it shuts down."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: PlannerConfigEntry) -> None:
    """Forget the planner's memory and settings."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
