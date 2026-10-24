"""Base entities for Solar EV Charging."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN, VERSION
from .coordinator import SolarEvCoordinator


class SolarEvEntity(CoordinatorEntity[SolarEvCoordinator]):
    """Base coordinator entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SolarEvCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_solar_ev_charging")},
            name=f"{coordinator.entry.title} Solar Charging",
            manufacturer="HASS Solar Rivian",
            model="Solar EV Charging Controller",
            sw_version=VERSION,
        )
