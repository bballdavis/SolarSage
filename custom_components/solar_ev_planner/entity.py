"""Base entities: the planner's device and one device per car."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_NAME,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DEVICE_MODEL_CAR,
    DOMAIN,
    ENTRY_TITLE,
)
from .coordinator import CarView, PlannerCoordinator


class PlannerEntity(CoordinatorEntity[PlannerCoordinator]):
    """An entity of the planner (``car_id`` None) or of one car's charging plan.

    Entity IDs follow the device names: ``<platform>.solar_ev_planner_<key>`` for the
    planner and ``<platform>.<car name>_planner_<key>`` for a car.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PlannerCoordinator, key: str, car_id: str | None = None
    ) -> None:
        super().__init__(coordinator)
        self.car_id = car_id
        self._attr_translation_key = key
        entry_id = coordinator.config_entry.entry_id
        if car_id is None:
            self._attr_unique_id = f"{entry_id}_{key}"
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, entry_id)},
                name=ENTRY_TITLE,
                manufacturer=DEVICE_MANUFACTURER,
                model=DEVICE_MODEL,
                entry_type=DeviceEntryType.SERVICE,
            )
        else:
            self._attr_unique_id = f"{entry_id}_{car_id}_{key}"
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{entry_id}_{car_id}")},
                name=f"{coordinator.cars[car_id][CONF_NAME]} planner",
                manufacturer=DEVICE_MANUFACTURER,
                model=DEVICE_MODEL_CAR,
                entry_type=DeviceEntryType.SERVICE,
            )

    @property
    def car_view(self) -> CarView | None:
        """This car at the last cycle."""
        data = self.coordinator.data
        if data is None or self.car_id is None:
            return None
        return data.cars.get(self.car_id)


class SettingEntity(PlannerEntity):
    """An owner setting: stored by the planner, always available, even while the planning
    cycle fails."""

    @property
    def available(self) -> bool:
        return True
