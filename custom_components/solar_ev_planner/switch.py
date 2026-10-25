"""Switches: the planner on or off, shadow mode, daytime solar charging."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import S_DAYTIME_SOLAR, S_ENABLED, S_SHADOW
from .coordinator import PlannerConfigEntry, PlannerCoordinator
from .entity import SettingEntity

PARALLEL_UPDATES = 0

# translation key -> setting
SWITCHES = {
    # off sends nothing; the heartbeat goes unavailable, so Rivian schedule control parks its window
    "planning": S_ENABLED,
    "shadow_mode": S_SHADOW,  # plan and explain, never call a service
    "daytime_solar_charging": S_DAYTIME_SOLAR,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the setting switches."""
    coordinator = entry.runtime_data
    async_add_entities(
        SettingSwitch(coordinator, key, setting) for key, setting in SWITCHES.items()
    )


class SettingSwitch(SettingEntity, SwitchEntity):
    """An on/off owner setting."""

    def __init__(self, coordinator: PlannerCoordinator, key: str, setting: str) -> None:
        super().__init__(coordinator, key)
        self.setting = setting

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.settings[self.setting])

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_setting(self.setting, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_setting(self.setting, False)
