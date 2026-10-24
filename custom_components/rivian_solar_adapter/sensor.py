"""Sensors: the adapter's control state and the vehicle status for evcc."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import EVCC_STATUSES
from .controller import ControlState
from .entity import RivianSolarAdapterEntity
from .runtime import AdapterRuntime, RivianSolarAdapterConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RivianSolarAdapterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors."""
    runtime = entry.runtime_data
    async_add_entities([ControlStateSensor(runtime), EvccStatusSensor(runtime)])


class ControlStateSensor(RivianSolarAdapterEntity, SensorEntity):
    """What the adapter is doing, with its journal summary as attributes."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [state.value for state in ControlState]

    def __init__(self, runtime: AdapterRuntime) -> None:
        super().__init__(runtime, "control_state")

    @property
    def native_value(self) -> str:
        """The control state."""
        return self.runtime.controller.state(dt_util.utcnow()).value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Journal summary, errors and the last provider read; no coordinates."""
        return self.runtime.attributes(dt_util.utcnow())


class EvccStatusSensor(RivianSolarAdapterEntity, SensorEntity):
    """Vehicle status for evcc's ``status`` sensor: a, b or c."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = EVCC_STATUSES

    def __init__(self, runtime: AdapterRuntime) -> None:
        super().__init__(runtime, "evcc_status")

    @property
    def native_value(self) -> str:
        """a unless eligible (plugged in at home, at the Wall Connector if known)
        or while the override names another vehicle; c while charging, else b."""
        return self.runtime.evcc_status
