"""Tests for the journal guard: a journal Home Assistant set aside is never ignored."""

from __future__ import annotations

from pathlib import Path

from conftest import FakeRivian, make_entry
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rivian_solar_adapter.const import DOMAIN, storage_key


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load this repository's integration."""


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    """Use an empty configuration directory, so storage files can be placed."""
    return str(tmp_path)


def _set_aside(hass: HomeAssistant, entry: MockConfigEntry) -> Path:
    """Leave a file as Home Assistant does when it cannot parse a journal."""
    storage = Path(hass.config.path(".storage"))
    storage.mkdir(exist_ok=True)
    corrupt = storage / f"{storage_key(entry.entry_id)}.corrupt.2026-10-24T12:00:00+00:00"
    _write(corrupt, "{not json")
    return corrupt


def _write(path: Path, text: str) -> None:
    path.write_text(text)


async def test_setup_refuses_after_corrupt_journal(
    hass: HomeAssistant, mock_rivian: FakeRivian
) -> None:
    """If the journal was moved aside, setup stops until the owner deals with it."""
    entry = make_entry()
    entry.add_to_hass(hass)
    corrupt = _set_aside(hass, entry)

    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert corrupt.name in (entry.reason or "")

    # Once the owner removes the set-aside file, the adapter starts.
    corrupt.unlink()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED


async def test_existing_journal_wins_over_old_corrupt_file(
    hass: HomeAssistant, mock_rivian: FakeRivian
) -> None:
    """An old set-aside file does not block a journal that was written since."""
    entry = make_entry()
    entry.add_to_hass(hass)
    _set_aside(hass, entry)
    _write(Path(hass.config.path(".storage", storage_key(entry.entry_id))), "{}")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED


async def test_remove_warns_about_corrupt_journal(
    hass: HomeAssistant, mock_rivian: FakeRivian
) -> None:
    """Removal cannot restore from a corrupt journal, so it tells the owner."""
    entry = make_entry()
    entry.add_to_hass(hass)
    corrupt = _set_aside(hass, entry)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    notification = hass.data["persistent_notification"][f"{DOMAIN}_{entry.entry_id}_restore_failed"]
    assert corrupt.name in notification["message"]
    assert corrupt.exists()
    assert mock_rivian.reads == 0
