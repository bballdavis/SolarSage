"""Pinned Home Assistant state-shape checks for forecast source handling."""

import asyncio
from datetime import UTC, datetime, timedelta
import inspect
from types import MappingProxyType

import pytest

pytest.importorskip("homeassistant")

from custom_components.rivian.solar_ev_charging.coordinator import SolarEvCoordinator
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


def _entry(options):
    extra = (
        {"subentries_data": []}
        if "subentries_data" in inspect.signature(ConfigEntry).parameters
        else {}
    )
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain="rivian",
        title="Forecast source test",
        data={},
        options=options,
        source="user",
        unique_id="forecast-source-test",
        discovery_keys=MappingProxyType({}),
        **extra,
    )


def _options(**changes):
    values = {
        "forecast_mode": "protect",
        "forecast_remaining_entity": "sensor.remaining_today",
        "forecast_next_hour_entity": "sensor.next_hour",
        "forecast_value_field": "estimate10",
        "forecast_source_updated_entity": "sensor.solcast_last_polled",
        "forecast_source_max_age_hours": 6,
        "forecast_confidence": 0.75,
        "battery_capacity_kwh": 60,
        "battery_target_soc": 60,
        "household_budget_kwh": 2,
        "minimum_session_minutes": 30,
    }
    values.update(changes)
    return values


def _set_solcast_energy(hass, *, next_attributes=None):
    hass.states.async_set(
        "sensor.remaining_today",
        "12000",
        {
            "unit_of_measurement": "Wh",
            "device_class": "energy",
            "estimate": 12000,
            "estimate10": 9800,
            "estimate90": 14500,
        },
    )
    hass.states.async_set(
        "sensor.next_hour",
        "2200",
        next_attributes
        or {
            "unit_of_measurement": "Wh",
            "device_class": "energy",
            "estimate": 2200,
            "estimate10": 1600,
            "estimate90": 2800,
        },
    )


def test_stale_source_blocks_even_when_derived_solcast_sensors_are_fresh(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        coordinator = SolarEvCoordinator(hass, _entry(_options()))
        _set_solcast_energy(hass)
        hass.states.async_set(
            "sensor.solcast_last_polled",
            (datetime.now(UTC) - timedelta(hours=7)).isoformat(),
        )

        result = coordinator._forecast_data(50, 240)

        assert result["reason"] == "forecast_stale"
        assert result["allow_start"] is False
        assert result["remaining_kwh"] == 9.8
        assert result["next_hour_kwh"] == 1.6
        assert result["source_status"] == "stale"
        assert result["source_age_seconds"] > 6 * 3600
        assert result["value_field"] == "estimate10"
        assert result["source_entity"] == "sensor.solcast_last_polled"

    asyncio.run(run())


def test_invalid_or_future_source_and_missing_percentile_fail_closed(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        coordinator = SolarEvCoordinator(hass, _entry(_options()))
        _set_solcast_energy(hass)

        hass.states.async_set("sensor.solcast_last_polled", "not-a-timestamp")
        assert coordinator._forecast_data(50, 240)["reason"] == "forecast_invalid"

        hass.states.async_set(
            "sensor.solcast_last_polled",
            (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        )
        assert coordinator._forecast_data(50, 240)["reason"] == "forecast_invalid"

        hass.states.async_set(
            "sensor.solcast_last_polled",
            (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        )
        _set_solcast_energy(
            hass,
            next_attributes={"unit_of_measurement": "Wh", "estimate": 2200},
        )
        result = coordinator._forecast_data(50, 240)
        assert result["reason"] == "forecast_invalid"
        assert result["next_hour_kwh"] is None
        assert result["ev_budget_kwh"] is None

        hass.states.async_set("sensor.next_hour", "unavailable")
        assert coordinator._forecast_data(50, 240)["reason"] == "forecast_invalid"

    asyncio.run(run())


@pytest.mark.parametrize(
    "soc,voltage", [(None, 240), (50, None), (True, 240), (101, 240)]
)
def test_missing_or_invalid_physical_inputs_never_publish_a_valid_budget(
    tmp_path, soc, voltage
):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        coordinator = SolarEvCoordinator(hass, _entry(_options()))
        _set_solcast_energy(hass)
        hass.states.async_set(
            "sensor.solcast_last_polled", datetime.now(UTC).isoformat()
        )
        result = coordinator._forecast_data(soc, voltage)
        assert result["reason"] == "forecast_invalid"
        assert result["allow_start"] is False
        assert result["ev_budget_kwh"] is None
        assert result["battery_needed_kwh"] is None

    asyncio.run(run())


def test_stale_derived_entity_cannot_borrow_source_freshness(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        coordinator = SolarEvCoordinator(hass, _entry(_options()))
        _set_solcast_energy(hass)
        hass.states.async_set(
            "sensor.solcast_last_polled", datetime.now(UTC).isoformat()
        )
        native_fresh = coordinator._state_fresh
        coordinator._state_fresh = lambda entity, age: (
            False if entity == "sensor.next_hour" else native_fresh(entity, age)
        )
        result = coordinator._forecast_data(50, 240)
        assert result["source_status"] == "fresh"
        assert result["derived_entities_fresh"] is False
        assert result["reason"] == "forecast_stale"
        assert result["allow_start"] is False

    asyncio.run(run())


@pytest.mark.parametrize("field", [["estimate10"], {"estimate": 10}, None])
def test_malformed_persisted_forecast_field_fails_closed_without_exception(
    tmp_path, field
):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        coordinator = SolarEvCoordinator(
            hass, _entry(_options(forecast_value_field=field))
        )
        _set_solcast_energy(hass)
        hass.states.async_set(
            "sensor.solcast_last_polled", datetime.now(UTC).isoformat()
        )
        result = coordinator._forecast_data(50, 240)
        assert result["reason"] == "forecast_invalid"
        assert result["allow_start"] is False
        assert result["remaining_kwh"] is None

    asyncio.run(run())
