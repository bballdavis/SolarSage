"""Which car is on the Wall Connector: the Wall Connector's vehicle-connected sensor, each
car's own plug and home state, and the override select."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.solar_ev_planner.inputs import CarSignals, identify_car

from .conftest import (
    RIV,
    RIVIAN,
    TES,
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

RIV_ON = CarSignals("rivian", True, True)
TES_ON = CarSignals("tesla", True, True)
TES_OFF = CarSignals("tesla", False, True)


def test_one_car_plugged_in_at_home_is_on_the_charger() -> None:
    found = identify_car([RIV_ON, TES_OFF], True)
    assert found.car_id == "rivian" and "Wall Connector sees a vehicle" in found.reason


def test_the_wall_connector_seeing_no_vehicle_rules_every_car_out() -> None:
    assert identify_car([RIV_ON, TES_OFF], False).car_id is None


def test_an_unknown_wall_connector_leaves_it_to_the_cars() -> None:
    assert identify_car([RIV_ON, TES_OFF], None).car_id == "rivian"


def test_a_car_plugged_in_away_or_unknown_is_not_on_the_charger() -> None:
    assert identify_car([CarSignals("rivian", True, False), TES_OFF], True).car_id is None
    assert identify_car([CarSignals("rivian", True, None), TES_OFF], True).car_id is None
    assert identify_car([CarSignals("rivian", None, True), TES_OFF], True).car_id is None


def test_two_cars_are_ambiguous_until_the_override_names_one() -> None:
    both = identify_car([RIV_ON, TES_ON], True)
    assert both.car_id is None and "override" in both.reason
    assert both.candidates == ("rivian", "tesla")
    assert identify_car([RIV_ON, TES_ON], True, "tesla").car_id == "tesla"


def test_the_override_never_picks_a_car_that_is_not_plugged_in() -> None:
    found = identify_car([RIV_ON, TES_OFF], True, "tesla")
    assert found.car_id is None and "not plugged in" in found.reason


async def test_override_select_and_commands(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    set_tesla(hass, plugged="on", charging=False)  # both cars say plugged in at home
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    await turn(hass, "switch.solar_ev_planner_shadow_mode", False)
    sensor = hass.states.get("sensor.solar_ev_planner_car_on_charger")
    assert sensor.state == "none" and "More than one car" in sensor.attributes["why"]
    assert coordinator.data.mode == "empty"
    assert fake_cars.calls == []

    select = "select.solar_ev_planner_charger_car_override"
    assert hass.states.get(select).attributes["options"] == ["auto", "rivian", "tesla"]
    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": "tesla"}, blocking=True
    )
    await cycle(hass, coordinator, freezer)
    assert hass.states.get("sensor.solar_ev_planner_car_on_charger").state == "tesla"
    tesla_calls = [c for c in fake_cars.calls if c[2] not in (RIV["switch"],)]
    assert tesla_calls and all(c[2] in (TES["switch"],) or c[1] == "set_value" for c in tesla_calls)
    assert hass.states.get(TES["switch"]).state == "on"
    assert hass.states.get(RIV["switch"]).state == "off"  # the Rivian was never commanded


async def test_the_wall_connector_off_ends_the_session(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    assert coordinator.runtime["rivian"].plugged_since is not None
    hass.states.async_set(E["wall_connected"], "off")
    await cycle(hass, coordinator, freezer)
    assert coordinator.data.identification.car_id is None
    assert coordinator.runtime["rivian"].plugged_since is None


async def test_unknown_readings_keep_the_session(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    """A car integration that is still starting does not end the plug-in session."""
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN), fake_cars)
    await turn(hass, "switch.solar_ev_planner_shadow_mode", False)
    since = coordinator.runtime["rivian"].plugged_since
    assert hass.states.get("binary_sensor.r1s_planner_charging_wanted").state == "on"
    hass.states.async_set(RIV["plugged"], "unavailable")
    await cycle(hass, coordinator, freezer)
    assert coordinator.data.identification.car_id is None
    assert coordinator.runtime["rivian"].plugged_since == since
    assert hass.states.get("binary_sensor.r1s_planner_charging_wanted").state == "on"
    set_site(hass)
    set_rivian(hass, desired=True, requested=coordinator.runtime["rivian"].commanded_a)
    await cycle(hass, coordinator, freezer)
    assert coordinator.runtime["rivian"].plugged_since == since
    # unplugged for real: the session ends and the planner has no decision for the car
    set_rivian(hass, plugged="off")
    await cycle(hass, coordinator, freezer)
    assert coordinator.runtime["rivian"].plugged_since is None
    assert hass.states.get("binary_sensor.r1s_planner_charging_wanted").state == "unknown"
