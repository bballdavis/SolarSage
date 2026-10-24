"""Rivian Solar Charge Adapter.

Turns evcc's start/stop and current requests (a switch and a number entity) into
Rivian charging-schedule writes, with a persisted ownership journal and an exact
restore of the owner's schedules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util
import probatio as vol
from rivian import Rivian
from rivian.exceptions import RivianApiException

from .api import RivianScheduleApi
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    CONF_VEHICLE_ID,
    DOMAIN,
    PLATFORMS,
    REMOVE_RESTORE_TIMEOUT,
    REQUEST_TIMEOUT,
    SERVICE_ABANDON_OWNERSHIP,
    SERVICE_CLEAR_FAULT,
    SERVICE_READ_SCHEDULES,
    SERVICE_RELEASE,
    STORAGE_VERSION,
    storage_key,
)
from .controller import Observation, Phase, RivianScheduleController
from .runtime import AdapterRuntime, RivianSolarAdapterConfigEntry, adapter_config_from_entry

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SCHEMA = vol.Schema({vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string})


def create_client(hass: HomeAssistant, data: Mapping[str, Any]) -> Rivian:
    """Create a Rivian client from the tokens stored in the entry."""
    return Rivian(
        request_timeout=REQUEST_TIMEOUT,
        session=async_get_clientsession(hass),
        access_token=data.get(CONF_ACCESS_TOKEN) or "",
        refresh_token=data.get(CONF_REFRESH_TOKEN) or "",
        user_session_token=data.get(CONF_USER_SESSION_TOKEN) or "",
    )


def create_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Create the ownership journal store; it holds schedule coordinates."""
    return Store(hass, STORAGE_VERSION, storage_key(entry_id), private=True, atomic_writes=True)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's services."""
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RivianSolarAdapterConfigEntry) -> bool:
    """Set up the adapter for one vehicle."""
    client = create_client(hass, entry.data)
    try:
        await client.create_csrf_token()
    except RivianApiException as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    store = create_store(hass, entry.entry_id)
    controller = RivianScheduleController(
        RivianScheduleApi(client, entry.data[CONF_VEHICLE_ID]),
        store,
        adapter_config_from_entry(hass, entry),
    )
    try:
        await controller.async_load()
    except (HomeAssistantError, KeyError, TypeError, ValueError) as err:
        # Never start over without the journal: it may hold the owner's
        # original schedules. The file is left untouched for inspection.
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="journal_unreadable",
            translation_placeholders={"path": f".storage/{store.key}", "error": str(err)},
        ) from err

    integration = await async_get_integration(hass, DOMAIN)
    runtime = AdapterRuntime(
        hass, entry, controller, sw_version=str(integration.version or "") or None
    )
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RivianSolarAdapterConfigEntry) -> bool:
    """Unload the adapter.

    Ownership is deliberately kept: the journal persists, the lease lapses on
    its own if it is not renewed, and control resumes after a restart.
    """
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Restore the owner's schedule, best effort, when the adapter is removed."""
    store = create_store(hass, entry.entry_id)
    try:
        data = await store.async_load()
    except HomeAssistantError as err:
        _async_notify_restore_failed(hass, entry, store, f"the journal is unreadable: {err}")
        return
    if not data or (data.get("phase") == Phase.IDLE.value and data.get("original") is None):
        await store.async_remove()
        return

    controller: RivianScheduleController | None = None
    try:
        async with asyncio.timeout(REMOVE_RESTORE_TIMEOUT):
            client = create_client(hass, entry.data)
            await client.create_csrf_token()
            controller = RivianScheduleController(
                RivianScheduleApi(client, entry.data[CONF_VEHICLE_ID]),
                store,
                adapter_config_from_entry(hass, entry),
            )
            await controller.async_load()
            await controller.async_request_release("integration removed")
            # Unknown observations: the release is explicit, not location based.
            await controller.async_tick(dt_util.now(), Observation())
    except Exception as err:
        _LOGGER.exception("Could not restore the owner's charging schedule on removal")
        _async_notify_restore_failed(hass, entry, store, str(err) or type(err).__name__)
        return

    journal = controller.journal
    if journal.phase is Phase.IDLE and journal.original is None:
        await store.async_remove()
        _LOGGER.info("Owner's charging schedule restored; adapter journal removed")
        return
    reason = (
        journal.fault.message
        if journal.fault
        else controller.last_error or f"the adapter was still in phase {journal.phase.value}"
    )
    _async_notify_restore_failed(hass, entry, store, reason)


@callback
def _async_notify_restore_failed(
    hass: HomeAssistant, entry: ConfigEntry, store: Store[dict[str, Any]], reason: str
) -> None:
    persistent_notification.async_create(
        hass,
        (
            f"The Rivian Solar Charge Adapter for {entry.title} was removed while it "
            "still controlled the vehicle's charging schedule, and the original "
            f"schedule could not be restored automatically ({reason}).\n\n"
            "Check the charging schedules in the Rivian app and restore them by hand. "
            "The adapter's journal, which holds the original schedules, was kept at "
            f"`.storage/{store.key}`; delete it once the schedules are correct."
        ),
        title="Rivian charging schedule may need manual restoration",
        notification_id=f"{DOMAIN}_{entry.entry_id}_restore_failed",
    )


# Services ----------------------------------------------------------------


def _async_get_runtime(hass: HomeAssistant, call: ServiceCall) -> AdapterRuntime:
    entry_id: str = call.data[ATTR_CONFIG_ENTRY_ID]
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"title": entry.title},
        )
    return entry.runtime_data


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    async def async_release(call: ServiceCall) -> None:
        await _async_get_runtime(hass, call).async_request_release("service")

    async def async_clear_fault(call: ServiceCall) -> None:
        if not await _async_get_runtime(hass, call).async_clear_fault():
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="clear_fault_refused"
            )

    async def async_abandon(call: ServiceCall) -> None:
        await _async_get_runtime(hass, call).async_abandon()

    async def async_read_schedules(call: ServiceCall) -> ServiceResponse:
        return await _async_get_runtime(hass, call).async_read_schedules()

    hass.services.async_register(DOMAIN, SERVICE_RELEASE, async_release, SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_FAULT, async_clear_fault, SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_ABANDON_OWNERSHIP, async_abandon, SERVICE_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_SCHEDULES,
        async_read_schedules,
        SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
