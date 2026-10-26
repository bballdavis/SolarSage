"""A planning run on mocked states and recorder statistics: the history, the forecast, the
tariff from the Schedule helper, the decision and the dashboard sensors."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest

from custom_components.solar_ev_planner.const import DOMAIN, MODE_NO_DATA
from custom_components.solar_ev_planner.coordinator import PlannerCoordinator
from custom_components.solar_ev_planner.history import statistics_during_period

from .conftest import (
    FREE_HOURS,
    OPTIONS,
    RIV,
    RIVIAN,
    TESLA,
    E,
    FakeCars,
    cycle,
    make_entry,
    set_rivian,
    set_site,
    set_tesla,
    setup_entry,
    turn,
)

SHADOW = "switch.solar_ev_planner_shadow_mode"
PLANNING = "switch.solar_ev_planner_planning"
UNTIL = "sensor.r1s_planner_charging_until"
WANTED = "binary_sensor.r1s_planner_charging_wanted"
# 06:00 on Tuesday 16 June (UTC-7): the end of Monday night's free hours (20:00-06:00)
MONDAY_NIGHT_END = datetime(2026, 6, 16, 13, 0, tzinfo=dt_util.UTC)
TUESDAY_NIGHT_END = MONDAY_NIGHT_END + timedelta(days=1)


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


async def test_free_hours_charge_at_the_cars_maximum(
    site: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    set_rivian(hass, plugged="off")
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA))
    assert coordinator.data.mode == "empty"
    # 20:30, half an hour into the free hours: a car plugged in now charges at its maximum
    await cycle(hass, coordinator, freezer, minutes=8 * 60 + 29)
    set_site(hass, pv_w=0, grid_w=900, battery_w=0)
    set_rivian(hass, soc=60)
    await cycle(hass, coordinator, freezer)
    assert coordinator.data.mode == "night"
    assert hass.states.get("sensor.solar_ev_planner_mode").state == "night"
    assert hass.states.get("sensor.r1s_planner_planned_current").state == "48"
    nxt = hass.states.get("sensor.solar_ev_planner_next_change")
    assert nxt.attributes["amps"] == 0  # at the limit, before the free hours end


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


# --------------------------------------------------------------------------- charging until
# sensor.r1s_planner_charging_until: the end of the free hours while the planner charges the
# Rivian through them (Rivian schedule control then writes one window to it), else unknown.


def charging_until(hass: HomeAssistant) -> datetime | None:
    """The R1S's until sensor: its time, or None while it is unknown (never unavailable)."""
    state = hass.states.get(UNTIL)
    assert state is not None
    assert state.attributes["device_class"] == "timestamp"
    if state.state == STATE_UNKNOWN:
        return None
    value = dt_util.parse_datetime(state.state)
    assert value is not None and value.tzinfo is not None, state.state
    return value


async def free_hours_charging(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, cars: FakeCars, shadow: bool = False
) -> PlannerCoordinator:
    """From noon to 21:00 with the R1S plugged in: the planner commands it at 48 A through
    the free hours (unless in shadow mode)."""
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), cars)
    if not shadow:
        await turn(hass, SHADOW, False)
    set_site(hass, pv_w=0, grid_w=900, battery_w=0, soc=60)
    await cycle(hass, coordinator, freezer, minutes=9 * 60)
    assert coordinator.data.mode == "night"
    assert coordinator.data.decision.free_until == MONDAY_NIGHT_END
    if not shadow:
        assert coordinator.runtime["rivian"].commanded_a == 48
        assert charging_until(hass) == MONDAY_NIGHT_END
    return coordinator


async def test_charging_until_is_the_free_end_through_the_night(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    """Unknown by day while the R1S charges from the sun; the free hours' end from their
    start, unchanged all night; unknown again once they are over."""
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    await turn(hass, SHADOW, False)
    assert coordinator.runtime["rivian"].commanded_a  # charging by day
    assert charging_until(hass) is None
    set_site(hass, pv_w=0, grid_w=900, battery_w=0, soc=60)
    await cycle(hass, coordinator, freezer, minutes=8 * 60)  # 20:00, the free hours begin
    assert coordinator.data.mode == "night"
    assert coordinator.runtime["rivian"].commanded_a == 48
    assert charging_until(hass) == MONDAY_NIGHT_END
    assert dt_util.as_local(charging_until(hass)).hour == 6
    assert coordinator.runtime["rivian"].until == MONDAY_NIGHT_END
    changed = hass.states.get(UNTIL).last_changed
    fake_cars.clear()
    for _ in range(9):  # to 05:00: the same end, never changed, nothing sent
        await cycle(hass, coordinator, freezer, minutes=60)
        assert coordinator.data.mode == "night"
        assert charging_until(hass) == MONDAY_NIGHT_END
    await cycle(hass, coordinator, freezer, minutes=59)  # 05:59
    assert charging_until(hass) == MONDAY_NIGHT_END
    assert hass.states.get(UNTIL).last_changed == changed
    assert fake_cars.calls == []
    # 06:01: paid hours again
    await cycle(hass, coordinator, freezer, minutes=2)
    assert coordinator.data.mode == "day"
    assert charging_until(hass) is None
    assert coordinator.runtime["rivian"].until is None


async def test_charging_until_in_shadow_mode(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    """Shadow mode from the start publishes nothing; turning it off publishes the end, and
    turning it on again keeps it with the last command, which stands in shadow mode."""
    hass = site
    coordinator = await free_hours_charging(hass, freezer, fake_cars, shadow=True)
    assert coordinator.data.decision.shadow
    assert charging_until(hass) is None
    assert coordinator.runtime["rivian"].until is None
    for _ in range(3):
        await cycle(hass, coordinator, freezer, minutes=10)
        assert charging_until(hass) is None
    assert fake_cars.calls == []
    await turn(hass, SHADOW, False)
    assert charging_until(hass) == MONDAY_NIGHT_END
    assert fake_cars.calls == [("number", "set_value", 48), ("switch", "turn_on", RIV["switch"])]
    await turn(hass, SHADOW, True)
    assert charging_until(hass) == MONDAY_NIGHT_END
    await cycle(hass, coordinator, freezer, minutes=10)
    assert charging_until(hass) == MONDAY_NIGHT_END


async def test_charging_until_is_unknown_while_planning_is_off(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    """Off sends nothing and gives no end; on again the same night gives the same end, and
    the 48 A the car already holds is not sent again."""
    hass = site
    coordinator = await free_hours_charging(hass, freezer, fake_cars)
    fake_cars.clear()
    await turn(hass, PLANNING, False)
    assert charging_until(hass) is None
    for _ in range(3):
        await cycle(hass, coordinator, freezer, minutes=10)
        assert coordinator.data.mode == "off"
        assert charging_until(hass) is None
    await turn(hass, PLANNING, True)
    assert coordinator.data.mode == "night"
    assert charging_until(hass) == MONDAY_NIGHT_END
    await cycle(hass, coordinator, freezer, minutes=10)
    assert charging_until(hass) == MONDAY_NIGHT_END
    assert fake_cars.calls == []


async def test_charging_until_ends_with_the_session(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    """Unplugged in the free hours: the session ends and the end with it; plugged in again
    the same night: the end again for the new session."""
    hass = site
    coordinator = await free_hours_charging(hass, freezer, fake_cars)
    set_rivian(hass, plugged="off")
    await cycle(hass, coordinator, freezer, minutes=10)
    assert coordinator.data.mode == "empty"
    rt = coordinator.runtime["rivian"]
    assert rt.plugged_since is None and rt.until is None
    assert charging_until(hass) is None
    fake_cars.clear()
    set_rivian(hass, soc=50)
    await cycle(hass, coordinator, freezer, minutes=30)
    assert coordinator.data.mode == "night"
    assert fake_cars.calls == [("number", "set_value", 48), ("switch", "turn_on", RIV["switch"])]
    assert charging_until(hass) == MONDAY_NIGHT_END


async def test_charging_until_is_unknown_when_the_car_is_paused(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    """A pause, whether by day or in the free hours, gives no end; the next night's command
    gives that night's end."""
    hass = site
    coordinator = await free_hours_charging(hass, freezer, fake_cars)
    rt = coordinator.runtime["rivian"]
    # a 0 A command in the free hours (not one the core gives today): a pause, no end
    decision = coordinator.data.decision
    paused = replace(
        decision,
        commands=tuple(
            replace(c, amps=0) if c.car_id == "rivian" else c for c in decision.commands
        ),
    )
    fake_cars.clear()
    freezer.tick(timedelta(minutes=2))
    await coordinator._async_execute(paused, dt_util.now())
    await hass.async_block_till_done()
    assert fake_cars.calls == [("switch", "turn_off", RIV["switch"])]
    assert rt.commanded_a == 0 and rt.until is None
    assert charging_until(hass) is None
    # the planner's next night decision: 48 A again, and the end with it
    await cycle(hass, coordinator, freezer, minutes=2)
    assert rt.commanded_a == 48
    assert charging_until(hass) == MONDAY_NIGHT_END
    # Tuesday morning: the sun is up, the battery low on a dull day: a pause
    await cycle(hass, coordinator, freezer, minutes=11 * 60)  # 08:xx
    set_site(hass, soc=25, pv_w=1500, grid_w=0, battery_w=-1500, forecast_ratio=0.3)
    for _ in range(3):
        await cycle(hass, coordinator, freezer, minutes=20)
    assert coordinator.data.mode == "day" and rt.commanded_a == 0
    assert hass.states.get(RIV["switch"]).state == "off"
    assert charging_until(hass) is None
    # Tuesday night: that night's end
    set_site(hass, pv_w=0, grid_w=900, battery_w=0, soc=60)
    await cycle(hass, coordinator, freezer, minutes=11 * 60)  # 20:xx
    assert coordinator.data.mode == "night" and rt.commanded_a == 48
    assert charging_until(hass) == TUESDAY_NIGHT_END


async def test_a_car_full_in_the_free_hours_leaves_no_bare_request(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    """The R1S reaches its limit at 22:00 and stops by itself; the planner sends nothing
    more. Once the free hours are over, its request must still carry the end it was written
    with, or be paused: a bare 48 A request makes the schedule control write a new lease."""
    hass = site
    coordinator = await free_hours_charging(hass, freezer, fake_cars)
    fake_cars.clear()
    set_rivian(hass, soc=85, desired=True, requested=48, schedule_control_state="lease_active")
    await cycle(hass, coordinator, freezer, minutes=60)  # 22:00
    assert coordinator.data.mode == "full"
    await cycle(hass, coordinator, freezer, minutes=7 * 60 + 59)  # 05:59
    assert fake_cars.calls == []  # nothing while the night runs
    await cycle(hass, coordinator, freezer, minutes=61)  # 07:00
    assert coordinator.data.mode == "full"
    wanted = hass.states.get(WANTED).state
    assert charging_until(hass) == MONDAY_NIGHT_END or wanted == "off", wanted


async def test_the_tesla_has_no_charging_until(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    """The Tesla takes its current directly: no until sensor, and the R1S's says nothing
    while the Tesla charges through the free hours."""
    hass = site
    set_rivian(hass, plugged="off")
    set_tesla(hass, plugged="on")
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    await turn(hass, SHADOW, False)
    set_site(hass, pv_w=0, grid_w=900, battery_w=0, soc=60)
    await cycle(hass, coordinator, freezer, minutes=9 * 60)
    assert coordinator.data.mode == "night"
    assert coordinator.data.identification.car_id == "tesla"
    assert coordinator.runtime["tesla"].commanded_a == 48
    assert hass.states.get("sensor.roxxo_planner_charging_until") is None
    registry = er.async_get(hass)
    until = {
        e.entity_id
        for e in registry.entities.values()
        if e.platform == DOMAIN and e.translation_key == "charging_until"
    }
    assert until == {UNTIL}
    assert charging_until(hass) is None


async def test_charging_until_survives_a_restart(
    site: HomeAssistant,
    fake_cars: FakeCars,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """Reloaded in the free hours: the end is kept with the car's memory, the sensor shows
    it again at once, and nothing is sent again."""
    hass = site
    coordinator = await free_hours_charging(hass, freezer, fake_cars)
    entry = coordinator.config_entry
    fake_cars.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["runtime"]["rivian"]
    assert dt_util.parse_datetime(stored["until"]) == MONDAY_NIGHT_END
    coordinator = entry.runtime_data
    assert coordinator.runtime["rivian"].until == MONDAY_NIGHT_END
    assert charging_until(hass) == MONDAY_NIGHT_END
    await cycle(hass, coordinator, freezer, minutes=10)
    assert charging_until(hass) == MONDAY_NIGHT_END
    assert fake_cars.calls == []
