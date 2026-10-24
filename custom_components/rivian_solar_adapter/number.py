"""Number: evcc's charging current request."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import REQUEST_MAX_AMPS, REQUEST_MIN_AMPS
from .entity import RivianSolarAdapterEntity
from .runtime import AdapterRuntime, RivianSolarAdapterConfigEntry

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

    Any current evcc may send is accepted and shown as requested. Rivian
    schedules take even amperages from 8 to 48 A, so the controller applies
    the request rounded down to an even value within that range.
    """

    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = REQUEST_MIN_AMPS
    _attr_native_max_value = REQUEST_MAX_AMPS
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, runtime: AdapterRuntime) -> None:
        super().__init__(runtime, "solar_current")

    @property
    def native_value(self) -> float:
        """The current evcc last requested."""
        amps = self.runtime.controller.journal.requested_amps
        return int(amps) if float(amps).is_integer() else amps

    async def async_set_native_value(self, value: float) -> None:
        """Record the requested current; the write happens in the background."""
        await self.runtime.async_request_amps(value)
