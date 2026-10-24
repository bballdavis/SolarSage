"""Tests for the runtime: observations, tick triggers and error handling."""

from __future__ import annotations

from unittest.mock import patch

from conftest import (
    CHARGER_STATE_ENTITY,
    LOCATION_ENTITY,
    PLUGGED_ENTITY,
    FakeResponse,
    FakeRivian,
    make_entry,
)
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rivian_solar_adapter.controller import Observation
from custom_components.rivian_solar_adapter.runtime import describe_schedules


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
    """Without a charger state entity, charging is unknown and evcc sees b, never c."""
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
    assert runtime.evcc_status == "b"


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


async def test_unexpected_error_is_logged_and_ticking_continues(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_rivian: FakeRivian,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed response is logged; later requests are still handled."""
    good = mock_rivian.get_charging_schedules

    async def malformed(vehicle_id: str) -> FakeResponse:
        return FakeResponse(["not", "a", "dict"])  # type: ignore[arg-type]

    runtime = loaded_entry.runtime_data
    mock_rivian.get_charging_schedules = malformed
    await runtime.async_request_charge(True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "Unexpected error while driving the Rivian charging schedule" in caplog.text
    assert mock_rivian.writes == []

    mock_rivian.get_charging_schedules = good
    runtime.request_tick()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(mock_rivian.writes) == 1
    assert runtime.controller.state(runtime.last_tick_at).value == "lease_active"


def test_describe_schedules() -> None:
    """The notification text lists days, start, length, current and enabled."""
    text = describe_schedules(
        {
            "count": 1,
            "hash": "0123456789ab",
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
        "Rivian reports 1 charging schedule (hash 0123456789ab).",
        "1. Sunday from 01:15 for 45 min at 40 A, enabled",
    ]
