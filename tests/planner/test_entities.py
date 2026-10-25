"""The planner's entities: their IDs, the settings they hold (persisted, passed to the core),
the per-car plans, the price windows, and reloading when a car is added."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest

from custom_components.solar_ev_planner.const import DOMAIN, SUBENTRY_CAR, SUBENTRY_PRICE

from .conftest import (
    RIVIAN,
    TESLA,
    FakeCars,
    cycle,
    make_entry,
    setup_entry,
)

PLANNER_ENTITIES = {
    "switch.solar_ev_planner_planning",
    "switch.solar_ev_planner_shadow_mode",
    "switch.solar_ev_planner_daytime_solar_charging",
    "time.solar_ev_planner_night_window_start",
    "time.solar_ev_planner_night_window_end",
    "number.solar_ev_planner_night_charging_current",
    "select.solar_ev_planner_caution",
    "number.solar_ev_planner_battery_floor",
    "number.solar_ev_planner_battery_wanted_at_free_hours",
    "number.solar_ev_planner_minimum_time_between_changes",
    "number.solar_ev_planner_largest_change",
    "number.solar_ev_planner_paid_grid_tolerance",
    "number.solar_ev_planner_import_price",
    "number.solar_ev_planner_solar_buyback",
    "select.solar_ev_planner_charger_car_override",
    "sensor.solar_ev_planner_mode",
    "sensor.solar_ev_planner_reason",
    "sensor.solar_ev_planner_next_change",
    "sensor.solar_ev_planner_battery_minimum",
    "sensor.solar_ev_planner_battery_minimum_time",
    "sensor.solar_ev_planner_forecast_correction",
    "sensor.solar_ev_planner_car_on_charger",
    "sensor.solar_ev_planner_heartbeat",
}
CAR_ENTITIES = {
    "binary_sensor.{car}_planner_charging_wanted",
    "sensor.{car}_planner_planned_current",
    "sensor.{car}_planner_outlook",
    "sensor.{car}_planner_energy_from_sun",
    "sensor.{car}_planner_energy_in_free_hours",
    "sensor.{car}_planner_energy_paid",
    "number.{car}_planner_charge_limit",
    "number.{car}_planner_plan_target",
    "time.{car}_planner_plan_time",
    "select.{car}_planner_plan_days",
}


@pytest.fixture(autouse=True)
def _ha(site: HomeAssistant) -> None:
    """Noon on a sunny Monday with the Rivian plugged in (conftest.site)."""


async def test_entity_ids(site: HomeAssistant, fake_cars: FakeCars) -> None:
    hass = site
    await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    registry = er.async_get(hass)
    ids = {e.entity_id for e in registry.entities.values() if e.platform == DOMAIN}
    expected = set(PLANNER_ENTITIES)
    for car in ("r1s", "roxxo"):
        expected |= {e.format(car=car) for e in CAR_ENTITIES}
    expected.add("number.roxxo_planner_restore_current_at_unplug")  # the Tesla only
    assert ids == expected
    for entity_id in expected:
        assert hass.states.get(entity_id) is not None, entity_id


async def test_settings_reach_the_core_and_persist(
    site: HomeAssistant, fake_cars: FakeCars
) -> None:
    hass = site
    entry = make_entry(RIVIAN)
    coordinator = await setup_entry(hass, entry, fake_cars)
    core = coordinator.core_settings()
    assert (core.enabled, core.shadow, core.caution, core.floor_pct) == (
        True,
        True,
        "conservative",
        30.0,
    )
    assert (core.night_start, core.night_end, core.night_current_a) == ("22:00", "06:00", None)
    assert (core.min_change_min, core.max_step_a, core.paid_tolerance_min) == (15.0, 6, 5.0)
    assert core.wanted_at_free_start_pct == 40.0

    async def call(domain: str, service: str, entity_id: str, **data: object) -> None:
        await hass.services.async_call(
            domain, service, {"entity_id": entity_id, **data}, blocking=True
        )

    await call("select", "select_option", "select.solar_ev_planner_caution", option="near_certain")
    await call("number", "set_value", "number.solar_ev_planner_night_charging_current", value=32)
    await call("time", "set_value", "time.solar_ev_planner_night_window_start", time="21:30:00")
    await call("number", "set_value", "number.solar_ev_planner_battery_floor", value=25)
    await call("number", "set_value", "number.solar_ev_planner_import_price", value=0.4)
    core = coordinator.core_settings()
    assert core.caution == "near-certain" and core.caution_p() == 0.97
    assert core.night_current_a == 32 and core.night_start == "21:30"
    assert core.floor_pct == 25
    assert hass.states.get("select.solar_ev_planner_caution").state == "near_certain"
    assert hass.states.get("time.solar_ev_planner_night_window_start").state == "21:30:00"
    price = hass.states.get("number.solar_ev_planner_import_price")
    assert price.state == "0.4"
    assert price.attributes["unit_of_measurement"] == f"{hass.config.currency}/kWh"
    # 48 A, the top of the range, is "the car's maximum" again
    await call("number", "set_value", "number.solar_ev_planner_night_charging_current", value=48)
    assert coordinator.core_settings().night_current_a is None

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    core = entry.runtime_data.core_settings()
    assert core.caution == "near-certain" and core.floor_pct == 25
    assert entry.runtime_data.settings["price"] == 0.4


async def test_car_plans(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    assert coordinator._plans("tesla", dt_util.now()) == ()  # off by default
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.roxxo_planner_plan_days", "option": "weekdays"},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.roxxo_planner_plan_target", "value": 70},
        blocking=True,
    )
    await cycle(hass, coordinator, freezer)
    plans = coordinator._plans("tesla", dt_util.now())
    tomorrow = dt_util.now().date() + timedelta(days=1)
    assert plans[0].target_pct == 70
    assert plans[0].deadline == datetime.combine(tomorrow, time(7, 30), dt_util.now().tzinfo)
    outlook = hass.states.get("sensor.roxxo_planner_outlook")
    assert outlook.attributes["target"] == 70 and outlook.attributes["by"].startswith(
        tomorrow.isoformat()
    )
    # once: the next 13:00, then the plan turns itself off
    await hass.services.async_call(
        "time",
        "set_value",
        {"entity_id": "time.roxxo_planner_plan_time", "time": "13:00:00"},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.roxxo_planner_plan_days", "option": "once"},
        blocking=True,
    )
    await cycle(hass, coordinator, freezer)
    (plan,) = coordinator._plans("tesla", dt_util.now())
    assert plan.deadline.hour == 13 and plan.deadline.date() == dt_util.now().date()
    await cycle(hass, coordinator, freezer, minutes=60)
    assert hass.states.get("select.roxxo_planner_plan_days").state == "off"
    # the planner's own limit, below the car's
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.r1s_planner_charge_limit", "value": 60},
        blocking=True,
    )
    await cycle(hass, coordinator, freezer)
    assert hass.states.get("sensor.r1s_planner_planned_current").attributes["limit"] == 60


async def test_price_window_entity_and_tariff(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    hass.states.async_set("schedule.peak", "on")
    entry = make_entry(RIVIAN)
    entry.add_to_hass(hass)
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data={"schedule": "schedule.free_hours", "label": "Peak", "price": 0.45},
            subentry_type=SUBENTRY_PRICE,
            title="Peak",
            unique_id="schedule.free_hours",
        ),
    )
    coordinator = await setup_entry(hass, entry, fake_cars)
    state = hass.states.get("number.solar_ev_planner_peak_price")
    assert state is not None and state.state == "0.45"
    tariff, problem = coordinator._tariff()
    assert problem is None
    assert [w.label for w in tariff.windows] == ["free", "Peak"]
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.solar_ev_planner_peak_price", "value": 0.5},
        blocking=True,
    )
    tariff, _ = coordinator._tariff()
    assert tariff.windows[1].import_price == 0.5


async def test_adding_a_car_reloads_the_planner(site: HomeAssistant, fake_cars: FakeCars) -> None:
    hass = site
    entry = make_entry(RIVIAN)
    await setup_entry(hass, entry, fake_cars)
    assert hass.states.get("sensor.roxxo_planner_planned_current") is None
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(data=TESLA, subentry_type=SUBENTRY_CAR, title="Roxxo", unique_id="tesla"),
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.roxxo_planner_planned_current") is not None
    assert set(entry.runtime_data.cars) == {"rivian", "tesla"}
    assert hass.states.get("select.solar_ev_planner_charger_car_override").attributes[
        "options"
    ] == ["auto", "rivian", "tesla"]
