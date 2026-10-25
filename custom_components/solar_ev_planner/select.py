"""Selects: the caution level, the charger car override and each car's plan days."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    C_PLAN_DAYS,
    CAUTION_OPTIONS,
    OVERRIDE_AUTO,
    PLAN_DAYS,
    S_CAUTION,
    S_OVERRIDE,
)
from .coordinator import PlannerConfigEntry, PlannerCoordinator
from .entity import SettingEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the selects."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            SettingSelect(coordinator, "caution", S_CAUTION, list(CAUTION_OPTIONS)),
            # "auto" decides from the Wall Connector and the cars' own sensors; naming a car
            # leaves only that car. The Rivian adapter's vehicle override option can point
            # here too: it treats "auto" and "rivian" as no override.
            SettingSelect(
                coordinator,
                "charger_car_override",
                S_OVERRIDE,
                [OVERRIDE_AUTO, *coordinator.cars],
                EntityCategory.CONFIG,
            ),
        ]
    )
    for car_id, car in coordinator.cars.items():
        async_add_entities(
            [PlanDaysSelect(coordinator, car_id)], config_subentry_id=car["subentry_id"]
        )


class SettingSelect(SettingEntity, SelectEntity):
    """A choice among fixed options."""

    def __init__(
        self,
        coordinator: PlannerCoordinator,
        key: str,
        setting: str,
        options: list[str],
        category: EntityCategory | None = None,
    ) -> None:
        super().__init__(coordinator, key)
        self.setting = setting
        self._attr_options = options
        self._attr_entity_category = category

    @property
    def current_option(self) -> str:
        return str(self.coordinator.settings[self.setting])

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_setting(self.setting, option)


class PlanDaysSelect(SettingEntity, SelectEntity):
    """When the car's plan applies: off, once (the next plan time), weekdays, daily or
    weekends."""

    _attr_options = list(PLAN_DAYS)

    def __init__(self, coordinator: PlannerCoordinator, car_id: str) -> None:
        super().__init__(coordinator, "plan_days", car_id)

    @property
    def current_option(self) -> str:
        assert self.car_id is not None
        return str(self.coordinator.car_settings[self.car_id][C_PLAN_DAYS])

    async def async_select_option(self, option: str) -> None:
        assert self.car_id is not None
        await self.coordinator.async_set_car_setting(self.car_id, C_PLAN_DAYS, option)
