"""Number: evcc's charging current request."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import RivianSolarAdapterEntity
from .runtime import AdapterRuntime, RivianSolarAdapterConfigEntry
from .schedule import AMPS_MAX, AMPS_MIN, AMPS_STEP

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RivianSolarAdapterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the current number."""
    async_add_entities([SolarCurrentNumber(entry.runtime_data)])


class SolarCurrentNumber(RivianSolarAdapterEntity, NumberEntity):
    """Requested current; evcc's ``setMaxCurrent`` service target.

    Rivian schedules take even amperages from 8 to 48 A; other values are
    rounded down to the next even value.
    """

    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = AMPS_MIN
    _attr_native_max_value = AMPS_MAX
    _attr_native_step = AMPS_STEP
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, runtime: AdapterRuntime) -> None:
        super().__init__(runtime, "solar_current")

    @property
    def native_value(self) -> int:
        """The requested current as it will be written."""
        return self.runtime.controller.journal.desired_amps

    async def async_set_native_value(self, value: float) -> None:
        """Record the requested current; the write happens in the background."""
        await self.runtime.async_request_amps(value)
