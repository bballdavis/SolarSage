"""Tests for setup, control through the entities, services, unload and removal."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from conftest import (
    CHARGER_STATE_ENTITY,
    COORDINATE_FRAGMENTS,
    EMAIL,
    LOCATION_ENTITY,
    OWNER_SCHEDULES,
    PLUGGED_ENTITY,
    TOKENS,
    VEHICLE_ID,
    VEHICLE_NAME,
    WALL_CONNECTOR_ENTITY,
    FakeRivian,
    make_entry,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    get_scheduled_timer_handles,
)
import pytest
from rivian.exceptions import RivianApiException, RivianUnauthenticated

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.rivian_solar_adapter.const import DOMAIN, TICK_INTERVAL, storage_key
from custom_components.rivian_solar_adapter.diagnostics import (
    async_get_config_entry_diagnostics,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load this repository's integration."""


def entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    """Find an adapter entity by its unique ID."""
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{VEHICLE_ID}_{key}")
    assert found is not None, key
    return found


def state(hass: HomeAssistant, platform: str, key: str) -> str:
    current = hass.states.get(entity_id(hass, platform, key))
    assert current is not None
    return current.state


async def call(hass: HomeAssistant, domain: str, service: str, platform: str, key: str, **data):
    await hass.services.async_call(
        domain,
        service,
        {ATTR_ENTITY_ID: entity_id(hass, platform, key), **data},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def start_charging(hass: HomeAssistant, amps: int = 16) -> None:
    """What evcc does: set the current, then turn charging on."""
    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=amps)
    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, "switch", "solar_charging")


async def set_up(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


# Setup ------------------------------------------------------------------


async def test_setup_creates_entities(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """All entities exist on one device and nothing is read before a request."""
    assert loaded_entry.state is ConfigEntryState.LOADED
    assert state(hass, "switch", "solar_charging") == "off"
    assert state(hass, "switch", "lease_renewal") == "on"
    assert state(hass, "number", "solar_current") == "8"
    assert state(hass, "sensor", "control_state") == "idle"
    assert state(hass, "sensor", "evcc_status") == "b"
    for platform, key in (
        ("button", "release"),
        ("button", "clear_fault"),
        ("button", "read_schedules"),
    ):
        assert state(hass, platform, key) == "unknown"

    number = hass.states.get(entity_id(hass, "number", "solar_current"))
    assert number.attributes["min"] == 8
    assert number.attributes["max"] == 48
    assert number.attributes["step"] == 2
    assert number.attributes["unit_of_measurement"] == "A"
    control = hass.states.get(entity_id(hass, "sensor", "control_state"))
    assert "lease_active" in control.attributes["options"]
    assert control.attributes["phase"] == "idle"

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, VEHICLE_ID)})
    assert device is not None
    assert device.name == f"{VEHICLE_NAME} solar adapter"
    assert device.manufacturer == "HASS-Solar-Rivian"
    assert device.model == "Rivian charge adapter"
    assert device.sw_version == "0.1.0"
    entities = er.async_entries_for_config_entry(er.async_get(hass), loaded_entry.entry_id)
    assert len(entities) == 8
    assert {entity.device_id for entity in entities} == {device.id}
    assert mock_rivian.reads == 0
    assert mock_rivian.init_kwargs[-1]["access_token"] == TOKENS["access_token"]


async def test_setup_not_ready_when_rivian_unreachable(
    hass: HomeAssistant, mock_rivian: FakeRivian
) -> None:
    """A failed session handshake retries later."""
    mock_rivian.csrf_error = RivianApiException("unreachable")
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unreadable_journal_is_kept(
    hass: HomeAssistant, mock_rivian: FakeRivian, hass_storage: dict
) -> None:
    """A journal that cannot be read stops setup instead of being discarded."""
    entry = make_entry()
    bad = {"version": 1, "minor_version": 1, "key": storage_key(entry.entry_id)}
    hass_storage[storage_key(entry.entry_id)] = bad | {"data": {"version": 99}}
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "journal" in (entry.reason or "")
    assert hass_storage[storage_key(entry.entry_id)]["data"] == {"version": 99}


# Control ----------------------------------------------------------------


async def test_switch_on_writes_lease(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian, hass_storage
) -> None:
    """Plugged in at home, turning charging on writes one active lease window."""
    await start_charging(hass, amps=17)

    assert state(hass, "switch", "solar_charging") == "on"
    assert state(hass, "number", "solar_current") == "16"
    assert state(hass, "sensor", "control_state") == "lease_active"
    assert len(mock_rivian.writes) == 1
    [lease] = mock_rivian.writes[0]
    assert lease["amperage"] == 16
    assert lease["enabled"] is True
    assert lease["duration"] == 25
    # The owner's home schedule location is reused for the lease.
    assert lease["location"] == OWNER_SCHEDULES[0]["location"]

    journal = hass_storage[storage_key(loaded_entry.entry_id)]["data"]
    assert journal["phase"] == "owned"
    assert journal["original"] == OWNER_SCHEDULES
    attributes = hass.states.get(entity_id(hass, "sensor", "control_state")).attributes
    assert attributes["lease_amps"] == 16
    assert attributes["owner_schedule_count"] == 2

    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, "switch", "solar_charging")
    assert state(hass, "switch", "solar_charging") == "off"
    # The pause write waits for the minimum write interval.
    assert len(mock_rivian.writes) == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(mock_rivian.writes) == 2
    assert state(hass, "sensor", "control_state") == "paused"


async def test_requests_do_not_wait_for_rivian(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """evcc's switch call returns while the provider read is still pending."""
    mock_rivian.read_gate = asyncio.Event()
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id(hass, "switch", "solar_charging")},
        blocking=True,
    )
    assert state(hass, "switch", "solar_charging") == "on"
    assert mock_rivian.reads == 1
    assert mock_rivian.writes == []

    # More requests while the tick waits are coalesced into one follow-up tick.
    for amps in (10, 12, 14):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id(hass, "number", "solar_current"), ATTR_VALUE: amps},
            blocking=True,
        )
    assert state(hass, "number", "solar_current") == "14"

    mock_rivian.read_gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(mock_rivian.writes) == 1
    assert state(hass, "sensor", "control_state") == "lease_active"


async def test_dry_run_writes_nothing(hass: HomeAssistant, mock_rivian: FakeRivian) -> None:
    """With dry run on (the default), requests never reach Rivian."""
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    await set_up(hass, make_entry(dry_run=True))

    await start_charging(hass)
    async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_rivian.reads == 0
    assert mock_rivian.writes == []
    assert state(hass, "sensor", "control_state") == "dry_run"
    attributes = hass.states.get(entity_id(hass, "sensor", "control_state")).attributes
    assert attributes["dry_run_target"]["amperage"] == 16
    assert "location" not in attributes["dry_run_target"]


async def test_evcc_status(hass: HomeAssistant, mock_rivian: FakeRivian) -> None:
    """a unless plugged in at home with the Wall Connector connected; c while charging."""
    hass.states.async_set(PLUGGED_ENTITY, "off")
    hass.states.async_set(LOCATION_ENTITY, "not_home")
    hass.states.async_set(CHARGER_STATE_ENTITY, "charging_ready")
    hass.states.async_set(WALL_CONNECTOR_ENTITY, "on")
    await set_up(hass, make_entry(wall_connector_entity=WALL_CONNECTOR_ENTITY))

    def status() -> str:
        return state(hass, "sensor", "evcc_status")

    assert status() == "a"
    hass.states.async_set(PLUGGED_ENTITY, "on")
    await hass.async_block_till_done()
    assert status() == "a"  # plugged in, but away
    hass.states.async_set(LOCATION_ENTITY, "home")
    await hass.async_block_till_done()
    assert status() == "b"
    hass.states.async_set(CHARGER_STATE_ENTITY, "charging_active")
    await hass.async_block_till_done()
    assert status() == "c"
    hass.states.async_set(WALL_CONNECTOR_ENTITY, "off")
    await hass.async_block_till_done()
    assert status() == "a"  # the Wall Connector sees no vehicle
    hass.states.async_set(WALL_CONNECTOR_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "unavailable")
    await hass.async_block_till_done()
    assert status() == "a"  # unknown location is not home
    hass.states.async_set(LOCATION_ENTITY, "home")
    hass.states.async_set(CHARGER_STATE_ENTITY, "unknown")
    await hass.async_block_till_done()
    assert status() == "b"
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_unplug_restores_owner_schedule(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """Unplugging for the release delay restores the owner's schedules exactly."""
    await start_charging(hass)
    hass.states.async_set(PLUGGED_ENTITY, "off")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_rivian.schedules != OWNER_SCHEDULES
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=75))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert state(hass, "sensor", "control_state") == "idle"
    assert state(hass, "sensor", "evcc_status") == "a"


async def test_release_button_restores(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian, hass_storage
) -> None:
    """The release button restores the owner's schedules exactly."""
    await start_charging(hass)
    assert state(hass, "sensor", "control_state") == "lease_active"

    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "release")

    assert mock_rivian.writes[-1] == OWNER_SCHEDULES
    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert state(hass, "sensor", "control_state") == "idle"
    assert state(hass, "switch", "solar_charging") == "off"
    assert hass_storage[storage_key(loaded_entry.entry_id)]["data"]["phase"] == "idle"


async def test_fault_and_clear(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """An edit outside the adapter faults; clearing needs a known provider state."""
    await start_charging(hass)
    lease = mock_rivian.schedules
    mock_rivian.schedules = [OWNER_SCHEDULES[1]]  # the owner edits the schedule
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "release")
    assert state(hass, "sensor", "control_state") == "fault"
    attributes = hass.states.get(entity_id(hass, "sensor", "control_state")).attributes
    assert attributes["fault_code"] == "external_edit"
    assert mock_rivian.writes[-1] == lease  # nothing was overwritten

    with pytest.raises(HomeAssistantError) as err:
        await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "clear_fault")
    assert err.value.translation_key == "clear_fault_refused"
    assert state(hass, "sensor", "control_state") == "fault"

    mock_rivian.schedules = lease
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "clear_fault")
    assert state(hass, "sensor", "control_state") == "lease_active"


async def test_lease_renewal_switch(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """With renewal off, an active lease is left to lapse."""
    await start_charging(hass)
    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, "switch", "lease_renewal")
    assert state(hass, "switch", "lease_renewal") == "off"
    assert loaded_entry.runtime_data.controller.renewal_enabled is False

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=21))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(mock_rivian.writes) == 1
    assert state(hass, "sensor", "control_state") == "lease_lapsed"

    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, "switch", "lease_renewal")
    assert loaded_entry.runtime_data.controller.renewal_enabled is True


async def test_read_schedules_button_notifies_without_coordinates(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """The read button shows each schedule, never its location."""
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "read_schedules")

    notifications = hass.data["persistent_notification"]
    [notification] = [
        value
        for key, value in notifications.items()
        if key == f"{DOMAIN}_{loaded_entry.entry_id}_schedules"
    ]
    message = notification["message"]
    assert "2 charging schedules" in message
    assert "Monday, Tuesday, Wednesday, Thursday, Friday from 00:00 for 6 h 0 min at 48 A" in (
        message
    )
    assert "Saturday from 10:00 for 1 h 30 min at 32 A, disabled" in message
    assert not any(fragment in message for fragment in COORDINATE_FRAGMENTS)
    attributes = hass.states.get(entity_id(hass, "sensor", "control_state")).attributes
    assert attributes["last_provider_read"]["count"] == 2


# Authentication ----------------------------------------------------------


async def test_auth_error_starts_reauth_once(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """A rejected session starts one reauth flow and stops hammering Rivian."""
    mock_rivian.read_error = RivianUnauthenticated(401, {})
    await start_charging(hass)

    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == SOURCE_REAUTH
    ]
    assert len(flows) == 1
    assert flows[0]["context"]["entry_id"] == loaded_entry.entry_id
    attributes = hass.states.get(entity_id(hass, "sensor", "control_state")).attributes
    assert attributes["reauth_required"] is True
    assert loaded_entry.state is ConfigEntryState.LOADED

    reads = mock_rivian.reads
    for _ in range(3):
        async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_rivian.reads == reads  # retried only after the backoff

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_rivian.reads == reads + 1
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == SOURCE_REAUTH
    ]
    assert len(flows) == 1


# Services ---------------------------------------------------------------


async def test_read_schedules_service(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """The read service returns the provider's schedules without locations."""
    response = await hass.services.async_call(
        DOMAIN,
        "read_schedules",
        {"config_entry_id": loaded_entry.entry_id},
        blocking=True,
        return_response=True,
    )
    assert response["count"] == 2
    assert len(response["hash"]) == 12
    assert response["schedules"][1] == {
        "weekDays": ["Saturday"],
        "startTime": 600,
        "duration": 90,
        "amperage": 32,
        "enabled": False,
    }
    assert not any(fragment in str(response) for fragment in COORDINATE_FRAGMENTS)


async def test_release_and_abandon_services(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """release restores; abandon forgets ownership without writing."""
    await start_charging(hass)
    await hass.services.async_call(
        DOMAIN, "release", {"config_entry_id": loaded_entry.entry_id}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_rivian.schedules == OWNER_SCHEDULES

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await start_charging(hass)
    assert state(hass, "sensor", "control_state") == "lease_active"
    writes = len(mock_rivian.writes)
    await hass.services.async_call(
        DOMAIN, "abandon_ownership", {"config_entry_id": loaded_entry.entry_id}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(mock_rivian.writes) == writes
    assert state(hass, "sensor", "control_state") == "idle"
    assert loaded_entry.runtime_data.controller.journal.original is None

    # clear_fault without a fault is a no-op that succeeds.
    await hass.services.async_call(
        DOMAIN, "clear_fault", {"config_entry_id": loaded_entry.entry_id}, blocking=True
    )


async def test_services_reject_unknown_and_unloaded_entries(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Services name the problem when the entry is unknown or not loaded."""
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "release", {"config_entry_id": "missing"}, blocking=True
        )
    assert err.value.translation_key == "entry_not_found"

    other = MockConfigEntry(domain="other_domain")
    other.add_to_hass(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "release", {"config_entry_id": other.entry_id}, blocking=True
        )

    await hass.config_entries.async_unload(loaded_entry.entry_id)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "read_schedules",
            {"config_entry_id": loaded_entry.entry_id},
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "entry_not_loaded"


# Diagnostics ------------------------------------------------------------


async def test_diagnostics_redacted(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """Diagnostics carry the journal but no tokens, email, vehicle ID or coordinates."""
    await start_charging(hass)
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "read_schedules")

    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_entry)
    text = str(diagnostics)
    for secret in (*TOKENS.values(), EMAIL, VEHICLE_ID, *COORDINATE_FRAGMENTS):
        assert secret not in text, secret
    assert str(hass.config.latitude)[:5] not in text
    assert diagnostics["journal"]["phase"] == "owned"
    assert len(diagnostics["journal"]["original"]) == 2
    assert diagnostics["journal"]["original"][0]["location"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["access_token"] == "**REDACTED**"
    assert diagnostics["control_state"] == "lease_active"
    assert diagnostics["controller"]["lease_amps"] == 16


# Unload and removal -----------------------------------------------------


async def test_unload_keeps_ownership_and_stops_ticking(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian, hass_storage
) -> None:
    """Unloading neither releases nor leaves timers behind; reloading resumes."""
    await start_charging(hass)
    writes = len(mock_rivian.writes)

    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.state is ConfigEntryState.NOT_LOADED
    assert hass_storage[storage_key(loaded_entry.entry_id)]["data"]["phase"] == "owned"
    assert not [
        handle
        for handle in get_scheduled_timer_handles(hass.loop)
        if not handle.cancelled() and DOMAIN in repr(handle)
    ]

    reads = mock_rivian.reads
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=30))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_rivian.reads == reads
    assert len(mock_rivian.writes) == writes

    assert await hass.config_entries.async_setup(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, "switch", "solar_charging") == "on"
    assert loaded_entry.runtime_data.controller.journal.original == OWNER_SCHEDULES


async def test_remove_restores_owner_schedule(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian, hass_storage
) -> None:
    """Removing the integration while it owns the schedule restores it."""
    await start_charging(hass)
    assert mock_rivian.schedules != OWNER_SCHEDULES

    await hass.config_entries.async_remove(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert storage_key(loaded_entry.entry_id) not in hass_storage
    assert f"{DOMAIN}_{loaded_entry.entry_id}_restore_failed" not in hass.data[
        "persistent_notification"
    ]


async def test_remove_keeps_journal_when_restore_fails(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian, hass_storage
) -> None:
    """If the restore fails, the journal stays and the owner is told."""
    await start_charging(hass)
    mock_rivian.write_error = RivianApiException("down")

    await hass.config_entries.async_remove(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_rivian.schedules != OWNER_SCHEDULES
    journal = hass_storage[storage_key(loaded_entry.entry_id)]["data"]
    assert journal["original"] == OWNER_SCHEDULES
    notification = hass.data["persistent_notification"][
        f"{DOMAIN}_{loaded_entry.entry_id}_restore_failed"
    ]
    assert "manual" in notification["title"]
    assert not any(fragment in notification["message"] for fragment in COORDINATE_FRAGMENTS)


async def test_remove_without_ownership_deletes_journal(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian, hass_storage
) -> None:
    """Without ownership, removal deletes the journal and talks to nobody."""
    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=20)
    assert storage_key(loaded_entry.entry_id) in hass_storage
    await hass.config_entries.async_remove(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert storage_key(loaded_entry.entry_id) not in hass_storage
    assert mock_rivian.reads == 0
