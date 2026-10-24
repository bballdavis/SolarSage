"""Rivian (Unofficial)"""

from __future__ import annotations

import logging

from rivian import Rivian
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.storage import Store

from .const import (
    ATTR_API,
    ATTR_COORDINATOR,
    ATTR_SOLAR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    ATTR_WALLBOX,
    CONF_VEHICLE_CONTROL,
    DOMAIN,
    ISSUE_URL,
    VERSION,
)
from .coordinator import UserCoordinator, VehicleCoordinator, WallboxCoordinator
from .helpers import get_rivian_api_from_entry
from .schedule import canonical_schedule_hash
from .solar_ev_charging.const import (
    CONF_SOLAR_ENABLED,
    DOMAIN as SOLAR_DOMAIN,
    STORE_VERSION,
)
from .solar_ev_charging.coordinator import SolarEvCoordinator
from .solar_ev_charging.panel import async_register_panel, async_unregister_panel

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.DEVICE_TRACKER,
    Platform.IMAGE,
    Platform.LOCK,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
    Platform.UPDATE,
]
SERVICE_GET_SCHEDULES = "get_charging_schedules"
SERVICE_SET_SCHEDULES = "set_charging_schedules"
SERVICE_VEHICLE_ID = "vehicle_id"
SERVICE_SCHEDULES = "schedules"
SERVICE_EXPECTED_HASH = "expected_hash"
SERVICE_REASON = "reason"
_SERVICE_STATE = "_charging_schedule_services"


def _find_vehicle_coordinator(
    hass: HomeAssistant, vehicle_id: str
) -> VehicleCoordinator:
    """Find a loaded vehicle coordinator without logging its credentials."""
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(entry_data, dict):
            continue
        coordinators = entry_data.get(ATTR_COORDINATOR, {}).get(ATTR_VEHICLE, {})
        if vehicle_id in coordinators:
            return coordinators[vehicle_id]
    raise HomeAssistantError("Unknown Rivian vehicle_id")


async def _async_get_charging_schedules(call: ServiceCall) -> dict[str, object]:
    coordinator = _find_vehicle_coordinator(call.hass, call.data[SERVICE_VEHICLE_ID])
    schedules = await coordinator.async_get_charging_schedules(force_refresh=True)
    return {
        SERVICE_VEHICLE_ID: coordinator.vehicle_id,
        SERVICE_SCHEDULES: schedules,
        "hash": canonical_schedule_hash(schedules),
    }


async def _async_set_charging_schedules(call: ServiceCall) -> dict[str, object]:
    coordinator = _find_vehicle_coordinator(call.hass, call.data[SERVICE_VEHICLE_ID])
    try:
        schedules = await coordinator.async_set_charging_schedules(
            call.data[SERVICE_SCHEDULES], call.data.get(SERVICE_EXPECTED_HASH)
        )
    except Exception as err:
        raise HomeAssistantError(
            f"Unable to set Rivian charging schedules: {err}"
        ) from err
    return {
        SERVICE_VEHICLE_ID: coordinator.vehicle_id,
        SERVICE_SCHEDULES: schedules,
        "hash": canonical_schedule_hash(schedules),
    }


def _register_services(hass: HomeAssistant) -> None:
    if hass.data[DOMAIN].get(_SERVICE_STATE):
        hass.data[DOMAIN][_SERVICE_STATE] += 1
        return
    hass.data[DOMAIN][_SERVICE_STATE] = 1
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_SCHEDULES,
        _async_get_charging_schedules,
        schema=vol.Schema({vol.Required(SERVICE_VEHICLE_ID): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SCHEDULES,
        _async_set_charging_schedules,
        schema=vol.Schema(
            {
                vol.Required(SERVICE_VEHICLE_ID): cv.string,
                vol.Required(SERVICE_SCHEDULES): [dict],
                vol.Optional(SERVICE_EXPECTED_HASH): cv.string,
                vol.Optional(SERVICE_REASON): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )


def _unregister_services(hass: HomeAssistant) -> None:
    if DOMAIN not in hass.data or not hass.data[DOMAIN].get(_SERVICE_STATE):
        return
    hass.data[DOMAIN][_SERVICE_STATE] -= 1
    if not hass.data[DOMAIN][_SERVICE_STATE]:
        hass.services.async_remove(DOMAIN, SERVICE_GET_SCHEDULES)
        hass.services.async_remove(DOMAIN, SERVICE_SET_SCHEDULES)
        hass.data[DOMAIN].pop(_SERVICE_STATE, None)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the saved entries."""
    _LOGGER.info(
        "Rivian integration is starting under version %s. Please report issues at %s",
        VERSION,
        ISSUE_URL,
    )

    hass.data.setdefault(DOMAIN, {})

    client = get_rivian_api_from_entry(hass, entry)
    try:
        await client.create_csrf_token()
    except Exception as err:
        _LOGGER.error("Could not update Rivian Data: %s", err, exc_info=1)
        await client.close()
        raise ConfigEntryNotReady("Error communicating with API") from err

    coordinator = UserCoordinator(
        hass=hass, config_entry=entry, client=client, include_phones=True
    )
    await coordinator.async_config_entry_first_refresh()

    vehicle_control = entry.options.get(CONF_VEHICLE_CONTROL)
    if vehicle_control and not coordinator.data.get("registrationChannels"):
        vehicle_control = []
        async_create_issue(
            hass,
            DOMAIN,
            entry.entry_id,
            is_fixable=False,
            is_persistent=False,
            severity=IssueSeverity.WARNING,
            translation_key="2fa_missing",
        )
    else:
        async_delete_issue(hass, DOMAIN, entry.entry_id)

    vehicles = coordinator.get_vehicles()
    if vehicle_control and (
        enrolled := coordinator.get_enrolled_phone_data(entry.options.get("public_key"))
    ):
        for vehicle_id in vehicles:
            if vehicle_id in enrolled[1]:
                vehicles[vehicle_id]["phone_identity_id"] = enrolled[1][vehicle_id]

    vehicle_coordinators: dict[str, VehicleCoordinator] = {}
    for vehicle_id in vehicles:
        coor = VehicleCoordinator(
            hass=hass, config_entry=entry, client=client, vehicle_id=vehicle_id
        )
        await coor.async_config_entry_first_refresh()
        if not coor.data:
            raise ConfigEntryNotReady("Issue loading vehicle data")
        await coor.charging_coordinator.async_config_entry_first_refresh()
        await coor.drivers_coordinator.async_config_entry_first_refresh()
        vehicle_coordinators[vehicle_id] = coor

    wallbox_coordinator = WallboxCoordinator(
        hass=hass, config_entry=entry, client=client
    )
    await wallbox_coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        ATTR_API: client,
        ATTR_VEHICLE: vehicles,
        ATTR_COORDINATOR: {
            ATTR_USER: coordinator,
            ATTR_VEHICLE: vehicle_coordinators,
            ATTR_WALLBOX: wallbox_coordinator,
        },
    }
    _register_services(hass)

    # A failed cleanup must remain recoverable even if solar was disabled in options.
    saved_solar = await Store(
        hass, STORE_VERSION, f"{SOLAR_DOMAIN}.{entry.entry_id}"
    ).async_load()
    if entry.options.get(CONF_SOLAR_ENABLED, False) or saved_solar:
        solar_coordinator = SolarEvCoordinator(hass, entry)
        hass.data[DOMAIN][entry.entry_id][ATTR_SOLAR_COORDINATOR] = solar_coordinator
        # Recovery may call this entry's charging switch. Register entities before
        # recovery, while exposing only an inert startup state to their consumers.
        solar_coordinator.async_set_updated_data({"state": "starting"})

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if solar_coordinator := hass.data[DOMAIN][entry.entry_id].get(
        ATTR_SOLAR_COORDINATOR
    ):
        await solar_coordinator.async_initialize()
        await solar_coordinator.async_config_entry_first_refresh()

        async def stop_solar(_event) -> None:
            await solar_coordinator.async_shutdown()

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_solar)
        )

    # Setup must be reachable before required sensors and actuators are mapped.
    # Registering the UI does not create or enable a charging coordinator.
    await async_register_panel(hass, entry.entry_id)
    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    if solar_coordinator := entry_data.get(ATTR_SOLAR_COORDINATOR):
        await solar_coordinator.async_shutdown()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    api: Rivian = entry_data[ATTR_API]
    await api.close()

    if unload_ok:
        await async_unregister_panel(hass, entry.entry_id)
        _unregister_services(hass)
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    if public_key := entry.options.get("public_key"):
        client = get_rivian_api_from_entry(hass, entry)
        coordinator = UserCoordinator(
            hass=hass, config_entry=entry, client=client, include_phones=True
        )
        await coordinator.async_config_entry_first_refresh()

        if enrolled_data := coordinator.get_enrolled_phone_data(public_key=public_key):
            for identity_id in enrolled_data[1].values():
                await client.disenroll_phone(identity_id=identity_id)
        await client.close()


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    coordinators = hass.data[DOMAIN][config_entry.entry_id][ATTR_COORDINATOR]
    user_coordinator: UserCoordinator = coordinators[ATTR_USER]
    wallbox_coordinator: WallboxCoordinator = coordinators[ATTR_WALLBOX]

    vehicles = user_coordinator.get_vehicles().keys()
    wallboxes = {x["wallboxId"] for x in wallbox_coordinator.data}

    return not any(
        identifier
        for identifier in device_entry.identifiers
        if identifier[0] == DOMAIN and identifier[1] in vehicles | wallboxes
    )
