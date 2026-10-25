"""Times: each car's plan time."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import C_PLAN_TIME
from .coordinator import PlannerConfigEntry, PlannerCoordinator
from .entity import SettingEntity

PARALLEL_UPDATES = 0


def parse_hhmm(value: str) -> time:
    h, m = (int(x) for x in str(value).split(":")[:2])
    return time(h % 24, m)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the plan times."""
    coordinator = entry.runtime_data
    for car_id, car in coordinator.cars.items():
        async_add_entities([PlanTime(coordinator, car_id)], config_subentry_id=car["subentry_id"])


class PlanTime(SettingEntity, TimeEntity):
    """When the car's plan is due ("X % by this time")."""

    def __init__(self, coordinator: PlannerCoordinator, car_id: str) -> None:
        super().__init__(coordinator, "plan_time", car_id)

    @property
    def native_value(self) -> time:
        assert self.car_id is not None
        return parse_hhmm(self.coordinator.car_settings[self.car_id][C_PLAN_TIME])

    async def async_set_value(self, value: time) -> None:
        assert self.car_id is not None
        await self.coordinator.async_set_car_setting(
            self.car_id, C_PLAN_TIME, value.strftime("%H:%M")
        )
