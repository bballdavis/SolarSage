"""Buttons: release, clear a fault and read the provider's schedules."""

from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import RivianSolarAdapterEntity
from .runtime import AdapterRuntime, RivianSolarAdapterConfigEntry, describe_schedules

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RivianSolarAdapterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the buttons."""
    runtime = entry.runtime_data
    async_add_entities(
        [ReleaseButton(runtime), ClearFaultButton(runtime), ReadSchedulesButton(runtime)]
    )


class ReleaseButton(RivianSolarAdapterEntity, ButtonEntity):
    """Restore the owner's schedule now."""

    def __init__(self, runtime: AdapterRuntime) -> None:
        super().__init__(runtime, "release")

    async def async_press(self) -> None:
        """Ask the next tick to restore the owner's schedule."""
        await self.runtime.async_request_release("manual")


class ClearFaultButton(RivianSolarAdapterEntity, ButtonEntity):
    """Resume after a fault once Rivian holds a schedule the adapter knows."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: AdapterRuntime) -> None:
        super().__init__(runtime, "clear_fault")

    async def async_press(self) -> None:
        """Clear the fault, or explain why it stays."""
        if not await self.runtime.async_clear_fault():
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="clear_fault_refused"
            )


class ReadSchedulesButton(RivianSolarAdapterEntity, ButtonEntity):
    """Read Rivian's schedule list and show it in a notification."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime: AdapterRuntime) -> None:
        super().__init__(runtime, "read_schedules")

    async def async_press(self) -> None:
        """Read the schedules; coordinates are never shown."""
        result = await self.runtime.async_read_schedules()
        persistent_notification.async_create(
            self.hass,
            describe_schedules(result),
            title=f"{self.runtime.entry.title} charging schedules",
            notification_id=f"{DOMAIN}_{self.runtime.entry.entry_id}_schedules",
        )
