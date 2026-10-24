"""Sensors for Solar EV Charging."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.helpers import entity_registry as er

from .coordinator import SolarEvCoordinator
from .entity import SolarEvEntity


@dataclass(frozen=True, kw_only=True)
class SolarEvSensorDescription(SensorEntityDescription):
    """Describe a controller sensor."""

    data_key: str


DESCRIPTIONS = (
    SolarEvSensorDescription(
        key="control_phase", translation_key="control_phase", data_key="control_phase"
    ),
    SolarEvSensorDescription(
        key="smoothed_power",
        translation_key="smoothed_power",
        data_key="smoothed_watts",
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SolarEvSensorDescription(
        key="observed_power",
        translation_key="observed_power",
        data_key="observed_power_w",
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SolarEvSensorDescription(
        key="restart_countdown",
        translation_key="restart_countdown",
        data_key="restart_remaining_seconds",
        native_unit_of_measurement=UnitOfTime.SECONDS,
    ),
    SolarEvSensorDescription(
        key="forecast_budget",
        translation_key="forecast_budget",
        data_key="ev_budget_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SolarEvSensorDescription(
        key="controller_state", translation_key="controller_state", data_key="state"
    ),
    SolarEvSensorDescription(
        key="active_vehicle",
        translation_key="active_vehicle",
        data_key="active_vehicle",
    ),
    SolarEvSensorDescription(
        key="target_amps",
        translation_key="target_amps",
        data_key="target_amps",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
    ),
    SolarEvSensorDescription(
        key="available_power",
        translation_key="available_power",
        data_key="available_watts",
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    SolarEvSensorDescription(
        key="block_reason", translation_key="block_reason", data_key="reason"
    ),
    SolarEvSensorDescription(
        key="last_command",
        translation_key="last_command",
        data_key="last_command",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


class SolarEvSensor(SolarEvEntity, SensorEntity):
    """Represent one controller value."""

    entity_description: SolarEvSensorDescription
    _unrecorded_attributes = frozenset({"status", "controls"})

    def __init__(
        self, coordinator: SolarEvCoordinator, description: SolarEvSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the current value."""
        if self.entity_description.key == "forecast_budget":
            return self.coordinator.data.get("forecast", {}).get("ev_budget_kwh")
        return self.coordinator.data.get(self.entity_description.data_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Publish only controller telemetry and entity bindings for the HA panel."""
        if self.entity_description.key != "controller_state":
            return None
        registry = er.async_get(self.hass)
        entry = self.coordinator.entry
        controls = {}
        for action, domain, key in (
            ("enabled", "switch", "solar_charging"),
            ("charge_now", "button", "charge_now"),
            ("resume", "button", "resume_automatic"),
            ("probe", "button", "rivian_compatibility_probe"),
            ("commission_stop", "button", "commission_stop"),
            ("safe_release", "button", "safe_release"),
            ("clear_fault", "button", "clear_fault"),
        ):
            if entity_id := registry.async_get_entity_id(
                domain, "rivian", f"{entry.entry_id}_{key}"
            ):
                controls[action] = entity_id
        return {
            "solar_controller": True,
            "entry_id": entry.entry_id,
            "entry_title": entry.title,
            "status": self.coordinator.data,
            "controls": controls,
        }
