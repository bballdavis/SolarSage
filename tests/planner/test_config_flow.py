"""The config flow: the site in three steps prefilled with the owner's entities, the options,
and the car and price-window subentries."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_ev_planner.const import (
    CONF_CAR_ID,
    DEFAULT_SITE_OPTIONS,
    DOMAIN,
    SUBENTRY_CAR,
    SUBENTRY_PRICE,
)
from custom_components.solar_ev_planner.core.entities import (
    CAR_ENTITY_PREFERRED,
    TESLA_CUSTOM_ENTITIES,
)

from .conftest import OPTIONS, RIV, RIVIAN, E, set_rivian, set_site, set_tesla

SITE_INPUT = {
    "pv_power": E["pv_power"],
    "pv_power_unit": "W",
    "pv_power_invert": False,
    "grid_power": E["grid_power"],
    "grid_power_unit": "W",
    "grid_power_invert": False,
    "battery_power": E["battery_power"],
    "battery_power_unit": "W",
    "battery_power_invert": False,
    "battery_soc": E["battery_soc"],
    "battery_capacity": E["battery_capacity"],
    "battery_capacity_kwh": 31,
    "battery_max_charge_kw": 9,
    "battery_max_discharge_kw": 9,
    "battery_reserve_pct": 10,
}
CHARGER_INPUT = {
    "wall_power": E["wall_power"],
    "wall_power_unit": "kW",
    "wall_current": E["wall_current"],
    "wall_connected": E["wall_connected"],
    "wall_contactor": E["wall_contactor"],
    "charger_max_a": 48,
    "volts": 240,
}
TARIFF_INPUT = {
    "forecast_today": E["forecast_today"],
    "forecast_tomorrow": E["forecast_tomorrow"],
    "forecast_last_polled": E["forecast_last_polled"],
    "free_hours": "schedule.free_hours",
}
CAR_NUMBERS = {"min_a": 8, "max_a": 48, "step_a": 2, "capacity_kwh": 135}


@pytest.fixture(autouse=True)
def _ha(planner_hass: HomeAssistant) -> Generator[None]:
    """The flows only; setting the entry up is tested elsewhere."""
    with patch("custom_components.solar_ev_planner.async_setup_entry", return_value=True):
        yield


def suggested(result: dict[str, Any], key: str) -> Any:
    for marker in result["data_schema"].schema:
        if marker == key:
            return (marker.description or {}).get("suggested_value")
    raise KeyError(key)


async def start_site(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "site"}
    )


async def test_site_flow_suggests_the_owners_entities(planner_hass: HomeAssistant) -> None:
    hass = planner_hass
    set_site(hass)
    hass.states.async_set("schedule.free_hours", "off")
    result = await start_site(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "site"
    assert suggested(result, "pv_power") == "sensor.sol_ark_inverter_pv_power"
    assert suggested(result, "grid_power") == "sensor.sol_ark_inverter_grid_power"
    assert suggested(result, "battery_soc") == "sensor.sol_ark_inverter_battery_state_of_charge"
    assert suggested(result, "battery_capacity") == "sensor.sol_ark_battery_capacity_estimate"
    assert suggested(result, "pv_power_unit") == "W"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SITE_INPUT)
    assert result["step_id"] == "charger"
    assert suggested(result, "wall_power") == "sensor.tesla_wall_connector_total_power"
    assert suggested(result, "wall_power_unit") == "kW"
    assert suggested(result, "wall_current") == E["wall_current"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CHARGER_INPUT)
    assert result["step_id"] == "tariff"
    assert suggested(result, "forecast_today") == "sensor.solcast_pv_forecast_forecast_today"
    assert suggested(result, "free_hours") == "schedule.free_hours"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], TARIFF_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "SolarSage"
    options = result["options"]
    assert options["pv_power"] == E["pv_power"] and options["wall_power_unit"] == "kW"
    assert options["free_hours"] == "schedule.free_hours"
    assert options["battery_max_charge_kw"] == 9


async def test_missing_entities_are_not_suggested(planner_hass: HomeAssistant) -> None:
    result = await start_site(planner_hass)
    assert suggested(result, "pv_power") is None
    assert suggested(result, "battery_capacity_kwh") == 31.0


async def test_only_one_planner(planner_hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=OPTIONS).add_to_hass(planner_hass)
    result = await start_site(planner_hass)
    assert result["type"] is FlowResultType.ABORT


async def test_the_same_forecast_twice_is_refused(planner_hass: HomeAssistant) -> None:
    hass = planner_hass
    result = await start_site(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SITE_INPUT)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CHARGER_INPUT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], TARIFF_INPUT | {"forecast_tomorrow": E["forecast_today"]}
    )
    assert result["errors"] == {"forecast_tomorrow": "same_forecast"}


async def test_options_change_signs_and_drop_optional_entities(
    planner_hass: HomeAssistant,
) -> None:
    hass = planner_hass
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=OPTIONS)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert suggested(result, "grid_power") == E["grid_power"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], SITE_INPUT | {"grid_power_invert": True}
    )
    charger = {k: v for k, v in CHARGER_INPUT.items() if k != "wall_contactor"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], charger)
    result = await hass.config_entries.options.async_configure(result["flow_id"], TARIFF_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["grid_power_invert"] is True
    assert "wall_contactor" not in entry.options
    assert entry.options["wall_connected"] == E["wall_connected"]


# --------------------------------------------------------------------------- cars


async def _add_car(hass: HomeAssistant, entry: MockConfigEntry, kind: str) -> dict[str, Any]:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_CAR), context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    return await hass.config_entries.subentries.async_configure(result["flow_id"], {"kind": kind})


def _car_input(result: dict[str, Any]) -> dict[str, Any]:
    """The suggested values, as the owner would accept them."""
    out = {}
    for marker in result["data_schema"].schema:
        value = (marker.description or {}).get("suggested_value")
        if value is not None:
            out[str(marker)] = value
    return out


async def test_add_cars_with_the_owners_entities(planner_hass: HomeAssistant) -> None:
    hass = planner_hass
    set_rivian(hass)
    # the Tesla on Tesla Fleet, with its charging sensor; leftover tesla_custom entities are
    # never suggested
    set_tesla(hass)
    hass.states.async_set("sensor.tesla_charging", "stopped")
    hass.states.async_set("binary_sensor.tesla_status", "off")
    hass.states.async_set(TESLA_CUSTOM_ENTITIES["switch"], "off")
    hass.states.async_set(TESLA_CUSTOM_ENTITIES["current"], "32")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=OPTIONS)
    entry.add_to_hass(hass)

    result = await _add_car(hass, entry, "rivian")
    assert result["step_id"] == "car"
    data = _car_input(result)
    assert data["switch"] == "switch.r1s_schedule_control_solar_charging"
    assert data["current"] == "number.r1s_schedule_control_solar_current"
    assert data["control_state"] == "sensor.r1s_schedule_control_control_state"
    assert data["plugged"] == "binary_sensor.r1s_charger_connection"
    assert data["location"] == "device_tracker.r1s_location"
    assert "charging_state" not in data and "status" not in data  # a Tesla's only
    assert (data["min_a"], data["max_a"], data["step_a"]) == (8, 48, 2)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await _add_car(hass, entry, "tesla")
    data = _car_input(result)
    assert data["switch"] == "switch.tesla_charge"
    assert data["current"] == "number.tesla_charge_current"
    assert data["limit"] == "number.tesla_charge_limit"
    assert data["charging_state"] == "sensor.tesla_charging"
    assert data["status"] == "binary_sensor.tesla_status"  # the spending guard's wakes
    assert data["soc"] == "sensor.tesla_battery_level"
    assert "control_state" not in data
    assert (data["min_a"], data["step_a"]) == (6, 1)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    cars = {s.data[CONF_CAR_ID]: s for s in entry.subentries.values()}
    assert set(cars) == {"rivian", "tesla"}
    assert cars["rivian"].title == "R1S" and cars["tesla"].title == "Tesla"
    assert cars["tesla"].data["kind"] == "tesla"
    assert cars["tesla"].data["charging_state"] == "sensor.tesla_charging"
    assert cars["tesla"].data["status"] == "binary_sensor.tesla_status"

    # a second Rivian gets its own id
    result = await _add_car(hass, entry, "rivian")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _car_input(result) | {"name": "R1T"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {s.data[CONF_CAR_ID] for s in entry.subentries.values()} == {
        "rivian",
        "tesla",
        "rivian_2",
    }


async def test_tesla_custom_is_never_suggested(planner_hass: HomeAssistant) -> None:
    """A Tesla only on tesla_custom gets no switch or current suggested: the owner maps
    Tesla Fleet's (tesla_custom cannot command the car without its proxy)."""
    hass = planner_hass
    set_tesla(hass)
    hass.states.async_remove("switch.tesla_charge")
    hass.states.async_remove("number.tesla_charge_current")
    hass.states.async_set(TESLA_CUSTOM_ENTITIES["switch"], "off")
    hass.states.async_set(TESLA_CUSTOM_ENTITIES["current"], "32")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=OPTIONS)
    entry.add_to_hass(hass)
    data = _car_input(await _add_car(hass, entry, "tesla"))
    assert "switch" not in data and "current" not in data
    assert "charging_state" not in data  # optional, and absent here
    assert data["limit"] == "number.tesla_charge_limit"


async def test_teslamate_readings_are_suggested_over_tesla_fleets(
    planner_hass: HomeAssistant,
) -> None:
    """With TeslaMate bridged in, its free readings are suggested; the switch and current
    stay Tesla Fleet's."""
    hass = planner_hass
    set_tesla(hass)
    preferred = CAR_ENTITY_PREFERRED["tesla"]
    for eid in preferred.values():
        hass.states.async_set(eid, "on")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=OPTIONS)
    entry.add_to_hass(hass)
    data = _car_input(await _add_car(hass, entry, "tesla"))
    assert {key: data.get(key) for key in preferred} == preferred
    assert data["switch"] == "switch.tesla_charge"
    assert data["current"] == "number.tesla_charge_current"


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        (("device_tracker.r1s",), "device_tracker.r1s"),  # a fresh upstream 2.0.0 install
        (("device_tracker.r1s", "device_tracker.r1s_location"), "device_tracker.r1s_location"),
        ((), None),
    ],
)
async def test_rivian_location_suggests_whichever_tracker_exists(
    planner_hass: HomeAssistant, present: tuple[str, ...], expected: str | None
) -> None:
    hass = planner_hass
    set_rivian(hass)
    hass.states.async_remove(RIV["location"])
    for eid in present:
        hass.states.async_set(eid, "home")
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=OPTIONS)
    entry.add_to_hass(hass)
    data = _car_input(await _add_car(hass, entry, "rivian"))
    assert data.get("location") == expected


async def test_car_errors_and_reconfigure(planner_hass: HomeAssistant) -> None:
    hass = planner_hass
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options=OPTIONS,
        subentries_data=[
            {"data": RIVIAN, "subentry_type": SUBENTRY_CAR, "title": "R1S", "unique_id": "rivian"}
        ],
    )
    entry.add_to_hass(hass)
    result = await _add_car(hass, entry, "tesla")
    bad = {
        "name": "Tesla",
        "switch": "switch.tesla_charge",
        "current": "number.tesla_charge_current",
        "soc": "sensor.tesla_battery_level",
        "limit": "number.tesla_charge_limit",
        "plugged": "binary_sensor.tesla_charge_cable",
        "min_a": 20,
        "max_a": 10,
        "step_a": 1,
        "capacity_kwh": 75,
    }
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], bad)
    assert result["errors"] == {"max_a": "max_below_min"}
    own = bad | {"max_a": 48, "switch": "switch.solarsage_planning"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], own)
    assert result["errors"] == {"base": "own_entity"}

    subentry_id = next(iter(entry.subentries))
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_CAR),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    assert suggested(result, "switch") == RIV["switch"]
    # the control state cleared (an emptied field is left out)
    data = {k: v for k, v in RIVIAN.items() if k not in ("car_id", "kind", "control_state")}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], data | {"max_a": 40}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    sub = entry.subentries[subentry_id]
    assert sub.data["max_a"] == 40 and sub.data["car_id"] == "rivian"
    assert "control_state" not in sub.data


# --------------------------------------------------------------------------- price windows


async def test_price_window(planner_hass: HomeAssistant) -> None:
    hass = planner_hass
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=OPTIONS)
    entry.add_to_hass(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_PRICE), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"schedule": "schedule.peak", "label": "Peak", "price": 0.45}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    sub = next(iter(entry.subentries.values()))
    assert sub.title == "Peak" and sub.data == {
        "schedule": "schedule.peak",
        "label": "Peak",
        "price": 0.45,
    }
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_PRICE),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": sub.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"schedule": "schedule.peak", "label": "Peak", "price": 0.5, "export_price": 0.05},
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[sub.subentry_id].data["export_price"] == 0.05


def test_default_options_cover_every_power() -> None:
    for key in ("pv_power", "grid_power", "battery_power", "wall_power"):
        assert f"{key}_unit" in DEFAULT_SITE_OPTIONS and f"{key}_invert" in DEFAULT_SITE_OPTIONS
