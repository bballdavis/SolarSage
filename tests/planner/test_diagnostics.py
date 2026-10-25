"""Diagnostics: the mapping, settings and the last decision, without coordinates."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant

from custom_components.solar_ev_planner.diagnostics import async_get_config_entry_diagnostics

from .conftest import RIVIAN, TESLA, FakeCars, make_entry, setup_entry


async def test_diagnostics_are_complete_and_redacted(
    site: HomeAssistant, fake_cars: FakeCars
) -> None:
    hass = site
    entry = make_entry(RIVIAN, TESLA)
    await setup_entry(hass, entry, fake_cars)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag)  # JSON-safe
    # the trackers' coordinates are never read, so they cannot appear
    assert "32.87" not in text and "-117.22" not in text
    assert diag["entry"]["options"]["pv_power"] == "sensor.sol_ark_inverter_pv_power"
    assert diag["cars"]["rivian"]["switch"] == "switch.r1s_schedule_control_solar_charging"
    assert diag["settings"]["shadow"] is True
    assert diag["decision"]["mode"] == "day" and diag["decision"]["car_id"] == "rivian"
    assert diag["last_cycle"]["identification"]["car_id"] == "rivian"
    assert diag["history"]["profile_days"] == 14
    assert diag["healthy"] is True
    assert diag["schedules"]["schedule.free_hours"]["monday"]


async def test_redaction_covers_identifiers(site: HomeAssistant, fake_cars: FakeCars) -> None:
    hass = site
    car = RIVIAN | {"vin": "not-a-real-vin", "latitude": 1.0}
    entry = make_entry(car)
    await setup_entry(hass, entry, fake_cars)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["cars"]["rivian"]["vin"] == "**REDACTED**"
    assert diag["cars"]["rivian"]["latitude"] == "**REDACTED**"
