"""Rivian Solar Charge Adapter.

Turns evcc's start/stop and current requests (a switch and a number entity) into
Rivian charging-schedule writes, with a persisted ownership journal and an exact
restore of the owner's schedules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
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
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util
import probatio as vol
from rivian import Rivian

from .api import AuthError, RivianScheduleApi, ScheduleApiError
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
    SERVICE_FORCE_RESTORE,
    SERVICE_READ_SCHEDULES,
    SERVICE_RELEASE,
)
from .controller import Observation, Phase, RivianScheduleController
from .runtime import (
    AdapterRuntime,
    RivianSolarAdapterConfigEntry,
    adapter_config_from_entry,
    restore_failed_notification_id,
)
from .store import JournalFileStore, JournalLoadError, async_check_legacy_journal

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SCHEMA = vol.Schema({vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string})

# Time zones that are almost certainly not the vehicle's local time.
_UTC_ZONES = frozenset({"UTC", "UCT", "GMT", "ZULU", "UNIVERSAL", "GREENWICH"})
# A disable or removal reads a restore back once more after this long when the
# first readback still showed the lease; it is a little over the controller's
# settle delay, and both reads fit in REMOVE_RESTORE_TIMEOUT.
RESTORE_SETTLE_WAIT = timedelta(seconds=31)


def create_client(hass: HomeAssistant, data: Mapping[str, Any]) -> Rivian:
    """Create a Rivian client from the tokens stored in the entry."""
    return Rivian(
        request_timeout=REQUEST_TIMEOUT,
        session=async_get_clientsession(hass),
        access_token=data.get(CONF_ACCESS_TOKEN) or "",
        refresh_token=data.get(CONF_REFRESH_TOKEN) or "",
        user_session_token=data.get(CONF_USER_SESSION_TOKEN) or "",
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's services."""
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RivianSolarAdapterConfigEntry) -> bool:
    """Set up the adapter for one vehicle."""
    vehicle_id: str = entry.data[CONF_VEHICLE_ID]
    _async_warn_if_utc(hass)
    if legacy := await async_check_legacy_journal(hass, entry.entry_id):
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="legacy_journal",
            translation_placeholders={"path": legacy},
        )

    store = JournalFileStore(hass, vehicle_id)
    api = RivianScheduleApi(create_client(hass, entry.data), vehicle_id)
    controller = RivianScheduleController(api, store, adapter_config_from_entry(hass, entry))
    # Never start over without the journal: it may hold the owner's original
    # schedules. An unreadable file is left untouched for inspection.
    try:
        await controller.async_load()
    except (JournalLoadError, KeyError, TypeError, ValueError) as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="journal_unreadable",
            translation_placeholders={
                "path": store.relative_path,
                "error": str(err) or type(err).__name__,
            },
        ) from err

    try:
        await api.async_start_session()
    except AuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="reauth_required"
        ) from err
    except ScheduleApiError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    # The adapter is back in charge of the journal it could not restore.
    persistent_notification.async_dismiss(hass, restore_failed_notification_id(vehicle_id))
    integration = await async_get_integration(hass, DOMAIN)
    runtime = AdapterRuntime(
        hass,
        entry,
        controller,
        journal_path=store.relative_path,
        sw_version=str(integration.version or "") or None,
    )
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.async_start()
    return True


@callback
def _async_warn_if_utc(hass: HomeAssistant) -> None:
    zone = hass.config.time_zone
    if zone.upper() in _UTC_ZONES or zone.startswith("Etc/"):
        _LOGGER.warning(
            "Home Assistant's time zone is %s. The vehicle evaluates charging schedules on "
            "its local clock, so set Home Assistant's time zone to the vehicle's; otherwise "
            "the adapter's charging windows are written at the wrong local time",
            zone,
        )


async def async_unload_entry(hass: HomeAssistant, entry: RivianSolarAdapterConfigEntry) -> bool:
    """Unload the adapter.

    A reload or restart keeps ownership: the journal persists, the lease
    lapses on its own if it is not renewed, and control resumes on setup.
    Disabling the entry restores the owner's schedule first, best effort.
    """
    runtime = entry.runtime_data
    await runtime.async_stop()
    if entry.disabled_by is not None and runtime.controller.journal.phase is not Phase.IDLE:
        try:
            async with asyncio.timeout(REMOVE_RESTORE_TIMEOUT):
                await _async_restore_owner_schedule(runtime.controller, "integration disabled")
        except Exception as err:
            _LOGGER.warning(
                "Could not restore the owner's charging schedule on disable: %s",
                str(err) or type(err).__name__,
            )
        if (reason := _unrestored_reason(runtime.controller)) is not None:
            _async_notify_restore_failed(hass, entry, runtime.journal_path, reason, removed=False)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Restore the owner's schedule, best effort, when the adapter is removed.

    The journal is kept when it still matters, and a re-added entry for the
    same vehicle inherits it.
    """
    vehicle_id: str = entry.data[CONF_VEHICLE_ID]
    store = JournalFileStore(hass, vehicle_id)
    try:
        data = await store.async_load()
    except JournalLoadError as err:
        _async_notify_restore_failed(
            hass, entry, store.relative_path, f"the journal is unreadable: {err}", removed=True
        )
        return
    if not data:
        return
    if data.get("phase") == Phase.IDLE.value and data.get("original") is None:
        await _async_finish_removal(hass, entry, store, data)
        return

    controller: RivianScheduleController | None = None
    try:
        async with asyncio.timeout(REMOVE_RESTORE_TIMEOUT):
            api = RivianScheduleApi(create_client(hass, entry.data), vehicle_id)
            controller = RivianScheduleController(
                api, store, adapter_config_from_entry(hass, entry)
            )
            await controller.async_load()
            await api.async_start_session()
            await _async_restore_owner_schedule(controller, "integration removed")
    except Exception as err:
        _LOGGER.warning(
            "Could not restore the owner's charging schedule on removal: %s",
            str(err) or type(err).__name__,
        )
        reason = (controller and _unrestored_reason(controller)) or str(err) or type(err).__name__
        _async_notify_restore_failed(hass, entry, store.relative_path, reason, removed=True)
        return

    if (reason := _unrestored_reason(controller)) is not None:
        _async_notify_restore_failed(hass, entry, store.relative_path, reason, removed=True)
        return
    _LOGGER.info("Owner's charging schedule restored on removal")
    await _async_finish_removal(hass, entry, store, controller.journal.to_dict())


async def _async_restore_owner_schedule(controller: RivianScheduleController, reason: str) -> None:
    """Release without handing back, then tick until the restore is settled.

    The observation is unknown: this release is explicit, not location based.
    The caller bounds the time this takes.
    """
    await controller.async_request_release(reason, hand_back=False)
    await controller.async_tick(dt_util.utcnow(), Observation())
    if controller.journal.phase is Phase.WRITING:
        # The readback still showed the previous list; read again once settled.
        await asyncio.sleep(RESTORE_SETTLE_WAIT.total_seconds())
        await controller.async_tick(dt_util.utcnow(), Observation())


def _unrestored_reason(controller: RivianScheduleController) -> str | None:
    """Why the owner's schedule is not back, or None if the adapter owns nothing."""
    journal = controller.journal
    if journal.phase is Phase.IDLE and journal.original is None:
        return None
    if journal.fault is not None:
        return f"fault {journal.fault.code}: {journal.fault.message}"
    return controller.last_error or f"the adapter was still in phase {journal.phase.value}"


async def _async_finish_removal(
    hass: HomeAssistant, entry: ConfigEntry, store: JournalFileStore, data: dict[str, Any]
) -> None:
    """Delete a finished journal; keep one that holds an archived owner list."""
    if data.get("archived_original") is None:
        await store.async_remove()
        return
    persistent_notification.async_create(
        hass,
        (
            f"The Rivian Solar Charge Adapter for {entry.title} was removed. Its journal at "
            f"`{store.relative_path}` was kept because it holds the owner's charging "
            "schedules archived by abandon ownership. Delete the file once the schedules in "
            "the Rivian app are correct; adding the adapter again for this vehicle reuses it."
        ),
        title="Rivian solar adapter journal kept",
        notification_id=restore_failed_notification_id(entry.data[CONF_VEHICLE_ID]),
    )


@callback
def _async_notify_restore_failed(
    hass: HomeAssistant, entry: ConfigEntry, path: str, reason: str, *, removed: bool
) -> None:
    action = "removed" if removed else "disabled"
    then = (
        "Adding the adapter again for this vehicle reuses the journal and resumes the restore."
        if removed
        else "Enabling the adapter again resumes the restore."
    )
    persistent_notification.async_create(
        hass,
        (
            f"The Rivian Solar Charge Adapter for {entry.title} was {action} while it "
            "controlled the vehicle's charging schedule, and the owner's schedules could "
            f"not be restored automatically ({reason}).\n\n"
            "Until they are restored, the vehicle keeps the adapter's short charging "
            "window, which recurs weekly. Check the charging schedules in the Rivian app "
            f"and restore them by hand. {then} The adapter's journal, which holds the "
            f"owner's schedules, was kept at `{path}`; delete it once the schedules are "
            "correct."
        ),
        title="Rivian charging schedule may need manual restoration",
        notification_id=restore_failed_notification_id(entry.data[CONF_VEHICLE_ID]),
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

    async def async_force_restore(call: ServiceCall) -> None:
        await _async_get_runtime(hass, call).async_force_restore()

    async def async_read_schedules(call: ServiceCall) -> ServiceResponse:
        return await _async_get_runtime(hass, call).async_read_schedules()

    hass.services.async_register(DOMAIN, SERVICE_RELEASE, async_release, SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_FAULT, async_clear_fault, SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_ABANDON_OWNERSHIP, async_abandon, SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_FORCE_RESTORE, async_force_restore, SERVICE_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_SCHEDULES,
        async_read_schedules,
        SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
