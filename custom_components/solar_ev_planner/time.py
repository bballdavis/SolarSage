"""Times: the night charging window, and each car's plan time."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import C_PLAN_TIME, S_NIGHT_END, S_NIGHT_START
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
    """Add the night window and the plan times."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            SettingTime(coordinator, "night_window_start", S_NIGHT_START),
            SettingTime(coordinator, "night_window_end", S_NIGHT_END),
        ]
    )
    for car_id, car in coordinator.cars.items():
        async_add_entities([PlanTime(coordinator, car_id)], config_subentry_id=car["subentry_id"])


class SettingTime(SettingEntity, TimeEntity):
    """A time of day setting, stored as HH:MM."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PlannerCoordinator, key: str, setting: str) -> None:
        super().__init__(coordinator, key)
        self.setting = setting

    @property
    def native_value(self) -> time:
        return parse_hhmm(self.coordinator.settings[self.setting])

    async def async_set_value(self, value: time) -> None:
        await self.coordinator.async_set_setting(self.setting, value.strftime("%H:%M"))


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
