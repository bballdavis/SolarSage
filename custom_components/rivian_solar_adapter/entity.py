"""Base entity for the Rivian Solar Charge Adapter."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DEVICE_MANUFACTURER, DEVICE_MODEL, DOMAIN
from .runtime import AdapterRuntime


class RivianSolarAdapterEntity(Entity):
    """An entity that writes its state after every adapter tick and request."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: AdapterRuntime, key: str) -> None:
        self.runtime = runtime
        self._attr_translation_key = key
        self._attr_unique_id = f"{runtime.vehicle_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.vehicle_id)},
            name=f"{runtime.entry.title} solar adapter",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            sw_version=runtime.sw_version,
        )

    async def async_added_to_hass(self) -> None:
        """Follow the runtime's updates."""
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.async_add_listener(self.async_write_ha_state))
