"""Tests for the runtime: observations, tick triggers and error handling."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from conftest import (
    CHARGER_STATE_ENTITY,
    LIVENESS_ENTITY,
    LOCATION_ENTITY,
    PLUGGED_ENTITY,
    WALL_CONNECTOR_ENTITY,
    FakeRivian,
    make_entry,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_ev_planner.rivian.const import MONITORED_ENTITY_OPTIONS
from custom_components.solar_ev_planner.rivian.controller import Fault, Observation
from custom_components.solar_ev_planner.rivian.runtime import _fault_message, describe_schedules

COMPONENT = Path(__file__).parent.parent / "custom_components" / "solar_ev_planner"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load this repository's integration."""


@pytest.mark.parametrize(
    ("plugged", "location", "charger", "expected"),
    [
        ("on", "home", "charging_active", Observation(True, True, True)),
        ("on", "home", "smart_charging_active", Observation(True, True, True)),
        ("on", "home", "charging_connecting", Observation(True, True, True)),
        ("on", "home", "charging_complete", Observation(True, True, False)),
        ("off", "not_home", "charging_ready", Observation(False, False, False)),
        ("on", "Work", "charging_active", Observation(True, False, True)),
        ("unavailable", "unknown", "unavailable", Observation(None, None, None)),
        ("bogus", "home", "", Observation(None, True, None)),
        (None, None, None, Observation(None, None, None)),
    ],
)
async def test_observation(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    plugged: str | None,
    location: str | None,
    charger: str | None,
    expected: Observation,
) -> None:
    """Entity states map to known facts; unknown, unavailable and missing stay None."""
    for entity_id, value in (
        (PLUGGED_ENTITY, plugged),
        (LOCATION_ENTITY, location),
        (CHARGER_STATE_ENTITY, charger),
    ):
        if value is None:
            hass.states.async_remove(entity_id)
        else:
            hass.states.async_set(entity_id, value)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.runtime_data.observation() == expected


async def test_charging_unknown_without_charger_entity(
    hass: HomeAssistant, mock_rivian: FakeRivian
) -> None:
    """Without a charger state entity, charging is unknown: the status is b, never c."""
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    hass.states.async_set(CHARGER_STATE_ENTITY, "charging_active")
    entry = make_entry()
    options = dict(entry.options)
    options.pop("charger_state_entity")
    entry = MockConfigEntry(
        domain=entry.domain,
        title=entry.title,
        unique_id=entry.unique_id,
        data=dict(entry.data),
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    runtime = entry.runtime_data
    assert runtime.observation() == Observation(plugged=True, home=True, charging=None)
    assert runtime.charger_status == "b"


async def test_attribute_changes_do_not_tick(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A tracker reporting new attributes alone does not trigger a tick."""
    controller = loaded_entry.runtime_data.controller
    with patch.object(controller, "async_tick", wraps=controller.async_tick) as tick:
        hass.states.async_set(LOCATION_ENTITY, "home", {"gps_accuracy": 5})
        await hass.async_block_till_done(wait_background_tasks=True)
        assert tick.call_count == 0

        hass.states.async_set(LOCATION_ENTITY, "not_home", {"gps_accuracy": 5})
        await hass.async_block_till_done(wait_background_tasks=True)
        assert tick.call_count == 1
        assert tick.call_args.args[1].home is False


@pytest.mark.parametrize(
    ("wall_connector", "liveness", "at_charger", "alive"),
    [
        ("on", "pv", True, True),
        ("off", "off", False, True),
        ("unavailable", "unavailable", None, False),
        ("unknown", "unknown", None, False),
        (None, None, None, False),
    ],
)
async def test_observation_wall_connector_and_liveness(
    hass: HomeAssistant,
    mock_rivian: FakeRivian,
    wall_connector: str | None,
    liveness: str | None,
    at_charger: bool | None,
    alive: bool,
) -> None:
    """The Wall Connector maps on/off/unknown; liveness is False unless available."""
    for entity_id, value in ((WALL_CONNECTOR_ENTITY, wall_connector), (LIVENESS_ENTITY, liveness)):
        if value is not None:
            hass.states.async_set(entity_id, value)
    entry = make_entry(
        wall_connector_entity=WALL_CONNECTOR_ENTITY, controller_liveness_entity=LIVENESS_ENTITY
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    obs = entry.runtime_data.observation()
    assert obs.at_charger is at_charger
    assert obs.controller_alive is alive


async def test_unconfigured_liveness_is_unknown(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Without a liveness entity, the controller's liveness is unknown and does not block."""
    obs = loaded_entry.runtime_data.observation()
    assert obs.controller_alive is None
    assert obs.at_charger is None


def test_describe_schedules() -> None:
    """The notification text lists days, start, length, current and enabled."""
    text = describe_schedules(
        {
            "count": 1,
            "fingerprint": "0123456789ab",
            "written_by_schedule_control": True,
            "schedules": [
                {
                    "weekDays": ["Sunday"],
                    "startTime": 75,
                    "duration": 45,
                    "amperage": 40,
                    "enabled": True,
                }
            ],
        }
    )
    assert text.splitlines() == [
        "Rivian reports 1 charging schedule (fingerprint 0123456789ab, written by Rivian "
        "schedule control).",
        "1. Sunday from 01:15 for 45 min at 40 A, enabled",
    ]


@pytest.mark.parametrize(
    ("code", "remedy"),
    [
        ("write_rejected", "Rivian repeatedly refused schedule control's writes"),
        ("restore_rejected", "Rivian refused the owner's schedules when they were written back"),
    ],
)
def test_fault_message_names_the_first_step(code: str, remedy: str) -> None:
    """Faults whose general remedies do not fit name what to do first."""
    message = _fault_message("Test R1S", Fault(code, "Controller message.", dt_util.utcnow()))
    assert f"Fault: {code}" in message
    assert f"First: {remedy}" in message
    assert message.index("First: ") < message.index("What you can do:")
    assert "**Clear fault**" in message


def test_fault_message_without_specific_remedy() -> None:
    """Other faults get the general remedies, including the archive rule of force restore."""
    message = _fault_message("Test R1S", Fault("external_edit", "Changed.", dt_util.utcnow()))
    assert "First: " not in message
    assert "only written while Rivian holds a window that schedule control wrote" in message


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json"])
def test_translations_cover_issues_and_errors(name: str) -> None:
    """Every repair issue and error schedule control raises has a translation."""
    strings = json.loads((COMPONENT / name).read_text())
    for option in MONITORED_ENTITY_OPTIONS:
        issue = strings["issues"][f"missing_{option}"]
        assert "{entity_id}" in issue["description"]
        assert "{title}" in issue["description"]
    setup_issue = strings["issues"]["setup_failed_while_owning"]["description"]
    for placeholder in ("{title}", "{error}", "{path}"):
        assert placeholder in setup_issue
    assert "remove the integration" in setup_issue
    liveness_issue = strings["issues"]["missing_controller_liveness_entity"]["description"]
    assert "starts and renewals are blocked" in liveness_issue
    for key in ("force_restore_unverified", "force_restore_archive_unsafe"):
        assert strings["exceptions"][key]["message"]


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json"])
def test_translations_cover_the_overlap_issues(name: str) -> None:
    """Both overlap issues name the entry and the shared weekdays."""
    strings = json.loads((COMPONENT / name).read_text())
    for key in ("overlapping_owner_schedules", "overlapping_owner_schedules_fallback"):
        issue = strings["issues"][key]
        assert issue["title"]
        assert "{title}" in issue["description"]
        assert "{days}" in issue["description"]
    assert (
        "Restore enabled schedules only"
        in (strings["issues"]["overlapping_owner_schedules"]["description"])
    )
