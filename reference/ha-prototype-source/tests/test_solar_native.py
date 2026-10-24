"""Local Home Assistant service/state/storage tests, without vehicle network I/O."""

import asyncio
import inspect
from types import MappingProxyType

import pytest

pytest.importorskip("homeassistant")

from custom_components.rivian.solar_ev_charging.const import (
    CONF_SOLAR_ENABLED,
    CONF_VEHICLE_MODE,
    DOMAIN,
    STORE_VERSION,
)
from custom_components.rivian.solar_ev_charging.coordinator import SolarEvCoordinator
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store


def _entry():
    # Native ConfigEntry added subentries after the minimum supported HA release.
    extra = (
        {"subentries_data": []}
        if "subentries_data" in inspect.signature(ConfigEntry).parameters
        else {}
    )
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain="rivian",
        title="Local solar test",
        data={},
        options={CONF_SOLAR_ENABLED: False, CONF_VEHICLE_MODE: "tesla"},
        source="user",
        unique_id="solar-test",
        discovery_keys=MappingProxyType({}),
        **extra,
    )


def test_native_coordinator_initializes_disabled_without_vehicle_commands(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        entry = _entry()
        coordinator = SolarEvCoordinator(hass, entry)
        assert coordinator._initialized is False
        coordinator.async_set_updated_data({"state": "starting"})
        await coordinator.async_initialize()
        result = await coordinator._async_update_data()
        assert coordinator._initialized is True
        assert result["enabled"] is False
        assert result["reason"] == "disabled"
        assert not result["pending_cleanup"]
        await coordinator.async_shutdown()
        await hass.async_block_till_done()

    asyncio.run(run())


def test_legacy_unbound_journal_is_preserved_and_blocks_automatic_resume(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        entry = _entry()
        legacy = {
            "vehicle_id": "old-rivian",
            "base_schedules": [],
            "base_hash": "original",
            "last_hash": "lease",
        }
        store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        await store.async_save({"enabled": True, "journal": legacy})
        coordinator = SolarEvCoordinator(hass, entry)
        await coordinator.async_initialize()
        assert not coordinator.enabled
        assert "cleanup_binding_missing" in coordinator._fault
        assert coordinator._runtime["journal"]["base_hash"] == "original"
        assert coordinator._runtime["journal"]["stop_required"]
        await hass.async_block_till_done()

    asyncio.run(run())


def test_native_accepted_noop_off_retains_durable_cleanup_journal(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        entry = _entry()
        coordinator = SolarEvCoordinator(hass, entry)
        await coordinator.async_initialize()
        hass.states.async_set("switch.test_ev", "on")
        hass.states.async_set("sensor.test_wall", "2000", {"unit_of_measurement": "W"})
        calls = []

        async def ignored_off(call):
            # The service accepts the request but the hardware state never changes.
            calls.append(call.data)

        hass.services.async_register("switch", "turn_off", ignored_off)
        coordinator._feedback_timeout = lambda: 1
        coordinator._runtime["journal"] = {
            "vehicle": "tesla",
            "switch_entity": "switch.test_ev",
            "wall_power_entity": "sensor.test_wall",
            "phase": "charging_confirmed",
            "stop_required": True,
        }
        assert await coordinator._async_safe_release_locked("native_test") is False
        assert calls == [{"entity_id": "switch.test_ev"}]
        assert hass.states.get("switch.test_ev").state == "on"
        assert coordinator._runtime["journal"]["phase"] == "cleanup_pending"
        assert coordinator._fault
        # Read a new Store, not the coordinator's in-memory dictionary.
        persisted = await Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        ).async_load()
        assert persisted["journal"]["switch_entity"] == "switch.test_ev"
        assert persisted["journal"]["stop_required"] is True
        assert persisted["fault"]
        # No listener/timer was registered in this local test; retain the evidence
        # rather than re-running cleanup during teardown.
        await hass.async_block_till_done()

    asyncio.run(run())
