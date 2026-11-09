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
    "switch.solarsage_planning",
    "switch.solarsage_shadow_mode",
    "switch.solarsage_daytime_solar_charging",
    "select.solarsage_caution",
    "select.solarsage_night_charging_start",
    "number.solarsage_battery_floor",
    "number.solarsage_battery_wanted_at_free_hours",
    "number.solarsage_minimum_time_between_changes",
    "number.solarsage_largest_change",
    "number.solarsage_paid_grid_tolerance",
    "number.solarsage_import_price",
    "number.solarsage_solar_buyback",
    "select.solarsage_charger_car_override",
    "sensor.solarsage_mode",
    "sensor.solarsage_reason",
    "sensor.solarsage_next_change",
    "sensor.solarsage_battery_minimum",
    "sensor.solarsage_battery_minimum_time",
    "sensor.solarsage_forecast_correction",
    "sensor.solarsage_car_on_charger",
    "sensor.solarsage_heartbeat",
}
TESLA_SPEND_ENTITIES = {
    "sensor.solarsage_tesla_api_spend",
    "number.solarsage_tesla_api_budget",
    "number.solarsage_tesla_fleet_polling_estimate",
    "number.solarsage_tesla_refresh_interval",
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
    for car in ("r1s", "tesla"):
        expected |= {e.format(car=car) for e in CAR_ENTITIES}
    expected.add("number.tesla_planner_restore_current_at_unplug")  # the Tesla only
    expected.add("sensor.r1s_planner_charging_until")  # the Rivian only
    expected |= TESLA_SPEND_ENTITIES  # with a Tesla only
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
    assert (core.min_change_min, core.max_step_a, core.paid_tolerance_min) == (15.0, 6, 5.0)
    assert core.wanted_at_free_start_pct == 30.0

    async def call(domain: str, service: str, entity_id: str, **data: object) -> None:
        await hass.services.async_call(
            domain, service, {"entity_id": entity_id, **data}, blocking=True
        )

    await call("select", "select_option", "select.solarsage_caution", option="near_certain")
    await call("number", "set_value", "number.solarsage_battery_floor", value=25)
    await call("number", "set_value", "number.solarsage_battery_wanted_at_free_hours", value=50)
    await call("number", "set_value", "number.solarsage_import_price", value=0.4)
    core = coordinator.core_settings()
    assert core.caution == "near-certain" and core.caution_p() == 0.97
    assert core.floor_pct == 25 and core.wanted_at_free_start_pct == 50
    assert hass.states.get("select.solarsage_caution").state == "near_certain"
    price = hass.states.get("number.solarsage_import_price")
    assert price.state == "0.4"
    assert price.attributes["unit_of_measurement"] == f"{hass.config.currency}/kWh"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    core = entry.runtime_data.core_settings()
    assert core.caution == "near-certain" and core.floor_pct == 25
    assert core.wanted_at_free_start_pct == 50
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
        {"entity_id": "select.tesla_planner_plan_days", "option": "weekdays"},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.tesla_planner_plan_target", "value": 70},
        blocking=True,
    )
    await cycle(hass, coordinator, freezer)
    plans = coordinator._plans("tesla", dt_util.now())
    tomorrow = dt_util.now().date() + timedelta(days=1)
    assert plans[0].target_pct == 70
    assert plans[0].deadline == datetime.combine(tomorrow, time(7, 30), dt_util.now().tzinfo)
    outlook = hass.states.get("sensor.tesla_planner_outlook")
    assert outlook.attributes["target"] == 70 and outlook.attributes["by"].startswith(
        tomorrow.isoformat()
    )
    # once: the next 13:00, then the plan turns itself off
    await hass.services.async_call(
        "time",
        "set_value",
        {"entity_id": "time.tesla_planner_plan_time", "time": "13:00:00"},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.tesla_planner_plan_days", "option": "once"},
        blocking=True,
    )
    await cycle(hass, coordinator, freezer)
    (plan,) = coordinator._plans("tesla", dt_util.now())
    assert plan.deadline.hour == 13 and plan.deadline.date() == dt_util.now().date()
    await cycle(hass, coordinator, freezer, minutes=60)
    assert hass.states.get("select.tesla_planner_plan_days").state == "off"
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
    state = hass.states.get("number.solarsage_peak_price")
    assert state is not None and state.state == "0.45"
    tariff, problem = coordinator._tariff()
    assert problem is None
    assert [w.label for w in tariff.windows] == ["free", "Peak"]
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.solarsage_peak_price", "value": 0.5},
        blocking=True,
    )
    tariff, _ = coordinator._tariff()
    assert tariff.windows[1].import_price == 0.5


async def test_adding_a_car_reloads_the_planner(site: HomeAssistant, fake_cars: FakeCars) -> None:
    hass = site
    entry = make_entry(RIVIAN)
    await setup_entry(hass, entry, fake_cars)
    assert hass.states.get("sensor.tesla_planner_planned_current") is None
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(data=TESLA, subentry_type=SUBENTRY_CAR, title="Tesla", unique_id="tesla"),
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.tesla_planner_planned_current") is not None
    assert set(entry.runtime_data.cars) == {"rivian", "tesla"}
    assert hass.states.get("select.solarsage_charger_car_override").attributes["options"] == [
        "auto",
        "rivian",
        "tesla",
    ]


async def test_rebranding_preserves_existing_registered_ids(site, fake_cars) -> None:
    """An existing installation must keep automations referencing pre-branding entities."""
    entry = make_entry(RIVIAN)
    entry.add_to_hass(site)
    registry = er.async_get(site)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_heartbeat",
        suggested_object_id="solar_ev_planner_heartbeat",
        config_entry=entry,
    )
    # setup_entry normally adds the entry; this one is already registered.
    assert await site.config_entries.async_setup(entry.entry_id)
    await site.async_block_till_done(wait_background_tasks=True)
    assert site.states.get("sensor.solar_ev_planner_heartbeat") is not None
    assert site.states.get("sensor.solarsage_heartbeat") is None
