"""A planning run on mocked states and recorder statistics: the history, the forecast, the
tariff from the Schedule helper, the decision and the dashboard sensors."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.solar_ev_planner.const import (
    CONF_BATTERY_FREE_HOURS,
    DEFAULT_SITE_OPTIONS,
    LEGACY_BATTERY_HOLDS,
    MODE_NO_DATA,
)
from custom_components.solar_ev_planner.history import statistics_during_period

from .conftest import (
    FREE_HOURS,
    OPTIONS,
    RIVIAN,
    TESLA,
    E,
    FakeCars,
    cycle,
    make_entry,
    set_rivian,
    set_site,
    setup_entry,
)


@pytest.fixture(autouse=True)
def _ha(site: HomeAssistant, fake_cars: FakeCars) -> None:
    """Noon on a sunny Monday with the Rivian plugged in (conftest.site)."""


async def test_run_reads_history_forecast_and_tariff(site: HomeAssistant) -> None:
    hass = site
    with patch(
        "custom_components.solar_ev_planner.history.statistics_during_period",
        wraps=statistics_during_period,
    ) as stats:
        coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA))
    # 14 days of hourly means, and the last 24 h of 5-minute statistics, off the event loop
    periods = [c.args[4] for c in stats.call_args_list]
    assert periods == ["hour", "5minute"]
    assert {
        c.args[3] == {E["pv_power"], E["grid_power"], E["battery_power"], E["wall_power"]}
        for c in stats.call_args_list
    } == {True}
    assert coordinator.history.profile.n_days == 14
    assert coordinator.history.hourly_rows >= 14 * 24
    # the free hours from the Schedule helper (20:00-06:00, 24:00 as the end of the day)
    assert coordinator.schedules[FREE_HOURS]["monday"] == [
        {"from": "00:00:00", "to": "06:00:00"},
        {"from": "20:00:00", "to": "24:00:00"},
    ]
    data = coordinator.data
    assert data.forecast_slots == 96
    assert data.mode == "day"
    assert data.identification.car_id == "rivian"
    assert data.decision is not None and data.decision.shadow
    assert data.site.house_kw == pytest.approx(7.8 - 0.15 - 2.9)

    # the dashboard
    assert hass.states.get("sensor.solar_ev_planner_mode").state == "day"
    reason = hass.states.get("sensor.solar_ev_planner_reason").state
    assert reason.startswith("Solar charging at") and len(reason) <= 255
    assert hass.states.get("sensor.solar_ev_planner_car_on_charger").state == "rivian"
    planned = hass.states.get("sensor.r1s_planner_planned_current")
    assert int(planned.state) >= 8 and int(planned.state) % 2 == 0
    assert hass.states.get("sensor.roxxo_planner_planned_current").state == "0"
    low = hass.states.get("sensor.solar_ev_planner_battery_minimum")
    assert 0 < float(low.state) <= 100 and low.attributes["trajectory"]
    assert hass.states.get("sensor.solar_ev_planner_battery_minimum_time").state != "unknown"
    assert hass.states.get("sensor.solar_ev_planner_next_change").state != "unknown"
    correction = hass.states.get("sensor.solar_ev_planner_forecast_correction")
    assert 0.3 < float(correction.state) < 1.8
    assert correction.attributes["remaining_p50_kwh"] > 0
    outlook = hass.states.get("sensor.r1s_planner_outlook")
    assert outlook.state and outlook.attributes["need_kwh"] > 0
    assert float(hass.states.get("sensor.r1s_planner_energy_from_sun").state) >= 0
    assert hass.states.get("sensor.roxxo_planner_energy_in_free_hours") is not None
    assert hass.states.get("sensor.r1s_planner_energy_paid") is not None


async def test_free_hours_and_the_night_window(
    site: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    set_rivian(hass, plugged="off")
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA))
    assert coordinator.data.mode == "empty"
    # 20:30: free hours, before the 22:00 night window: a car plugged in now waits
    await cycle(hass, coordinator, freezer, minutes=8 * 60 + 29)
    set_site(hass, pv_w=0, grid_w=900, battery_w=0)
    set_rivian(hass, soc=60)
    await cycle(hass, coordinator, freezer)
    assert coordinator.data.mode == "wait"
    assert hass.states.get("sensor.r1s_planner_planned_current").state == "0"
    nxt = hass.states.get("sensor.solar_ev_planner_next_change")
    assert dt_util.as_local(dt_util.parse_datetime(nxt.state)).hour == 22
    # 22:30: night charging at the car's maximum
    await cycle(hass, coordinator, freezer, minutes=120)
    assert coordinator.data.mode == "night"
    assert hass.states.get("sensor.r1s_planner_planned_current").state == "48"


async def test_missing_site_readings_mean_no_plan(
    site: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN))
    hass.states.async_set(E["battery_soc"], STATE_UNAVAILABLE)
    await cycle(hass, coordinator, freezer)
    assert coordinator.data.mode == MODE_NO_DATA
    assert "battery_soc" in coordinator.data.reason
    assert hass.states.get("sensor.solar_ev_planner_mode").state == MODE_NO_DATA
    assert hass.states.get("sensor.solar_ev_planner_heartbeat").state == STATE_UNAVAILABLE


async def test_site_sensors_that_went_quiet_are_stale(
    site: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """One unchanged value is fine; all of them silent for half an hour is not."""
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN))
    freezer.tick(timedelta(minutes=10))
    await coordinator.async_refresh()
    assert coordinator.data.mode == "day"  # nothing reported for 10 min: still fresh
    freezer.tick(timedelta(minutes=25))
    await coordinator.async_refresh()
    assert coordinator.data.mode == MODE_NO_DATA
    assert "have not reported" in coordinator.data.reason


async def test_unreadable_free_hours_schedule_stops_planning(
    site: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    options = OPTIONS | {"free_hours": "schedule.does_not_exist"}
    coordinator = await setup_entry(hass, make_entry(RIVIAN, options=options))
    assert coordinator.data.mode == MODE_NO_DATA
    assert "free-hours schedule" in coordinator.data.reason


async def test_recorder_without_history_uses_a_flat_profile(
    site: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    options = OPTIONS | {"pv_power": "sensor.no_history_pv"}
    hass.states.async_set("sensor.no_history_pv", "7800", {"unit_of_measurement": "W"})
    coordinator = await setup_entry(hass, make_entry(RIVIAN, options=options))
    assert coordinator.history.profile.n_days == 0
    assert "pv_power" in coordinator.history.missing
    assert coordinator.data.mode == "day"


async def test_memory_and_settings_survive_a_reload(
    site: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    entry = make_entry(RIVIAN)
    coordinator = await setup_entry(hass, entry)
    await coordinator.async_set_setting("floor_pct", 35.0)
    since = coordinator.runtime["rivian"].plugged_since
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.settings["floor_pct"] == 35.0
    assert coordinator.runtime["rivian"].plugged_since == since
    assert hass.states.get("number.solar_ev_planner_battery_floor").state == "35.0"


async def test_new_day_records_yesterdays_solar(
    site: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN))
    yesterday = dt_util.now().date()
    await cycle(hass, coordinator, freezer, minutes=12 * 60 + 5)
    set_rivian(hass)
    await cycle(hass, coordinator, freezer)
    assert [d.day for d in coordinator.pv_days] == [yesterday]
    assert coordinator.pv_days[0].forecast_kwh > 30


@pytest.mark.parametrize(("holds", "mode"), [(True, "hold"), (False, "normal")])
async def test_an_entry_from_1_1_keeps_its_battery_behaviour(
    site: HomeAssistant, holds: bool, mode: str
) -> None:
    old = {k: v for k, v in OPTIONS.items() if k != CONF_BATTERY_FREE_HOURS}
    entry = make_entry(RIVIAN, options=old | {LEGACY_BATTERY_HOLDS: holds}, minor_version=1)
    coordinator = await setup_entry(site, entry)
    assert (entry.version, entry.minor_version) == (1, 2)
    assert LEGACY_BATTERY_HOLDS not in entry.options
    assert entry.options[CONF_BATTERY_FREE_HOURS] == mode
    assert coordinator.options[CONF_BATTERY_FREE_HOURS] == mode


async def test_new_entries_refill_the_battery_in_free_hours(site: HomeAssistant) -> None:
    assert DEFAULT_SITE_OPTIONS[CONF_BATTERY_FREE_HOURS] == "refill"
    entry = make_entry(RIVIAN, options=dict(OPTIONS), minor_version=1)
    await setup_entry(site, entry)
    assert entry.minor_version == 2
    assert entry.options[CONF_BATTERY_FREE_HOURS] == "refill"
