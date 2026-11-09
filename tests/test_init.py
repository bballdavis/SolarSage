"""Tests for setup, control through the entities, services, unload and removal."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
import logging
from typing import Any
from unittest.mock import patch

from conftest import (
    CHARGER_STATE_ENTITY,
    CHARGING_ENTITY,
    COORDINATE_FRAGMENTS,
    EMAIL,
    LIVENESS_ENTITY,
    LOCATION_ENTITY,
    OVERRIDE_ENTITY,
    OWNER_SCHEDULES,
    PLUGGED_ENTITY,
    TOKENS,
    VEHICLE_ID,
    VEHICLE_NAME,
    WALL_CONNECTOR_ENTITY,
    FakeResponse,
    FakeRivian,
    content_type_error,
    journal_path,
    make_entry,
    read_journal,
)
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_HOMEASSISTANT_STOP,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.core import CoreState, HassJob, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    get_scheduled_timer_handles,
)
from rivian.exceptions import RivianApiException, RivianBadRequestError, RivianUnauthenticated

from custom_components.solar_ev_planner.rivian.const import (
    DOMAIN,
    TICK_INTERVAL,
    journal_key,
)
from custom_components.solar_ev_planner.rivian.controller import Observation
from custom_components.solar_ev_planner.rivian.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.solar_ev_planner.rivian.runtime import (
    restore_failed_issue_id,
    restore_failed_notification_id,
    setup_failed_issue_id,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load this repository's integration."""


def entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    """Find a schedule control entity by its unique ID."""
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{VEHICLE_ID}_{key}")
    assert found is not None, key
    return found


def state(hass: HomeAssistant, platform: str, key: str) -> str:
    current = hass.states.get(entity_id(hass, platform, key))
    assert current is not None
    return current.state


def attributes(hass: HomeAssistant) -> dict[str, Any]:
    return dict(hass.states.get(entity_id(hass, "sensor", "control_state")).attributes)


def notifications(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    return hass.data.get("persistent_notification", {})


async def call(
    hass: HomeAssistant, domain: str, service: str, platform: str, key: str, **data: Any
) -> None:
    """Call an entity service and let the ticks it schedules finish."""
    await hass.services.async_call(
        domain,
        service,
        {ATTR_ENTITY_ID: entity_id(hass, platform, key), **data},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def call_schedule_control(hass: HomeAssistant, service: str, entry: MockConfigEntry) -> Any:
    """Call one of schedule control's own services and let its ticks finish."""
    response = await hass.services.async_call(
        DOMAIN,
        service,
        {"config_entry_id": entry.entry_id},
        blocking=True,
        return_response=service == "read_schedules",
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    return response


async def start_charging(hass: HomeAssistant) -> None:
    """Turn charging on at the default current (8 A)."""
    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, "switch", "solar_charging")


async def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    """Move the clock forward, fire the periodic tick and let it finish."""
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def set_up(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def fault_by_owner_edit(hass: HomeAssistant, mock_rivian: FakeRivian) -> list[dict]:
    """Take control, then let the owner edit the schedule so a release faults."""
    await start_charging(hass)
    lease = mock_rivian.schedules
    mock_rivian.schedules = [OWNER_SCHEDULES[1]]
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "release")
    assert state(hass, "sensor", "control_state") == "fault"
    return lease


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
    assert state(hass, "sensor", "charger_status") == "b"
    for platform, key in (
        ("button", "release"),
        ("button", "clear_fault"),
        ("button", "read_schedules"),
    ):
        assert state(hass, platform, key) == "unknown"

    number = hass.states.get(entity_id(hass, "number", "solar_current"))
    assert number.attributes["min"] == 0
    assert number.attributes["max"] == 80
    assert number.attributes["step"] == 1
    assert number.attributes["unit_of_measurement"] == "A"
    control = hass.states.get(entity_id(hass, "sensor", "control_state"))
    assert {"lease_active", "handed_back"} <= set(control.attributes["options"])
    assert control.attributes["phase"] == "idle"

    [device] = dr.async_entries_for_config_entry(dr.async_get(hass), loaded_entry.entry_id)
    assert device.identifiers == {(DOMAIN, VEHICLE_ID)}
    assert device.name == f"{VEHICLE_NAME} schedule control"
    assert device.manufacturer == "SolarSage"
    assert device.model == "Rivian schedule control"
    assert device.sw_version == "0.2.0"
    entities = er.async_entries_for_config_entry(er.async_get(hass), loaded_entry.entry_id)
    assert len(entities) == 8
    assert {entity.device_id for entity in entities} == {device.id}
    assert mock_rivian.reads == 0
    assert mock_rivian.init_kwargs[-1]["access_token"] == TOKENS["access_token"]
    # The controller builds windows on Home Assistant's local clock.
    controller = loaded_entry.runtime_data.controller
    assert controller.config.time_zone == dt_util.get_time_zone(hass.config.time_zone)


@pytest.mark.parametrize(
    "error",
    [RivianApiException("unreachable"), content_type_error(), TimeoutError()],
    ids=["api", "html", "timeout"],
)
async def test_setup_not_ready_when_rivian_unreachable(
    hass: HomeAssistant, mock_rivian: FakeRivian, error: Exception
) -> None:
    """A failed session handshake, an HTML error page included, retries later."""
    mock_rivian.csrf_error = error
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    # Without ownership there is nothing for the owner to do.
    assert ir.async_get(hass).async_get_issue(DOMAIN, setup_failed_issue_id(entry.entry_id)) is None


async def test_setup_rejected_session_starts_reauth(
    hass: HomeAssistant, mock_rivian: FakeRivian
) -> None:
    """A session Rivian rejects at setup asks for a new login."""
    mock_rivian.csrf_error = RivianUnauthenticated(401, {})
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == SOURCE_REAUTH
    ]


async def test_utc_time_zone_warns(
    hass: HomeAssistant, mock_rivian: FakeRivian, caplog: pytest.LogCaptureFixture
) -> None:
    """A UTC time zone is almost never the vehicle's, so setup warns about it."""
    await hass.config.async_set_time_zone("Etc/UTC")
    await set_up(hass, make_entry())
    assert "time zone is Etc/UTC" in caplog.text
    assert "A change takes effect automatically" in caplog.text


async def test_time_zone_change_reloads_and_rewrites_window(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A new Home Assistant time zone reloads schedule control, which moves its window."""
    await start_charging(hass)
    runtime = loaded_entry.runtime_data
    [lease] = mock_rivian.writes[-1]

    # Other core settings leave schedule control alone.
    await hass.config.async_update(location_name="Test home")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.runtime_data is runtime

    await hass.config.async_update(time_zone="America/Chicago")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.state is ConfigEntryState.LOADED
    assert loaded_entry.runtime_data is not runtime
    controller = loaded_entry.runtime_data.controller
    assert controller.config.time_zone == dt_util.get_time_zone("America/Chicago")

    # Ownership is kept, and the same lease now starts two hours later on the
    # vehicle's wall clock (Chicago is two hours ahead of the test's US/Pacific).
    assert controller.journal.original == OWNER_SCHEDULES
    assert len(mock_rivian.writes) == 2
    [moved] = mock_rivian.writes[-1]
    assert (moved["startTime"] - lease["startTime"]) % (24 * 60) == 120
    assert moved["amperage"] == lease["amperage"]
    assert state(hass, "sensor", "control_state") == "lease_active"


@pytest.mark.parametrize(
    ("error", "entry_state", "reason"),
    [
        (
            RivianApiException("unreachable"),
            ConfigEntryState.SETUP_RETRY,
            "Rivian request failed (RivianApiException)",
        ),
        (
            RivianUnauthenticated(401, {}),
            ConfigEntryState.SETUP_ERROR,
            "The Rivian session is unauthenticated",
        ),
    ],
    ids=["not_ready", "auth_failed"],
)
async def test_setup_failure_while_owning_raises_persistent_issue(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    error: Exception,
    entry_state: ConfigEntryState,
    reason: str,
) -> None:
    """A setup that fails while schedule control owns the schedule raises a lasting issue."""
    issues = ir.async_get(hass)
    issue_id = setup_failed_issue_id(loaded_entry.entry_id)
    await start_charging(hass)
    mock_rivian.csrf_error = error
    await hass.config_entries.async_reload(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.state is entry_state

    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.is_persistent is True
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == "setup_failed_while_owning"
    assert issue.translation_placeholders == {
        "title": VEHICLE_NAME,
        "error": reason,
        "path": f".storage/{journal_key(VEHICLE_ID)}",
    }

    # Home Assistant does not unload an entry whose setup failed, so disabling it
    # restores nothing; the issue stays.
    writes = len(mock_rivian.writes)
    assert await hass.config_entries.async_set_disabled_by(
        loaded_entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(mock_rivian.writes) == writes
    assert read_journal(hass)["phase"] == "owned"
    assert issues.async_get_issue(DOMAIN, issue_id) is not None

    # Once Rivian accepts the session again, a successful setup removes it.
    mock_rivian.csrf_error = None
    assert await hass.config_entries.async_set_disabled_by(loaded_entry.entry_id, None)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.state is ConfigEntryState.LOADED
    assert issues.async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.usefixtures("fast_restore")
async def test_remove_after_setup_failure_restores_and_clears_issue(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """Removal, which the issue points to, restores the schedule and ends the issue."""
    issue_id = setup_failed_issue_id(loaded_entry.entry_id)
    await start_charging(hass)
    mock_rivian.csrf_error = RivianApiException("unreachable")
    await hass.config_entries.async_reload(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    mock_rivian.csrf_error = None
    await hass.config_entries.async_remove(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert not journal_path(hass).exists()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# Control ----------------------------------------------------------------


async def test_switch_on_writes_lease(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Plugged in at home, turning charging on writes one active lease window."""
    await start_charging(hass)

    assert state(hass, "switch", "solar_charging") == "on"
    assert state(hass, "sensor", "control_state") == "lease_active"
    assert len(mock_rivian.writes) == 1
    [lease] = mock_rivian.writes[0]
    assert lease["amperage"] == 8
    assert lease["enabled"] is True
    assert lease["duration"] == 30  # 20 min lease plus 5 min of clock tolerance at each end
    # The owner's home schedule location is reused for the lease.
    assert lease["location"] == OWNER_SCHEDULES[0]["location"]

    journal = read_journal(hass)
    assert journal["phase"] == "owned"
    assert journal["original"] == OWNER_SCHEDULES
    assert attributes(hass)["lease_amps"] == 8
    assert attributes(hass)["owner_schedule_count"] == 2

    # The request is shown as sent. Increases wait for the increase interval,
    # then apply the request exactly: the planner asks for exact currents.
    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=20)
    assert state(hass, "number", "solar_current") == "20"
    assert len(mock_rivian.writes) == 1
    await advance(hass, freezer, timedelta(seconds=110))
    assert len(mock_rivian.writes) == 1  # the increase interval runs from the start
    await advance(hass, freezer, TICK_INTERVAL)
    assert len(mock_rivian.writes) == 2
    assert mock_rivian.writes[-1][0]["amperage"] == 20

    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=48)
    await advance(hass, freezer, timedelta(seconds=125))
    assert mock_rivian.writes[-1][0]["amperage"] == 48
    assert attributes(hass)["lease_amps"] == 48

    # A decrease applies at once, rounded down to an even current.
    await advance(hass, freezer, timedelta(seconds=31))
    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=11)
    assert mock_rivian.writes[-1][0]["amperage"] == 10

    writes = len(mock_rivian.writes)
    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, "switch", "solar_charging")
    assert state(hass, "switch", "solar_charging") == "off"
    assert len(mock_rivian.writes) == writes  # a pause waits 5 s after the last write
    await advance(hass, freezer, TICK_INTERVAL)
    assert len(mock_rivian.writes) == writes + 1
    assert state(hass, "sensor", "control_state") == "paused"


async def test_evcc_increase_rules_are_options(
    hass: HomeAssistant, mock_rivian: FakeRivian, freezer: FrozenDateTimeFactory
) -> None:
    """With evcc's margin and ceiling hold set, increases land below the request again."""
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    entry = make_entry(increase_margin_amps=3, ceiling_hold_minutes=30)
    await set_up(hass, entry)
    config = entry.runtime_data.controller.config
    assert config.increase_margin_amps == 3
    assert config.ceiling_hold == timedelta(minutes=30)

    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=48)
    assert mock_rivian.writes[-1][0]["amperage"] == 48
    # Just short of 48 A, then 48 A again: the margin is held at the ceiling.
    await advance(hass, freezer, timedelta(seconds=31))
    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=46)
    assert mock_rivian.writes[-1][0]["amperage"] == 46
    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=48)
    await advance(hass, freezer, timedelta(minutes=3))
    assert mock_rivian.writes[-1][0]["amperage"] == 46  # 48 A minus 3 A is no increase
    # Lower requests keep the margin too: 20 A requested is written as 16 A.
    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=12)
    assert mock_rivian.writes[-1][0]["amperage"] == 12
    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=20)
    await advance(hass, freezer, timedelta(minutes=3))
    assert mock_rivian.writes[-1][0]["amperage"] == 16


async def test_current_counts_as_start(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """A current request alone starts charging unless charging was turned off.

    evcc set the current before it enabled charging; the planner turns the
    switch on or off explicitly.
    """
    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=16)
    assert len(mock_rivian.writes) == 1
    assert mock_rivian.writes[-1][0]["amperage"] == 16
    assert state(hass, "sensor", "control_state") == "lease_active"
    assert state(hass, "switch", "solar_charging") == "on"

    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, "switch", "solar_charging")
    assert len(mock_rivian.writes) == 1


async def test_requests_do_not_wait_for_rivian(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """The controller's calls return while the provider read is still pending."""
    controller = loaded_entry.runtime_data.controller
    mock_rivian.read_gate = asyncio.Event()
    with patch.object(controller, "async_tick", wraps=controller.async_tick) as tick:
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: entity_id(hass, "switch", "solar_charging")},
            blocking=True,
        )
        assert state(hass, "switch", "solar_charging") == "on"
        assert mock_rivian.reads == 1
        assert mock_rivian.writes == []

        # Requests while the tick waits are coalesced into one follow-up tick.
        for amps in (10, 12, 14):
            await hass.services.async_call(
                NUMBER_DOMAIN,
                SERVICE_SET_VALUE,
                {ATTR_ENTITY_ID: entity_id(hass, "number", "solar_current"), ATTR_VALUE: amps},
                blocking=True,
            )
        assert state(hass, "number", "solar_current") == "14"
        assert tick.call_count == 1

        mock_rivian.read_gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert tick.call_count == 2

    assert len(mock_rivian.writes) == 1
    assert mock_rivian.writes[0][0]["amperage"] == 14
    assert state(hass, "sensor", "control_state") == "lease_active"


async def test_release_while_taking_over_cancels_it(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """A release pressed while the first read is under way stops the takeover."""
    mock_rivian.read_gate = asyncio.Event()
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id(hass, "switch", "solar_charging")},
        blocking=True,
    )
    assert mock_rivian.reads == 1
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: entity_id(hass, "button", "release")},
        blocking=True,
    )
    mock_rivian.read_gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_rivian.writes == []
    assert state(hass, "sensor", "control_state") == "handed_back"
    assert read_journal(hass)["phase"] == "idle"


async def test_request_while_away_is_not_acted_on_later(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A command for a car that is not here does not cause a takeover at plug-in."""
    hass.states.async_set(PLUGGED_ENTITY, "off")
    hass.states.async_set(LOCATION_ENTITY, "not_home")
    await hass.async_block_till_done(wait_background_tasks=True)
    await start_charging(hass)
    assert state(hass, "sensor", "charger_status") == "a"

    await advance(hass, freezer, timedelta(days=2))
    hass.states.async_set(LOCATION_ENTITY, "home")
    hass.states.async_set(PLUGGED_ENTITY, "on")
    await hass.async_block_till_done(wait_background_tasks=True)
    await advance(hass, freezer, TICK_INTERVAL)
    assert mock_rivian.reads == 0
    assert mock_rivian.writes == []
    assert state(hass, "sensor", "control_state") == "idle"


async def test_dry_run_writes_nothing(
    hass: HomeAssistant, mock_rivian: FakeRivian, freezer: FrozenDateTimeFactory
) -> None:
    """With dry run on (the default), requests never reach Rivian."""
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    await set_up(hass, make_entry(dry_run=True))

    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=16)
    await start_charging(hass)
    await advance(hass, freezer, TICK_INTERVAL)

    assert mock_rivian.reads == 0
    assert mock_rivian.writes == []
    assert state(hass, "sensor", "control_state") == "dry_run"
    target = attributes(hass)["dry_run_target"]
    assert target["amperage"] == 16
    assert "location" not in target


async def test_charger_status_follows_eligibility(
    hass: HomeAssistant, mock_rivian: FakeRivian
) -> None:
    """a unless plugged in at home and not known absent from the Wall Connector."""
    hass.states.async_set(PLUGGED_ENTITY, "off")
    hass.states.async_set(LOCATION_ENTITY, "not_home")
    hass.states.async_set(CHARGER_STATE_ENTITY, "charging_ready")
    hass.states.async_set(WALL_CONNECTOR_ENTITY, "on")
    entry = make_entry(wall_connector_entity=WALL_CONNECTOR_ENTITY)
    await set_up(hass, entry)
    runtime = entry.runtime_data

    def status() -> str:
        return state(hass, "sensor", "charger_status")

    assert status() == "a"
    hass.states.async_set(PLUGGED_ENTITY, "on")
    await hass.async_block_till_done()
    assert status() == "a"  # plugged in, but away
    hass.states.async_set(LOCATION_ENTITY, "home")
    await hass.async_block_till_done()
    assert status() == "b"
    assert runtime.observation().at_charger is True
    hass.states.async_set(CHARGER_STATE_ENTITY, "charging_active")
    await hass.async_block_till_done()
    assert status() == "c"
    hass.states.async_set(WALL_CONNECTOR_ENTITY, "off")
    await hass.async_block_till_done()
    assert status() == "a"  # the Wall Connector sees no vehicle
    assert runtime.observation().at_charger is False
    hass.states.async_set(WALL_CONNECTOR_ENTITY, "unavailable")
    await hass.async_block_till_done()
    assert status() == "c"  # an unknown Wall Connector is not evidence either way
    assert runtime.observation().at_charger is None
    hass.states.async_set(WALL_CONNECTOR_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "unavailable")
    await hass.async_block_till_done()
    assert status() == "a"  # unknown location is not home
    hass.states.async_set(LOCATION_ENTITY, "home")
    hass.states.async_set(CHARGER_STATE_ENTITY, "unknown")
    await hass.async_block_till_done()
    assert status() == "b"
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_vehicle_override_entity(hass: HomeAssistant, mock_rivian: FakeRivian) -> None:
    """While the override names another vehicle, the charger status shows the Rivian absent."""
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    hass.states.async_set(OVERRIDE_ENTITY, "auto")
    entry = make_entry(vehicle_override_entity=OVERRIDE_ENTITY)
    await set_up(hass, entry)

    for override, expected in (
        ("auto", "b"),
        ("rivian", "b"),
        ("Rivian", "b"),
        ("tesla", "a"),
        ("unknown", "b"),
        ("unavailable", "b"),
        ("Tesla", "a"),
    ):
        hass.states.async_set(OVERRIDE_ENTITY, override)
        await hass.async_block_till_done()
        assert state(hass, "sensor", "charger_status") == expected, override
    assert attributes(hass)["vehicle_override_active"] is True

    hass.states.async_remove(OVERRIDE_ENTITY)
    await hass.async_block_till_done()
    assert state(hass, "sensor", "charger_status") == "b"
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_liveness_lost_stops_renewal(
    hass: HomeAssistant, mock_rivian: FakeRivian, freezer: FrozenDateTimeFactory
) -> None:
    """While the controller's liveness entity is unavailable, nothing is renewed or started.

    Once the controller has looked down for the grace period, its last request
    is held and the lease is parked. When it is back with nothing saying it
    kept running (no charging entity is mapped here), that request is dropped:
    the controller must ask again.
    """
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    hass.states.async_set(LIVENESS_ENTITY, "pv")
    entry = make_entry(controller_liveness_entity=LIVENESS_ENTITY)
    await set_up(hass, entry)
    assert entry.runtime_data.observation().controller_alive is True

    await start_charging(hass)
    assert len(mock_rivian.writes) == 1

    # With the controller alive the lease is renewed once 8 minutes or less remain.
    await advance(hass, freezer, timedelta(minutes=12, seconds=15))
    assert len(mock_rivian.writes) == 2
    assert state(hass, "sensor", "control_state") == "lease_active"

    hass.states.async_set(LIVENESS_ENTITY, "unavailable")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.observation().controller_alive is False
    # One unavailable reading (a planner reload, say) is not enough.
    await advance(hass, freezer, timedelta(minutes=2))
    assert state(hass, "switch", "solar_charging") == "on"
    assert len(mock_rivian.writes) == 2
    await advance(hass, freezer, timedelta(minutes=1))
    assert len(mock_rivian.writes) == 3
    [parked] = mock_rivian.writes[-1]
    assert parked["duration"] == 20  # a parked window: it ended 5 minutes ago
    assert state(hass, "sensor", "control_state") == "paused"
    assert attributes(hass)["request_held"] is True
    assert state(hass, "switch", "solar_charging") == "on"  # held, not yet dropped

    for _ in range(4):
        await advance(hass, freezer, timedelta(minutes=6))
    assert len(mock_rivian.writes) == 3

    # A missing entity counts as the controller being down too.
    hass.states.async_remove(LIVENESS_ENTITY)
    await advance(hass, freezer, TICK_INTERVAL)
    assert entry.runtime_data.observation().controller_alive is False
    assert len(mock_rivian.writes) == 3

    # The controller is back, but has not asked again: nothing is started on its
    # old request.
    hass.states.async_set(LIVENESS_ENTITY, "2026-10-25T12:00:00+00:00")
    await hass.async_block_till_done(wait_background_tasks=True)
    await advance(hass, freezer, timedelta(seconds=30))
    assert len(mock_rivian.writes) == 3
    assert state(hass, "sensor", "control_state") == "paused"
    assert state(hass, "switch", "solar_charging") == "off"
    assert read_journal(hass)["desired_charge"] is None

    await start_charging(hass)
    assert len(mock_rivian.writes) == 4
    assert state(hass, "sensor", "control_state") == "lease_active"


async def test_held_request_resumes_while_the_controller_wants_charging(
    hass: HomeAssistant, mock_rivian: FakeRivian, freezer: FrozenDateTimeFactory
) -> None:
    """The liveness entity failed while the controller ran on: its charging state resumes it."""
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    hass.states.async_set(LIVENESS_ENTITY, "2026-10-25T12:00:00+00:00")
    hass.states.async_set(CHARGING_ENTITY, "on")
    entry = make_entry(
        controller_liveness_entity=LIVENESS_ENTITY, controller_charging_entity=CHARGING_ENTITY
    )
    await set_up(hass, entry)
    assert entry.runtime_data.observation().controller_charging is True
    await start_charging(hass)
    assert len(mock_rivian.writes) == 1

    for entity_id in (LIVENESS_ENTITY, CHARGING_ENTITY):
        hass.states.async_set(entity_id, "unavailable")
    await hass.async_block_till_done(wait_background_tasks=True)
    await advance(hass, freezer, timedelta(minutes=2))
    await advance(hass, freezer, timedelta(minutes=2))
    assert len(mock_rivian.writes) == 2  # held and parked
    assert attributes(hass)["request_held"] is True

    # The entities are back; the controller never restarted, so it still wants
    # charging and will not ask again.
    hass.states.async_set(LIVENESS_ENTITY, "2026-10-25T12:05:00+00:00")
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done(wait_background_tasks=True)
    await advance(hass, freezer, TICK_INTERVAL)
    assert len(mock_rivian.writes) == 3
    assert state(hass, "sensor", "control_state") == "lease_active"
    assert attributes(hass)["request_unconfirmed"] is False


async def test_tick_sees_entities_written_together(
    hass: HomeAssistant, mock_rivian: FakeRivian
) -> None:
    """Entities the controller updates together are read by the same tick."""
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    entry = make_entry(
        controller_liveness_entity=LIVENESS_ENTITY, controller_charging_entity=CHARGING_ENTITY
    )
    await set_up(hass, entry)
    controller = entry.runtime_data.controller
    seen: list[Observation] = []
    tick = controller.async_tick

    async def record(now, obs, **kwargs):
        seen.append(obs)
        await tick(now, obs, **kwargs)

    controller.async_tick = record
    hass.states.async_set(LIVENESS_ENTITY, "2026-10-25T12:00:00+00:00")
    hass.states.async_set(CHARGING_ENTITY, "on")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert seen and seen[0].controller_alive is True and seen[0].controller_charging is True


async def test_unplug_restores_owner_schedule(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Unplugging for the release delay restores the owner's schedules exactly."""
    await start_charging(hass)
    hass.states.async_set(PLUGGED_ENTITY, "off")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_rivian.schedules != OWNER_SCHEDULES
    await advance(hass, freezer, timedelta(seconds=75))
    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert state(hass, "sensor", "control_state") == "idle"
    assert state(hass, "sensor", "charger_status") == "a"


async def test_release_button_hands_back(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Release restores the owner's schedules and ignores the controller until unplug."""
    await start_charging(hass)
    assert state(hass, "sensor", "control_state") == "lease_active"

    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "release")

    assert mock_rivian.writes[-1] == OWNER_SCHEDULES
    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert state(hass, "sensor", "control_state") == "handed_back"
    assert state(hass, "switch", "solar_charging") == "off"
    assert read_journal(hass)["phase"] == "idle"

    # The controller's requests are ignored until the vehicle is unplugged.
    writes = len(mock_rivian.writes)
    await start_charging(hass)
    await advance(hass, freezer, TICK_INTERVAL)
    assert len(mock_rivian.writes) == writes
    assert state(hass, "sensor", "control_state") == "handed_back"

    hass.states.async_set(PLUGGED_ENTITY, "off")
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.states.async_set(PLUGGED_ENTITY, "on")
    await hass.async_block_till_done(wait_background_tasks=True)
    await advance(hass, freezer, timedelta(seconds=31))
    await start_charging(hass)
    assert len(mock_rivian.writes) == writes + 1
    assert state(hass, "sensor", "control_state") == "lease_active"


async def test_fault_notification_and_clear(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """A fault notifies the owner; the notification goes once the fault clears."""
    fault_id = f"{DOMAIN}_{loaded_entry.entry_id}_fault"
    release_id = f"{DOMAIN}_{loaded_entry.entry_id}_release_pending"
    lease = await fault_by_owner_edit(hass, mock_rivian)
    assert attributes(hass)["fault_code"] == "external_edit"
    assert mock_rivian.writes[-1] == lease  # nothing was overwritten

    notification = notifications(hass)[fault_id]
    assert "external_edit" in notification["title"]
    message = notification["message"]
    assert "changed outside Rivian schedule control" in message
    for remedy in ("Clear fault", "Restore owner schedule", "force restore", "abandon ownership"):
        assert remedy in message, remedy
    assert not any(fragment in message for fragment in COORDINATE_FRAGMENTS)

    with pytest.raises(HomeAssistantError) as err:
        await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "clear_fault")
    assert err.value.translation_key == "clear_fault_refused"
    assert state(hass, "sensor", "control_state") == "fault"

    # A release during the fault is recorded and explained.
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "release")
    assert "once the fault is cleared" in notifications(hass)[release_id]["message"]
    assert mock_rivian.writes[-1] == lease

    # Once Rivian holds schedule control's lease again, the fault clears and the
    # recorded release goes ahead.
    mock_rivian.schedules = lease
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "clear_fault")
    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert state(hass, "sensor", "control_state") == "handed_back"
    assert fault_id not in notifications(hass)
    assert release_id not in notifications(hass)


async def test_restore_rejected_fault_explains_recovery(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """Rivian refusing the owner's list back faults with a remedy that works."""
    await start_charging(hass)
    lease = mock_rivian.schedules
    accept = mock_rivian.set_charging_schedules

    async def refuse_owner_list(vehicle_id: str, schedules: list) -> FakeResponse:
        if len(schedules) > 1:  # the owner's list, not schedule control's single window
            raise RivianBadRequestError("invalid input")
        return await accept(vehicle_id, schedules)

    mock_rivian.set_charging_schedules = refuse_owner_list
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "release")
    assert attributes(hass)["fault_code"] == "restore_rejected"
    assert mock_rivian.schedules == lease
    writes = len(mock_rivian.writes)

    message = notifications(hass)[f"{DOMAIN}_{loaded_entry.entry_id}_fault"]["message"]
    assert "upstream PR #308" in message
    assert "First: " in message
    assert "Recreate the owner's schedules in the Rivian app" in message
    assert "do not use force restore" in message
    assert "abandon ownership" in message
    assert not any(fragment in message for fragment in COORDINATE_FRAGMENTS)

    # The owner recreates the schedules in the Rivian app; clearing the fault then
    # lets go of the vehicle without writing.
    mock_rivian.schedules = deepcopy(OWNER_SCHEDULES)
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "clear_fault")
    assert state(hass, "sensor", "control_state") == "handed_back"
    assert read_journal(hass)["phase"] == "idle"
    assert len(mock_rivian.writes) == writes
    assert f"{DOMAIN}_{loaded_entry.entry_id}_fault" not in notifications(hass)


async def test_lease_renewal_switch(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With renewal off, an active lease is left to lapse; the setting is persisted.

    The window ends on the vehicle with no write (the cloud-free stop). Once it
    has ended, it is parked so it cannot recur a week later.
    """
    await start_charging(hass)
    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, "switch", "lease_renewal")
    assert state(hass, "switch", "lease_renewal") == "off"
    assert loaded_entry.runtime_data.controller.renewal_enabled is False
    assert read_journal(hass)["renewal_enabled"] is False
    assert attributes(hass)["start_blocked_by"] == ["renewal_off"]

    # Not renewed: still the first lease shortly before it ends.
    await advance(hass, freezer, timedelta(minutes=19))
    assert len(mock_rivian.writes) == 1
    assert state(hass, "sensor", "control_state") == "lease_active"

    # The lease has ended; its window still runs 5 minutes (the clock skew) on the car.
    await advance(hass, freezer, timedelta(minutes=2))
    assert len(mock_rivian.writes) == 1
    assert state(hass, "sensor", "control_state") == "lease_lapsed"

    await advance(hass, freezer, timedelta(minutes=5))
    assert len(mock_rivian.writes) == 2
    [parked] = mock_rivian.writes[-1]
    assert parked["duration"] == 20  # a parked window, not a new lease
    assert state(hass, "sensor", "control_state") == "paused"
    assert state(hass, "switch", "solar_charging") == "on"  # still requested

    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, "switch", "lease_renewal")
    assert loaded_entry.runtime_data.controller.renewal_enabled is True
    await advance(hass, freezer, TICK_INTERVAL)  # 5 s after the park
    assert len(mock_rivian.writes) == 3
    assert state(hass, "sensor", "control_state") == "lease_active"
    assert attributes(hass)["start_blocked_by"] == []

    # Persisted across a reload.
    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, "switch", "lease_renewal")
    assert await hass.config_entries.async_reload(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, "switch", "lease_renewal") == "off"


async def test_start_blocked_by_attribute(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """The control state names what keeps a requested start from happening."""
    hass.states.async_set(PLUGGED_ENTITY, "unavailable")
    await hass.async_block_till_done(wait_background_tasks=True)
    await start_charging(hass)
    assert attributes(hass)["start_blocked_by"] == ["vehicle_state_unknown"]
    assert state(hass, "sensor", "control_state") == "waiting_for_vehicle"
    assert mock_rivian.reads == 0

    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "not_home")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert attributes(hass)["start_blocked_by"] == ["vehicle_not_at_charger"]

    hass.states.async_set(LOCATION_ENTITY, "home")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, "sensor", "control_state") == "lease_active"
    attrs = attributes(hass)
    assert attrs["start_blocked_by"] == []
    # The attributes still carry no coordinates now that schedule control owns the schedule.
    text = str(attrs)
    for fragment in COORDINATE_FRAGMENTS:
        assert fragment not in text, fragment
    assert str(hass.config.latitude)[:5] not in text
    assert str(hass.config.longitude)[:6] not in text


async def test_read_schedules_button_notifies_without_coordinates(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """The read button shows each schedule, never its location."""
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "read_schedules")

    notification = notifications(hass)[f"{DOMAIN}_{loaded_entry.entry_id}_schedules"]
    message = notification["message"]
    assert "2 charging schedules" in message
    assert "not written by Rivian schedule control" in message
    assert "Monday, Tuesday, Wednesday, Thursday, Friday from 00:00 for 6 h 0 min at 48 A" in (
        message
    )
    assert "Saturday from 10:00 for 1 h 30 min at 32 A, disabled" in message
    assert not any(fragment in message for fragment in COORDINATE_FRAGMENTS)
    assert attributes(hass)["last_provider_read"]["count"] == 2


# Missing entities and unexpected errors ---------------------------------


async def test_missing_mapped_entity_raises_repair_issue(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A plug or location entity missing for 5 minutes is reported until it is back."""
    issues = ir.async_get(hass)
    issue_id = f"missing_entity_{loaded_entry.entry_id}_plugged_entity"
    hass.states.async_remove(PLUGGED_ENTITY)
    await hass.async_block_till_done(wait_background_tasks=True)
    await advance(hass, freezer, timedelta(minutes=4))
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    await advance(hass, freezer, timedelta(minutes=1, seconds=15))
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "missing_plugged_entity"
    assert issue.translation_placeholders["entity_id"] == PLUGGED_ENTITY
    assert (
        issues.async_get_issue(DOMAIN, f"missing_entity_{loaded_entry.entry_id}_location_entity")
        is None
    )

    hass.states.async_set(PLUGGED_ENTITY, "on")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert issues.async_get_issue(DOMAIN, issue_id) is None


async def test_missing_liveness_entity_raises_repair_issue(
    hass: HomeAssistant, mock_rivian: FakeRivian, freezer: FrozenDateTimeFactory
) -> None:
    """A mapped liveness entity that does not exist blocks charging; it is reported."""
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    entry = make_entry(controller_liveness_entity=LIVENESS_ENTITY)
    await set_up(hass, entry)
    issues = ir.async_get(hass)
    issue_id = f"missing_entity_{entry.entry_id}_controller_liveness_entity"
    assert entry.runtime_data.observation().controller_alive is False

    # Missing means the controller is down: no lease is started.
    await start_charging(hass)
    assert not any(window["duration"] == 30 for write in mock_rivian.writes for window in write)
    assert state(hass, "sensor", "control_state") != "lease_active"

    await advance(hass, freezer, timedelta(minutes=4))
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    await advance(hass, freezer, timedelta(minutes=1, seconds=15))
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "missing_controller_liveness_entity"
    assert issue.translation_placeholders == {"entity_id": LIVENESS_ENTITY, "title": VEHICLE_NAME}

    hass.states.async_set(LIVENESS_ENTITY, "pv")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    assert entry.runtime_data.observation().controller_alive is True


async def test_unexpected_tick_error_logged_once(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected error is logged in full once, then quietly until a tick works."""
    good = mock_rivian.get_charging_schedules

    async def broken(vehicle_id: str) -> Any:
        raise RuntimeError("boom")

    def errors() -> list[logging.LogRecord]:
        return [
            record
            for record in caplog.records
            if record.levelno >= logging.ERROR and "Unexpected error" in record.getMessage()
        ]

    runtime = loaded_entry.runtime_data
    mock_rivian.get_charging_schedules = broken
    caplog.set_level(logging.DEBUG, logger="custom_components.solar_ev_planner.rivian")
    await start_charging(hass)
    for _ in range(3):
        await advance(hass, freezer, TICK_INTERVAL)
    assert len(errors()) == 1
    assert errors()[0].exc_info is not None
    assert attributes(hass)["unexpected_error"] == "RuntimeError"
    assert mock_rivian.writes == []

    mock_rivian.get_charging_schedules = good
    await advance(hass, freezer, TICK_INTERVAL)
    assert len(mock_rivian.writes) == 1
    assert runtime.unexpected_error is None

    mock_rivian.get_charging_schedules = broken
    await advance(hass, freezer, timedelta(minutes=13))
    assert len(errors()) == 2


# Authentication ----------------------------------------------------------


def reauth_flows(hass: HomeAssistant) -> list[dict[str, Any]]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"].get("source") == SOURCE_REAUTH
    ]


async def test_auth_error_starts_reauth(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A rejected session asks for a login, again after an abandoned flow, every 5 min."""
    mock_rivian.read_error = RivianUnauthenticated(401, {})
    await start_charging(hass)

    [flow] = reauth_flows(hass)
    assert flow["context"]["entry_id"] == loaded_entry.entry_id
    assert attributes(hass)["reauth_required"] is True
    assert loaded_entry.state is ConfigEntryState.LOADED

    reads = mock_rivian.reads
    for _ in range(3):
        await advance(hass, freezer, TICK_INTERVAL)
    assert mock_rivian.reads == reads  # retried only after the backoff

    # The owner closes the dialog; the next rejected request asks again.
    hass.config_entries.flow.async_abort(flow["flow_id"])
    assert reauth_flows(hass) == []
    await advance(hass, freezer, timedelta(minutes=6))
    assert mock_rivian.reads > reads
    assert len(reauth_flows(hass)) == 1

    # A tick that works again clears the flag.
    mock_rivian.read_error = None
    await advance(hass, freezer, timedelta(minutes=6))
    assert attributes(hass)["reauth_required"] is False
    assert state(hass, "sensor", "control_state") == "lease_active"


# Shutdown ---------------------------------------------------------------


async def test_no_ticks_while_stopping(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """Requests are recorded while Home Assistant stops, but nothing is sent."""
    hass.set_state(CoreState.stopping)
    try:
        await start_charging(hass)
        assert read_journal(hass)["desired_charge"] is True
        assert mock_rivian.reads == 0
    finally:
        hass.set_state(CoreState.running)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    loaded_entry.runtime_data.request_tick()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_rivian.reads == 0


# Services ---------------------------------------------------------------


async def test_read_schedules_service(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """The read service returns the provider's schedules without locations."""
    response = await call_schedule_control(hass, "read_schedules", loaded_entry)
    assert response["count"] == 2
    assert len(response["fingerprint"]) == 12
    assert response["written_by_schedule_control"] is False
    assert response["schedules"][1] == {
        "weekDays": ["Saturday"],
        "startTime": 600,
        "duration": 90,
        "amperage": 32,
        "enabled": False,
    }
    assert not any(fragment in str(response) for fragment in COORDINATE_FRAGMENTS)

    await start_charging(hass)
    response = await call_schedule_control(hass, "read_schedules", loaded_entry)
    assert response["count"] == 1
    assert response["written_by_schedule_control"] is True


async def test_release_service(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """release restores and hands back; in idle it cancels a pending command."""
    await start_charging(hass)
    await call_schedule_control(hass, "release", loaded_entry)
    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert state(hass, "sensor", "control_state") == "handed_back"

    # Released again while idle: nothing to restore, no write.
    writes = len(mock_rivian.writes)
    await call_schedule_control(hass, "release", loaded_entry)
    assert len(mock_rivian.writes) == writes

    # clear_fault without a fault is a no-op that succeeds.
    await call_schedule_control(hass, "clear_fault", loaded_entry)


async def test_abandon_ownership(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """abandon is refused unless faulted; then it archives and lists the owner's list."""
    await start_charging(hass)
    with pytest.raises(ServiceValidationError) as err:
        await call_schedule_control(hass, "abandon_ownership", loaded_entry)
    assert err.value.translation_key == "abandon_not_faulted"
    assert state(hass, "sensor", "control_state") == "lease_active"
    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, "switch", "solar_charging")

    hass.states.async_set(PLUGGED_ENTITY, "on")
    lease = mock_rivian.schedules
    mock_rivian.schedules = [OWNER_SCHEDULES[1]]
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "release")
    assert state(hass, "sensor", "control_state") == "fault"
    writes = len(mock_rivian.writes)

    await call_schedule_control(hass, "abandon_ownership", loaded_entry)
    assert len(mock_rivian.writes) == writes
    assert mock_rivian.schedules == [OWNER_SCHEDULES[1]]
    assert lease != mock_rivian.schedules
    journal = loaded_entry.runtime_data.controller.journal
    assert journal.original is None
    assert journal.archived_original == OWNER_SCHEDULES
    assert attributes(hass)["archived_owner_schedule_count"] == 2
    assert state(hass, "sensor", "control_state") != "fault"

    message = notifications(hass)[f"{DOMAIN}_{loaded_entry.entry_id}_abandoned"]["message"]
    assert "archived" in message
    assert "1. Monday, Tuesday, Wednesday, Thursday, Friday from 00:00 for 6 h 0 min" in message
    assert "2. Saturday from 10:00 for 1 h 30 min at 32 A, disabled" in message
    assert not any(fragment in message for fragment in COORDINATE_FRAGMENTS)
    assert f"{DOMAIN}_{loaded_entry.entry_id}_fault" not in notifications(hass)


async def test_force_restore(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """force_restore only runs in a fault, and writes the owner's list over anything."""
    await start_charging(hass)
    with pytest.raises(ServiceValidationError) as err:
        await call_schedule_control(hass, "force_restore", loaded_entry)
    assert err.value.translation_key == "force_restore_not_faulted"
    assert mock_rivian.schedules != OWNER_SCHEDULES
    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, "switch", "solar_charging")

    mock_rivian.schedules = [OWNER_SCHEDULES[1]]
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "release")
    assert state(hass, "sensor", "control_state") == "fault"

    await call_schedule_control(hass, "force_restore", loaded_entry)
    assert mock_rivian.writes[-1] == OWNER_SCHEDULES
    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert state(hass, "sensor", "control_state") == "handed_back"
    assert f"{DOMAIN}_{loaded_entry.entry_id}_fault" not in notifications(hass)

    # A readback that does not show the owner's list keeps the fault.
    hass.states.async_set(PLUGGED_ENTITY, "off")
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.states.async_set(PLUGGED_ENTITY, "on")
    await hass.async_block_till_done(wait_background_tasks=True)
    await start_charging(hass)
    mock_rivian.schedules = [OWNER_SCHEDULES[1]]
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "release")
    assert state(hass, "sensor", "control_state") == "fault"
    mock_rivian.write_error = RivianApiException("rejected")
    with pytest.raises(HomeAssistantError) as err:
        await call_schedule_control(hass, "force_restore", loaded_entry)
    assert err.value.translation_key == "force_restore_unverified"
    assert attributes(hass)["fault_code"] == "force_restore_unverified"


async def test_force_restore_uses_archive_only_over_own_window(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """An archived owner list never replaces schedules the owner has set up since."""
    lease = await fault_by_owner_edit(hass, mock_rivian)
    await call_schedule_control(hass, "abandon_ownership", loaded_entry)
    assert read_journal(hass)["archived_original"] == OWNER_SCHEDULES

    # Later, Rivian holds schedule control's old window again, so the next takeover faults.
    mock_rivian.schedules = lease
    hass.states.async_set(PLUGGED_ENTITY, "off")
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.states.async_set(PLUGGED_ENTITY, "on")
    await hass.async_block_till_done(wait_background_tasks=True)
    await start_charging(hass)
    assert attributes(hass)["fault_code"] == "own_window_present"
    assert read_journal(hass)["original"] is None

    # The owner sets up new schedules in the Rivian app first: the archive is stale.
    mock_rivian.schedules = [OWNER_SCHEDULES[1]]
    writes = len(mock_rivian.writes)
    with pytest.raises(HomeAssistantError) as err:
        await call_schedule_control(hass, "force_restore", loaded_entry)
    assert err.value.translation_key == "force_restore_archive_unsafe"
    assert len(mock_rivian.writes) == writes
    assert mock_rivian.schedules == [OWNER_SCHEDULES[1]]
    assert attributes(hass)["fault_code"] == "own_window_present"

    # While Rivian holds schedule control's window, the archived list is written.
    mock_rivian.schedules = lease
    await call_schedule_control(hass, "force_restore", loaded_entry)
    assert mock_rivian.writes[-1] == OWNER_SCHEDULES
    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert state(hass, "sensor", "control_state") == "handed_back"


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
    for service in ("read_schedules", "force_restore"):
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                DOMAIN,
                service,
                {"config_entry_id": loaded_entry.entry_id},
                blocking=True,
                return_response=service == "read_schedules",
            )
        assert err.value.translation_key == "entry_not_loaded"


# Diagnostics ------------------------------------------------------------


async def test_diagnostics_redacted(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """Diagnostics carry the journal but no tokens, email, vehicle ID or coordinates."""
    await start_charging(hass)
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "read_schedules")

    def check(diagnostics: dict[str, Any]) -> None:
        text = str(diagnostics)
        for secret in (*TOKENS.values(), EMAIL, VEHICLE_ID, *COORDINATE_FRAGMENTS):
            assert secret not in text, secret
        assert str(hass.config.latitude)[:5] not in text
        assert str(hass.config.longitude)[:6] not in text

    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_entry)
    check(diagnostics)
    journal = diagnostics["journal"]
    assert journal["phase"] == "owned"
    assert len(journal["original"]) == 2
    assert journal["original"][0]["location"] == "**REDACTED**"
    assert journal["original_fp"]["locations"] == "**REDACTED**"
    assert journal["owned_fp"]["locations"] == "**REDACTED**"
    assert journal["written_states"][0]["locations"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["access_token"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["vehicle_id"] == "**REDACTED**"
    assert diagnostics["control_state"] == "lease_active"
    assert diagnostics["charger_status"] == "b"
    assert diagnostics["controller"]["lease_amps"] == 8
    assert diagnostics["controller"]["start_blocked_by"] == []
    check(attributes(hass))

    # The archived owner list of an abandoned journal is redacted too.
    await call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, "switch", "solar_charging")
    mock_rivian.schedules = [OWNER_SCHEDULES[1]]
    await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "release")
    await call_schedule_control(hass, "abandon_ownership", loaded_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_entry)
    check(diagnostics)
    assert len(diagnostics["journal"]["archived_original"]) == 2
    assert diagnostics["journal"]["archived_original"][1]["location"] == "**REDACTED**"


# Unload, disable and removal ---------------------------------------------


async def test_unload_keeps_ownership_and_stops_ticking(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Unloading neither releases nor leaves timers behind; reloading resumes."""
    await start_charging(hass)
    writes = len(mock_rivian.writes)

    def tick_timers() -> list:
        return [
            handle
            for handle in get_scheduled_timer_handles(hass.loop)
            if not handle.cancelled()
            and handle._args
            and isinstance(job := handle._args[-1], HassJob)
            and str(job.name).startswith(f"{DOMAIN} tick")
        ]

    assert tick_timers()
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.state is ConfigEntryState.NOT_LOADED
    assert read_journal(hass)["phase"] == "owned"
    assert not tick_timers()

    reads = mock_rivian.reads
    await advance(hass, freezer, timedelta(minutes=30))
    assert mock_rivian.reads == reads
    assert len(mock_rivian.writes) == writes

    assert await hass.config_entries.async_setup(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.runtime_data.controller.journal.original == OWNER_SCHEDULES
    # The lease lapsed while unloaded and nothing says the controller still wants
    # charging (a restarted controller asks again), so its old request is dropped
    # and the window parked.
    await advance(hass, freezer, timedelta(seconds=30))
    assert state(hass, "switch", "solar_charging") == "off"
    assert mock_rivian.writes[-1][0]["amperage"] == 8


async def test_disable_restores_owner_schedule(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """Disabling the entry while it owns the schedule gives the owner's list back."""
    await start_charging(hass)
    assert mock_rivian.schedules != OWNER_SCHEDULES

    assert await hass.config_entries.async_set_disabled_by(
        loaded_entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert loaded_entry.state is ConfigEntryState.NOT_LOADED
    assert mock_rivian.schedules == OWNER_SCHEDULES
    journal = read_journal(hass)
    assert journal["phase"] == "idle"
    assert journal["original"] is None
    assert journal["handed_back"] is False
    assert restore_failed_notification_id(VEHICLE_ID) not in notifications(hass)

    # Enabled again, schedule control starts idle and takes over on the next request.
    assert await hass.config_entries.async_set_disabled_by(loaded_entry.entry_id, None)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.state is ConfigEntryState.LOADED
    assert state(hass, "sensor", "control_state") == "idle"


@pytest.mark.usefixtures("fast_restore")
async def test_disable_while_faulted_notifies(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """A disable that cannot restore tells the owner and keeps the journal."""
    await fault_by_owner_edit(hass, mock_rivian)
    writes = len(mock_rivian.writes)

    assert await hass.config_entries.async_set_disabled_by(
        loaded_entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(mock_rivian.writes) == writes
    notification = notifications(hass)[restore_failed_notification_id(VEHICLE_ID)]
    assert "disabled" in notification["message"]
    assert "external_edit" in notification["message"]
    assert read_journal(hass)["original"] == OWNER_SCHEDULES
    # A repair issue survives a restart, unlike the notification.
    issue = ir.async_get(hass).async_get_issue(DOMAIN, restore_failed_issue_id(VEHICLE_ID))
    assert issue is not None and issue.is_persistent
    assert issue.translation_key == "restore_failed_disabled"
    assert "external_edit" in issue.translation_placeholders["reason"]

    # Enabled again, schedule control takes the journal back over and the issue goes.
    assert await hass.config_entries.async_set_disabled_by(loaded_entry.entry_id, None)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, restore_failed_issue_id(VEHICLE_ID)) is None


async def test_disable_during_error_backoff_restores(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A disable right after a failed request still restores: it ignores the backoff."""
    await start_charging(hass)
    mock_rivian.read_error = RivianApiException("unreachable")
    await advance(hass, freezer, timedelta(minutes=12, seconds=15))  # the renewal's read fails
    assert attributes(hass)["backoff_until"] is not None
    assert attributes(hass)["last_error"] == "Rivian request failed (RivianApiException)"

    # Rivian is reachable again, and the owner disables within the backoff.
    mock_rivian.read_error = None
    assert await hass.config_entries.async_set_disabled_by(
        loaded_entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_rivian.writes[-1] == OWNER_SCHEDULES
    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert read_journal(hass)["phase"] == "idle"
    assert restore_failed_notification_id(VEHICLE_ID) not in notifications(hass)


@pytest.mark.usefixtures("fast_restore")
async def test_restore_failed_reason_is_from_this_attempt(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A restore notification never blames an error from before the restore."""
    await start_charging(hass)
    lease = mock_rivian.schedules
    mock_rivian.read_error = RivianApiException("unreachable")
    await advance(hass, freezer, timedelta(minutes=12, seconds=15))
    assert attributes(hass)["last_error"] == "Rivian request failed (RivianApiException)"
    mock_rivian.read_error = None

    # Rivian now accepts writes without applying them, and reports no error.
    async def accept_without_applying(vehicle_id: str, schedules: list) -> FakeResponse:
        mock_rivian.writes.append(schedules)
        return FakeResponse({"data": {"setChargingSchedules": {"success": True}}})

    mock_rivian.set_charging_schedules = accept_without_applying
    assert await hass.config_entries.async_set_disabled_by(
        loaded_entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_rivian.writes[-1] == OWNER_SCHEDULES  # the restore was sent
    assert mock_rivian.schedules == lease
    message = notifications(hass)[restore_failed_notification_id(VEHICLE_ID)]["message"]
    assert "RivianApiException" not in message
    assert "the restore was not confirmed; schedule control was still in phase writing" in message


async def test_remove_restores_owner_schedule(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """Removing the integration while it owns the schedule restores it."""
    await start_charging(hass)
    assert mock_rivian.schedules != OWNER_SCHEDULES

    await hass.config_entries.async_remove(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert not journal_path(hass).exists()
    assert restore_failed_notification_id(VEHICLE_ID) not in notifications(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, restore_failed_issue_id(VEHICLE_ID)) is None


@pytest.mark.usefixtures("fast_restore")
async def test_remove_keeps_journal_and_readd_inherits_it(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed restore keeps the journal; the same vehicle added again finishes it."""
    await start_charging(hass)
    lease = mock_rivian.schedules
    mock_rivian.write_error = RivianApiException("down")
    reads = mock_rivian.reads

    await hass.config_entries.async_remove(loaded_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_rivian.schedules == lease
    # The guard read and readback of the failed restore, then one more read once
    # settled: that tick ignores the backoff the failed write started.
    assert mock_rivian.reads - reads == 3
    assert read_journal(hass)["original"] == OWNER_SCHEDULES
    notification_id = restore_failed_notification_id(VEHICLE_ID)
    notification = notifications(hass)[notification_id]
    assert "manual" in notification["title"]
    assert "removed" in notification["message"]
    # The reason is the error of this restore attempt.
    assert "Rivian request failed (RivianApiException)" in notification["message"]
    assert not any(fragment in notification["message"] for fragment in COORDINATE_FRAGMENTS)
    # The entry is gone, but a persistent issue keeps saying so after a restart.
    issues = ir.async_get(hass)
    issue = issues.async_get_issue(DOMAIN, restore_failed_issue_id(VEHICLE_ID))
    assert issue is not None and issue.is_persistent
    assert issue.translation_key == "restore_failed_removed"
    assert issues.async_get_issue(DOMAIN, setup_failed_issue_id(loaded_entry.entry_id)) is None

    # Added again, the entry uses the vehicle's journal, never the lease as "original".
    # The first tick is held at its read so the journal can be checked before it acts.
    mock_rivian.write_error = None
    mock_rivian.read_gate = asyncio.Event()
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.runtime_data.controller.journal.original == OWNER_SCHEDULES
    assert entry.runtime_data.controller.journal.release_requested == "integration removed"
    assert notification_id not in notifications(hass)
    assert issues.async_get_issue(DOMAIN, restore_failed_issue_id(VEHICLE_ID)) is None
    mock_rivian.read_gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    # The interrupted restore is resolved by reading Rivian, then sent again.
    await advance(hass, freezer, timedelta(seconds=31))
    assert mock_rivian.writes[-1] == OWNER_SCHEDULES
    assert mock_rivian.schedules == OWNER_SCHEDULES
    assert read_journal(hass)["phase"] == "idle"
    assert state(hass, "sensor", "control_state") == "idle"


async def test_remove_without_ownership_deletes_journal(
    hass: HomeAssistant, mock_rivian: FakeRivian
) -> None:
    """Without ownership, removal deletes the journal and talks to nobody."""
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    entry = make_entry(dry_run=True)
    await set_up(hass, entry)
    await call(hass, NUMBER_DOMAIN, SERVICE_SET_VALUE, "number", "solar_current", value=20)
    assert journal_path(hass).exists()

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not journal_path(hass).exists()
    assert mock_rivian.reads == 0


# Window location, overlapping schedules and request counts --------------------------

# The vehicle's GPS fix, about 20 m from Home Assistant's test home.
VEHICLE_FIX = {"latitude": 32.87354, "longitude": -117.22743}


async def test_window_is_written_at_the_vehicles_position_reported_only_as_a_distance(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """The tracker's coordinates locate the window; reports carry only the distance."""
    hass.states.async_set(LOCATION_ENTITY, "home", VEHICLE_FIX | {"gps_accuracy": 5})
    await hass.async_block_till_done(wait_background_tasks=True)
    await start_charging(hass)
    assert mock_rivian.writes[-1][0]["location"] == VEHICLE_FIX

    control = attributes(hass)
    assert control["window_location_source"] == "vehicle"
    assert control["window_location_distance_m"] == 0
    assert control["reads_last_hour"] == 3
    assert control["writes_last_hour"] == 1
    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_entry)
    assert diagnostics["controller"]["window_location_distance_m"] == 0
    assert diagnostics["controller"]["writes_last_hour"] == 1
    assert diagnostics["journal"]["window_location"] == "**REDACTED**"
    for text in (str(control), str(diagnostics)):
        assert "32.873" not in text and "117.227" not in text


@pytest.mark.parametrize(
    "attrs",
    [
        {},
        {"latitude": "32.9", "longitude": -117.2},
        {"latitude": 32.9},
        {"latitude": 95.0, "longitude": 1.0},
    ],
)
async def test_tracker_without_a_valid_position_gives_none(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, attrs: dict[str, Any]
) -> None:
    hass.states.async_set(LOCATION_ENTITY, "home", attrs)
    assert loaded_entry.runtime_data.observation().location is None
    hass.states.async_set(LOCATION_ENTITY, "unavailable", VEHICLE_FIX)
    assert loaded_entry.runtime_data.observation().location is None


async def test_tracker_position_is_read(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    hass.states.async_set(LOCATION_ENTITY, "not_home", {"latitude": 33, "longitude": -117.5})
    assert loaded_entry.runtime_data.observation().location == {
        "latitude": 33.0,
        "longitude": -117.5,
    }


OVERLAPPING_SCHEDULES = [
    OWNER_SCHEDULES[0],
    OWNER_SCHEDULES[1] | {"weekDays": ["Friday", "Saturday"]},
]


@pytest.mark.parametrize(
    ("fallback", "translation_key"),
    [
        (False, "overlapping_owner_schedules"),
        (True, "overlapping_owner_schedules_fallback"),
    ],
)
async def test_overlapping_owner_schedules_raise_a_repair_issue_before_any_write(
    hass: HomeAssistant,
    mock_rivian: FakeRivian,
    freezer: FrozenDateTimeFactory,
    fallback: bool,
    translation_key: str,
) -> None:
    """Shared weekdays are reported before the first write; the issue ends on unload."""
    mock_rivian.schedules = deepcopy(OVERLAPPING_SCHEDULES)
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    entry = make_entry(restore_enabled_only=fallback)
    await set_up(hass, entry)
    await start_charging(hass)

    issue_id = f"overlapping_owner_schedules_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == translation_key
    assert issue.translation_placeholders == {"title": VEHICLE_NAME, "days": "Friday"}
    assert mock_rivian.writes == []
    assert attributes(hass)["owner_schedules_overlap_days"] == ["Friday"]

    await advance(hass, freezer, TICK_INTERVAL)
    assert len(mock_rivian.writes) == 1
    assert state(hass, "sensor", "control_state") == "lease_active"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_rejected_restore_writes_enabled_schedules_when_opted_in(
    hass: HomeAssistant, mock_rivian: FakeRivian, freezer: FrozenDateTimeFactory
) -> None:
    """With the option on, a refused full list is restored as its enabled entries."""
    mock_rivian.schedules = deepcopy(OVERLAPPING_SCHEDULES)
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    entry = make_entry(restore_enabled_only=True)
    await set_up(hass, entry)
    await start_charging(hass)
    await advance(hass, freezer, TICK_INTERVAL)

    write = mock_rivian.set_charging_schedules

    async def refuse_full_list(vehicle_id: str, schedules: list[dict[str, Any]]) -> Any:
        if schedules == OVERLAPPING_SCHEDULES:
            raise RivianBadRequestError(400, {"errors": [{"message": "INVALID_INPUT"}]})
        return await write(vehicle_id, schedules)

    with patch.object(mock_rivian, "set_charging_schedules", refuse_full_list):
        await call(hass, BUTTON_DOMAIN, SERVICE_PRESS, "button", "release")
        assert state(hass, "sensor", "control_state") != "fault"
        await advance(hass, freezer, timedelta(seconds=31))
    assert mock_rivian.schedules == [OWNER_SCHEDULES[0]]
    assert state(hass, "sensor", "control_state") == "handed_back"
    assert read_journal(hass)["archived_original"] == OVERLAPPING_SCHEDULES
