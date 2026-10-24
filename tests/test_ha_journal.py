"""Tests for the durable, vehicle-keyed ownership journal."""

from __future__ import annotations

import json
import stat
from unittest.mock import patch

from conftest import (
    OWNER_SCHEDULES,
    VEHICLE_ID,
    FakeRivian,
    journal_path,
    make_entry,
    read_journal,
    write_journal_file,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.file import WriteError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rivian_solar_adapter.const import (
    DOMAIN,
    journal_key,
    legacy_storage_key,
)
from custom_components.rivian_solar_adapter.runtime import restore_failed_notification_id


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load this repository's integration."""


async def _set_up(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def test_journal_key_hashes_the_vehicle_id() -> None:
    """The file name identifies the vehicle without containing its ID."""
    key = journal_key(VEHICLE_ID)
    assert key.startswith(f"{DOMAIN}.")
    assert len(key.removeprefix(f"{DOMAIN}.")) == 16
    assert VEHICLE_ID not in key
    assert journal_key(VEHICLE_ID) == key
    assert journal_key("another-vehicle") != key


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        json.dumps({"version": 1, "key": "someone-else", "data": {}}),
        json.dumps({"version": 1, "key": journal_key(VEHICLE_ID), "data": {"version": 99}}),
        json.dumps({"version": 1, "key": journal_key(VEHICLE_ID), "data": {"version": 2}}),
    ],
    ids=["invalid_json", "not_an_object", "wrong_key", "unknown_version", "incomplete"],
)
async def test_unreadable_journal_stops_setup(
    hass: HomeAssistant, mock_rivian: FakeRivian, text: str
) -> None:
    """A journal that cannot be used stops setup and is left exactly as it was."""
    path = write_journal_file(hass, text)
    entry = make_entry()
    await _set_up(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert journal_key(VEHICLE_ID) in (entry.reason or "")
    assert path.read_text() == text
    assert mock_rivian.reads == 0


async def test_journal_is_saved_privately_and_atomically(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """The journal is on disk, readable by its owner only, before Rivian is written."""
    saved: list[dict | None] = []
    original_set = mock_rivian.set_charging_schedules

    async def set_and_check(vehicle_id: str, schedules: list) -> object:
        saved.append(read_journal(hass))
        return await original_set(vehicle_id, schedules)

    mock_rivian.set_charging_schedules = set_and_check
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {
            ATTR_ENTITY_ID: er.async_get(hass).async_get_entity_id(
                "switch", DOMAIN, f"{VEHICLE_ID}_solar_charging"
            )
        },
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    # The intent and the owner's list were on disk when the write went out.
    [before_write] = saved
    assert before_write["phase"] == "writing"
    assert before_write["original"] == OWNER_SCHEDULES
    assert read_journal(hass)["phase"] == "owned"
    mode = stat.S_IMODE(journal_path(hass).stat().st_mode)
    assert mode & 0o077 == 0


async def test_failed_save_rejects_the_request(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_rivian: FakeRivian
) -> None:
    """If the journal cannot be written, the request fails and nothing is written."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{VEHICLE_ID}_solar_charging"
    )
    with (
        patch(
            "custom_components.rivian_solar_adapter.store.write_utf8_file_atomic",
            side_effect=WriteError("No space left on device"),
        ),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )
    assert err.value.translation_key == "journal_save_failed"
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_rivian.writes == []
    assert read_journal(hass) is None


async def test_legacy_journal_with_ownership_stops_setup(
    hass: HomeAssistant, mock_rivian: FakeRivian
) -> None:
    """A journal keyed by config entry, from an earlier version, is never ignored."""
    entry = make_entry()
    legacy = {"version": 1, "data": {"version": 1, "phase": "owned", "original": []}}
    path = write_journal_file(hass, json.dumps(legacy), legacy_storage_key(entry.entry_id))
    await _set_up(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert legacy_storage_key(entry.entry_id) in (entry.reason or "")
    assert path.exists()

    # Once the owner has dealt with it and deleted it, the adapter starts.
    path.unlink()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED


async def test_idle_legacy_journal_is_deleted(hass: HomeAssistant, mock_rivian: FakeRivian) -> None:
    """A legacy journal with nothing to restore is cleaned up."""
    entry = make_entry()
    legacy = {"version": 1, "data": {"version": 1, "phase": "idle", "original": None}}
    path = write_journal_file(hass, json.dumps(legacy), legacy_storage_key(entry.entry_id))
    await _set_up(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert not path.exists()


async def test_remove_warns_about_unreadable_journal(
    hass: HomeAssistant, mock_rivian: FakeRivian
) -> None:
    """Removal cannot restore from an unreadable journal, so it tells the owner."""
    path = write_journal_file(hass, "{not json")
    entry = make_entry()
    entry.add_to_hass(hass)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    notification = hass.data["persistent_notification"][
        restore_failed_notification_id(VEHICLE_ID)
    ]
    assert "unreadable" in notification["message"]
    assert journal_key(VEHICLE_ID) in notification["message"]
    assert path.exists()
    assert mock_rivian.reads == 0
