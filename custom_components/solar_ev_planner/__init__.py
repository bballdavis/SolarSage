"""Solar EV planner.

Plans each car's charging current on the Wall Connector from the Solcast forecast, recent
house use (recorder statistics) and the home battery's predicted level, and commands the car
on the charger: the Tesla through Tesla Fleet, the Rivian through this repository's charge
adapter. Shadow mode (on by default) plans and explains but never calls a service. The
algorithm is the pure-Python package in ``core`` (docs/PLANNER_DESIGN.md); installing and
configuring it is in docs/PLANNER.md.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_BATTERY_FREE_HOURS,
    DOMAIN,
    ENTRY_MINOR_VERSION,
    ENTRY_VERSION,
    LEGACY_BATTERY_HOLDS,
    PLATFORMS,
    STORAGE_VERSION,
)
from .coordinator import PlannerConfigEntry, PlannerCoordinator
from .core import FREE_HOLD, FREE_NORMAL

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: PlannerConfigEntry) -> bool:
    """Start the planner: restore its memory, read the history, run the first cycle."""
    coordinator = PlannerCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Options and car or price-window subentries changed: start again with the new mapping.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: PlannerConfigEntry) -> bool:
    """Migrate an entry made by an earlier version of the planner.

    Minor version 2 replaces the on/off "battery holds during free hours" with what the
    battery does in free hours (refill, hold or normal): on becomes hold and off normal, so
    the prediction is unchanged until the owner picks refill.
    """
    if entry.version > ENTRY_VERSION:
        return False  # made by a newer version of the planner
    if entry.minor_version < 2:
        options = dict(entry.options)
        if LEGACY_BATTERY_HOLDS in options:
            holds = options.pop(LEGACY_BATTERY_HOLDS)
            options.setdefault(CONF_BATTERY_FREE_HOURS, FREE_HOLD if holds else FREE_NORMAL)
        hass.config_entries.async_update_entry(
            entry, options=options, minor_version=ENTRY_MINOR_VERSION
        )
        _LOGGER.info(
            "Migrated the Solar EV planner to version %s.%s", ENTRY_VERSION, ENTRY_MINOR_VERSION
        )
    return True


async def _async_reload(hass: HomeAssistant, entry: PlannerConfigEntry) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PlannerConfigEntry) -> bool:
    """Stop the planner; the coordinator saves its memory as it shuts down."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: PlannerConfigEntry) -> None:
    """Forget the planner's memory and settings."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
