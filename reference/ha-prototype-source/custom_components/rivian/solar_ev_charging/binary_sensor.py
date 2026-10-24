"""Binary sensors for Solar EV Charging."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory

from .const import STATE_FAULT
from .coordinator import SolarEvCoordinator
from .entity import SolarEvEntity


class ControllerHealthyBinarySensor(SolarEvEntity, BinarySensorEntity):
    """Report whether the controller is free of a hard fault."""

    _attr_translation_key = "controller_healthy"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SolarEvCoordinator) -> None:
        super().__init__(coordinator, "controller_healthy")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("state") != STATE_FAULT


class RivianCompatibleBinarySensor(SolarEvEntity, BinarySensorEntity):
    """Report whether transactional schedule control was proven."""

    _attr_translation_key = "rivian_compatible"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SolarEvCoordinator) -> None:
        super().__init__(coordinator, "rivian_compatible")

    @property
    def is_on(self) -> bool:
        return self.coordinator.rivian_compatible
