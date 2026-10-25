"""Rivian Solar Charge Adapter.

Turns a charging controller's start/stop and current requests (a switch and a
number entity; the controller is the solar planner) into Rivian charging-schedule
writes, with a persisted ownership journal and an exact restore of the owner's
schedules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
    split_entity_id,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util
import probatio as vol
from rivian import Rivian

from .api import AuthError, RivianScheduleApi, ScheduleApiError
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    CHARGER_STATUS_KEY,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    CONF_VEHICLE_ID,
    DOMAIN,
    ENTRY_MINOR_VERSION,
    ENTRY_VERSION,
    LEGACY_CHARGER_STATUS_KEY,
    LEGACY_OPTION_KEYS,
    PLATFORMS,
    REMOVE_RESTORE_TIMEOUT,
    REQUEST_TIMEOUT,
    SERVICE_ABANDON_OWNERSHIP,
    SERVICE_CLEAR_FAULT,
    SERVICE_FORCE_RESTORE,
    SERVICE_READ_SCHEDULES,
    SERVICE_RELEASE,
)
from .controller import Journal, Observation, Phase, RivianScheduleController
from .runtime import (
    AdapterRuntime,
    RivianSolarAdapterConfigEntry,
    adapter_config_from_entry,
    restore_failed_issue_id,
    restore_failed_notification_id,
    setup_failed_issue_id,
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


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an entry made by an earlier version of the adapter.

    Minor version 2 (the solar planner replaces evcc) renames the two
    controller options and the evcc status sensor. The sensor keeps its
    registry entry, so its history is kept. The tuning options that version
    adds take their defaults: the evcc-era increase margin and ceiling hold
    are off.
    """
    if entry.version > ENTRY_VERSION:
        return False  # made by a newer version of the adapter
    if entry.version == ENTRY_VERSION and entry.minor_version < 2:
        await _async_migrate_charger_status_entity(hass, entry)
        options = dict(entry.options)
        for old, new in LEGACY_OPTION_KEYS.items():
            if old in options:
                value = options.pop(old)
                options.setdefault(new, value)
        hass.config_entries.async_update_entry(
            entry, options=options, minor_version=ENTRY_MINOR_VERSION
        )
        _LOGGER.info(
            "Migrated the solar adapter for %s to version %s.%s",
            entry.title,
            ENTRY_VERSION,
            ENTRY_MINOR_VERSION,
        )
    return True


async def _async_migrate_charger_status_entity(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move the evcc status sensor's registry entry to the charger status unique ID.

    Its entity ID follows the new name unless it was changed by hand or the new
    one is taken; the recorder moves the history along with a changed entity ID.
    """
    vehicle_id: str = entry.data[CONF_VEHICLE_ID]
    old_unique_id = f"{vehicle_id}_{LEGACY_CHARGER_STATUS_KEY}"
    new_unique_id = f"{vehicle_id}_{CHARGER_STATUS_KEY}"
    registry = er.async_get(hass)

    @callback
    def migrate(entity_entry: er.RegistryEntry) -> dict[str, Any] | None:
        if entity_entry.domain != Platform.SENSOR or entity_entry.unique_id != old_unique_id:
            return None
        if registry.async_get_entity_id(Platform.SENSOR, DOMAIN, new_unique_id):
            _LOGGER.warning(
                "Not migrating %s: a charger status sensor is already registered",
                entity_entry.entity_id,
            )
            return None
        updates: dict[str, Any] = {"new_unique_id": new_unique_id}
        object_id = split_entity_id(entity_entry.entity_id)[1]
        legacy_suffix = f"_{LEGACY_CHARGER_STATUS_KEY}"
        if object_id.endswith(legacy_suffix):
            new_entity_id = (
                f"{Platform.SENSOR}.{object_id.removesuffix(legacy_suffix)}_{CHARGER_STATUS_KEY}"
            )
            if registry.async_get(new_entity_id) is None and hass.states.async_available(
                new_entity_id
            ):
                updates["new_entity_id"] = new_entity_id
        return updates

    await er.async_migrate_entries(hass, entry.entry_id, migrate)


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
    time_zone = hass.config.time_zone
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
        await _async_start_session(api)
    except (ConfigEntryNotReady, ConfigEntryAuthFailed, ConfigEntryError) as err:
        _async_report_setup_failure(hass, entry, controller.journal, store.relative_path, err)
        raise

    # The adapter is back in charge of the journal it could not restore.
    persistent_notification.async_dismiss(hass, restore_failed_notification_id(vehicle_id))
    ir.async_delete_issue(hass, DOMAIN, restore_failed_issue_id(vehicle_id))
    ir.async_delete_issue(hass, DOMAIN, setup_failed_issue_id(entry.entry_id))
    integration = await async_get_integration(hass, DOMAIN)
    runtime = AdapterRuntime(
        hass,
        entry,
        controller,
        journal_path=store.relative_path,
        sw_version=str(integration.version or "") or None,
        time_zone=time_zone,
    )
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.async_start()
    return True


async def _async_start_session(api: RivianScheduleApi) -> None:
    """Create the Rivian app session, mapping failures to setup exceptions."""
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


@callback
def _async_report_setup_failure(
    hass: HomeAssistant,
    entry: ConfigEntry,
    journal: Journal,
    path: str,
    err: Exception,
) -> None:
    """Raise a persistent repair issue when setup fails while the adapter owns the schedule.

    Home Assistant does not unload an entry that failed to set up, so neither a
    retry nor disabling it restores the owner's schedule; only removal does.
    The issue is persistent so it survives restarts and a later disable.
    """
    issue_id = setup_failed_issue_id(entry.entry_id)
    if journal.phase is Phase.IDLE:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    # The cause carries Rivian's error class or code, never a response body.
    cause = err.__cause__ or err
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="setup_failed_while_owning",
        translation_placeholders={
            "title": entry.title,
            "error": str(cause) or type(cause).__name__,
            "path": path,
        },
    )


@callback
def _async_warn_if_utc(hass: HomeAssistant) -> None:
    zone = hass.config.time_zone
    if zone.upper() in _UTC_ZONES or zone.startswith("Etc/"):
        _LOGGER.warning(
            "Home Assistant's time zone is %s. The vehicle evaluates charging schedules on "
            "its local clock, so set Home Assistant's time zone to the vehicle's; otherwise "
            "the adapter's charging windows are written at the wrong local time. A change "
            "takes effect automatically: the adapter reloads and rewrites its window",
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
        reason = await _async_restore_owner_schedule(runtime.controller, "integration disabled")
        if reason is not None:
            _async_notify_restore_failed(hass, entry, runtime.journal_path, reason, removed=False)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Restore the owner's schedule, best effort, when the adapter is removed.

    The journal is kept when it still matters, and a re-added entry for the
    same vehicle inherits it.
    """
    vehicle_id: str = entry.data[CONF_VEHICLE_ID]
    # The entry is gone, so its setup issue no longer applies. A failed restore
    # below raises its own persistent issue, which follows the vehicle.
    ir.async_delete_issue(hass, DOMAIN, setup_failed_issue_id(entry.entry_id))
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

    api = RivianScheduleApi(create_client(hass, entry.data), vehicle_id)
    controller = RivianScheduleController(api, store, adapter_config_from_entry(hass, entry))
    try:
        await controller.async_load()
    except (JournalLoadError, KeyError, TypeError, ValueError) as err:
        _async_notify_restore_failed(
            hass,
            entry,
            store.relative_path,
            f"the journal is unreadable: {str(err) or type(err).__name__}",
            removed=True,
        )
        return
    reason = await _async_restore_owner_schedule(
        controller, "integration removed", start_session=api.async_start_session
    )
    if reason is not None:
        _async_notify_restore_failed(hass, entry, store.relative_path, reason, removed=True)
        return
    _LOGGER.info("Owner's charging schedule restored on removal")
    await _async_finish_removal(hass, entry, store, controller.journal.to_dict())


async def _async_restore_owner_schedule(
    controller: RivianScheduleController,
    reason: str,
    *,
    start_session: Callable[[], Awaitable[None]] | None = None,
) -> str | None:
    """Restore the owner's schedule for a disable or removal, within a time limit.

    Releases without handing back, then ticks until the restore is settled.
    The observation is unknown: this release is explicit, not location based.
    The ticks ignore an error backoff left by earlier ticks: this restore is a
    single, bounded attempt.

    Returns None once the adapter owns nothing, or why the owner's schedule is
    not back. The reason comes from this attempt only, never from an error
    recorded before it.
    """
    error_before = (controller.last_error, controller.last_error_at)
    failure: str | None = None
    try:
        async with asyncio.timeout(REMOVE_RESTORE_TIMEOUT):
            if start_session is not None:
                await start_session()
            await controller.async_request_release(reason, hand_back=False)
            await controller.async_tick(dt_util.utcnow(), Observation(), force=True)
            if controller.journal.phase is Phase.WRITING:
                # The readback still showed the previous list; read again once settled.
                await asyncio.sleep(RESTORE_SETTLE_WAIT.total_seconds())
                await controller.async_tick(dt_util.utcnow(), Observation(), force=True)
    except Exception as err:
        failure = (
            f"the restore did not finish within {REMOVE_RESTORE_TIMEOUT} seconds"
            if isinstance(err, TimeoutError)
            else str(err) or type(err).__name__
        )
        _LOGGER.warning("Could not restore the owner's charging schedule (%s): %s", reason, failure)
    journal = controller.journal
    if journal.phase is Phase.IDLE and journal.original is None:
        return None
    if journal.fault is not None:
        return f"fault {journal.fault.code}: {journal.fault.message}"
    if failure is not None:
        return failure
    if (controller.last_error, controller.last_error_at) != error_before:
        # Recorded by this attempt's ticks.
        return controller.last_error
    return f"the restore was not confirmed; the adapter was still in phase {journal.phase.value}"


async def _async_finish_removal(
    hass: HomeAssistant, entry: ConfigEntry, store: JournalFileStore, data: dict[str, Any]
) -> None:
    """Delete a finished journal; keep one that holds an archived owner list."""
    ir.async_delete_issue(hass, DOMAIN, restore_failed_issue_id(entry.data[CONF_VEHICLE_ID]))
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
    # The notification is lost on restart; the issue is not. It is deleted when
    # an entry for this vehicle takes over the journal again.
    ir.async_create_issue(
        hass,
        DOMAIN,
        restore_failed_issue_id(entry.data[CONF_VEHICLE_ID]),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="restore_failed_removed" if removed else "restore_failed_disabled",
        translation_placeholders={"title": entry.title, "reason": reason, "path": path},
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
