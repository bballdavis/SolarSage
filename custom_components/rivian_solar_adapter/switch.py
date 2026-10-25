"""Switches: the controller's start/stop request and lease renewal."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import RivianSolarAdapterEntity
from .runtime import AdapterRuntime, RivianSolarAdapterConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RivianSolarAdapterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the switches."""
    runtime = entry.runtime_data
    async_add_entities([SolarChargingSwitch(runtime), LeaseRenewalSwitch(runtime)])


class SolarChargingSwitch(RivianSolarAdapterEntity, SwitchEntity):
    """Charging allowed or paused, set by the controller (the solar planner).

    Turning it on or off records the request and returns at once. The schedule
    write happens in the background.
    """

    def __init__(self, runtime: AdapterRuntime) -> None:
        super().__init__(runtime, "solar_charging")

    @property
    def is_on(self) -> bool:
        """Whether charging is requested."""
        return self.runtime.controller.journal.desired_charge is True

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Request charging."""
        await self.runtime.async_request_charge(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Request a pause."""
        await self.runtime.async_request_charge(False)


class LeaseRenewalSwitch(RivianSolarAdapterEntity, SwitchEntity):
    """Lease renewal; turn off only for the attended lease-expiry trial.

    The setting is kept in the journal, so it survives restarts.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: AdapterRuntime) -> None:
        super().__init__(runtime, "lease_renewal")

    @property
    def is_on(self) -> bool:
        """Whether an active lease is renewed before it lapses."""
        return self.runtime.controller.renewal_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Renew the lease."""
        await self.runtime.async_set_renewal(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Let the lease lapse."""
        await self.runtime.async_set_renewal(False)
