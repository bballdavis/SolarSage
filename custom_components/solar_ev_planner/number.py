"""Numbers: the planner's limits and prices, and each car's limit, plan target and the
Tesla's current restored at unplug."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    C_LIMIT,
    C_PLAN_TARGET,
    C_RESTORE,
    CONF_KIND,
    CONF_LABEL,
    CONF_MAX_A,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL_WINDOW,
    DOMAIN,
    ENTRY_TITLE,
    KIND_TESLA,
    S_BUYBACK,
    S_FLOOR,
    S_MAX_STEP,
    S_MIN_CHANGE,
    S_PAID_TOLERANCE,
    S_PRICE,
    S_WANTED,
)
from .coordinator import PlannerConfigEntry, PlannerCoordinator
from .entity import SettingEntity

PARALLEL_UPDATES = 0
PRICE = "price"  # unit placeholder: the currency per kWh


@dataclass(frozen=True)
class Range:
    key: str  # translation key
    setting: str
    minimum: float
    maximum: float
    step: float
    unit: str | None
    config: bool = True  # a configuration entity (shown under the device's configuration)
    device_class: NumberDeviceClass | None = None


SETTINGS = (
    # 48 A (the top) means each car's maximum, as night charging defaults to
    Range("battery_floor", S_FLOOR, 5, 95, 1, PERCENTAGE, False),
    Range("battery_wanted_at_free_hours", S_WANTED, 0, 100, 1, PERCENTAGE, False),
    Range("minimum_time_between_changes", S_MIN_CHANGE, 1, 120, 1, UnitOfTime.MINUTES),
    Range("largest_change", S_MAX_STEP, 1, 48, 1, UnitOfElectricCurrent.AMPERE),
    Range("paid_grid_tolerance", S_PAID_TOLERANCE, 0, 60, 1, UnitOfTime.MINUTES),
    Range("import_price", S_PRICE, 0, 5, 0.01, PRICE, False),
    Range("solar_buyback", S_BUYBACK, 0, 5, 0.01, PRICE, False),
)

INTEGER_SETTINGS = frozenset({S_MAX_STEP})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the setting numbers, the price windows' prices and the per-car numbers."""
    coordinator = entry.runtime_data
    async_add_entities(SettingNumber(coordinator, r) for r in SETTINGS)
    for subentry_id in coordinator.windows:
        async_add_entities(
            [WindowPriceNumber(coordinator, subentry_id)], config_subentry_id=subentry_id
        )
    for car_id, car in coordinator.cars.items():
        numbers: list[NumberEntity] = [
            CarNumber(coordinator, car_id, "charge_limit", C_LIMIT, 0, 100, PERCENTAGE),
            CarNumber(coordinator, car_id, "plan_target", C_PLAN_TARGET, 0, 100, PERCENTAGE),
        ]
        if car[CONF_KIND] == KIND_TESLA:
            numbers.append(
                CarNumber(
                    coordinator,
                    car_id,
                    "restore_current",
                    C_RESTORE,
                    0,
                    int(car[CONF_MAX_A]),
                    UnitOfElectricCurrent.AMPERE,
                )
            )
        async_add_entities(numbers, config_subentry_id=car["subentry_id"])


def _unit(coordinator: PlannerCoordinator, unit: str | None) -> str | None:
    return f"{coordinator.hass.config.currency}/kWh" if unit == PRICE else unit


class SettingNumber(SettingEntity, NumberEntity):
    """A numeric owner setting."""

    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: PlannerCoordinator, spec: Range) -> None:
        super().__init__(coordinator, spec.key)
        self.spec = spec
        self._attr_native_min_value = spec.minimum
        self._attr_native_max_value = spec.maximum
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = _unit(coordinator, spec.unit)
        if spec.config:
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> float:
        return float(self.coordinator.settings[self.spec.setting])

    async def async_set_native_value(self, value: float) -> None:
        stored: float | int = int(value) if self.spec.setting in INTEGER_SETTINGS else value
        await self.coordinator.async_set_setting(self.spec.setting, stored)


class WindowPriceNumber(SettingEntity, NumberEntity):
    """The import price while a price window's Schedule helper is on."""

    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 5
    _attr_native_step = 0.01

    def __init__(self, coordinator: PlannerCoordinator, subentry_id: str) -> None:
        super().__init__(coordinator, "window_price")
        self.subentry_id = subentry_id
        entry_id = coordinator.config_entry.entry_id
        label = str(coordinator.windows[subentry_id].get(CONF_LABEL) or "window")
        self._attr_unique_id = f"{entry_id}_{subentry_id}_price"
        # A device belongs to one config subentry: each price window has its own, named
        # "Solar EV planner <label>", so the entity is number.solar_ev_planner_<label>_price.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{subentry_id}")},
            name=f"{ENTRY_TITLE} {label}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL_WINDOW,
            entry_type=DeviceEntryType.SERVICE,
        )
        self._attr_native_unit_of_measurement = _unit(coordinator, PRICE)

    @property
    def native_value(self) -> float:
        return self.coordinator.window_prices[self.subentry_id]

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_window_price(self.subentry_id, value)


class CarNumber(SettingEntity, NumberEntity):
    """A per-car setting: the charge limit the planner aims for (0 = the car's own), the
    plan's target, or the Tesla's current restored at unplug (0 = off)."""

    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: PlannerCoordinator,
        car_id: str,
        key: str,
        setting: str,
        minimum: int,
        maximum: int,
        unit: str,
    ) -> None:
        super().__init__(coordinator, key, car_id)
        self.setting = setting
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = unit
        if setting != C_PLAN_TARGET:
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> float:
        assert self.car_id is not None
        return float(self.coordinator.car_settings[self.car_id][self.setting])

    async def async_set_native_value(self, value: float) -> None:
        assert self.car_id is not None
        await self.coordinator.async_set_car_setting(self.car_id, self.setting, int(value))
