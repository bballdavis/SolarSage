"""Switches for Solar EV Charging."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from .coordinator import SolarEvCoordinator
from .entity import SolarEvEntity


class SolarChargingSwitch(SolarEvEntity, SwitchEntity):
    """Enable automatic solar control."""

    _attr_translation_key = "solar_charging"

    def __init__(self, coordinator: SolarEvCoordinator) -> None:
        super().__init__(coordinator, "solar_charging")

    @property
    def is_on(self) -> bool:
        return self.coordinator.enabled

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_enabled(False)
