"""Fixtures for the planner integration's tests.

Everything is made up: the owner's entity IDs (``core.entities``) with synthetic values, a
recorder with imported statistics, a Schedule helper for the free hours, and fake services
standing in for Tesla Fleet and the Rivian charge adapter. Nothing talks to a real system.

The Home Assistant tests request ``planner_hass`` (recorder plus custom integrations); the
core's unit tests in ``core/`` need neither.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.solar_ev_planner.const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_CAPACITY_KWH,
    CONF_CAR_ID,
    CONF_CONTROL_STATE,
    CONF_CURRENT,
    CONF_FORECAST_POLLED,
    CONF_FORECAST_TODAY,
    CONF_FORECAST_TOMORROW,
    CONF_FREE_HOURS,
    CONF_GRID_POWER,
    CONF_KIND,
    CONF_LIMIT,
    CONF_LOCATION,
    CONF_MAX_A,
    CONF_MIN_A,
    CONF_NAME,
    CONF_PLUGGED,
    CONF_PV_POWER,
    CONF_SOC,
    CONF_STEP_A,
    CONF_SWITCH,
    CONF_WALL_CONNECTED,
    CONF_WALL_CONTACTOR,
    CONF_WALL_CURRENT,
    CONF_WALL_POWER,
    DEFAULT_SITE_OPTIONS,
    DOMAIN,
    ENTRY_MINOR_VERSION,
    ENTRY_TITLE,
    SUBENTRY_CAR,
)
from custom_components.solar_ev_planner.coordinator import PlannerCoordinator
from custom_components.solar_ev_planner.core.entities import CAR_ENTITIES, SITE_ENTITIES

# The test plugin sets SQLAlchemy's engine log to INFO; the recorder's thread then prints
# every statement once a test's log capture has ended. Errors still show.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

E = SITE_ENTITIES
RIV = CAR_ENTITIES["rivian"]
TES = CAR_ENTITIES["tesla"]
FREE_HOURS = "schedule.free_hours"

# 12:00 on Monday 15 June 2026 in the test instance's zone (US/Pacific, UTC-7)
NOON_UTC = datetime(2026, 6, 15, 19, 0, tzinfo=dt_util.UTC)

OPTIONS: dict[str, Any] = DEFAULT_SITE_OPTIONS | {
    CONF_PV_POWER: E["pv_power"],
    CONF_GRID_POWER: E["grid_power"],
    CONF_BATTERY_POWER: E["battery_power"],
    CONF_BATTERY_SOC: E["battery_soc"],
    CONF_BATTERY_CAPACITY: E["battery_capacity"],
    CONF_WALL_POWER: E["wall_power"],
    CONF_WALL_CURRENT: E["wall_current"],
    CONF_WALL_CONNECTED: E["wall_connected"],
    CONF_WALL_CONTACTOR: E["wall_contactor"],
    CONF_FORECAST_TODAY: E["forecast_today"],
    CONF_FORECAST_TOMORROW: E["forecast_tomorrow"],
    CONF_FORECAST_POLLED: E["forecast_last_polled"],
    CONF_FREE_HOURS: FREE_HOURS,
}

RIVIAN: dict[str, Any] = {
    CONF_CAR_ID: "rivian",
    CONF_KIND: "rivian",
    CONF_NAME: "R1S",
    CONF_SWITCH: RIV["switch"],
    CONF_CURRENT: RIV["current"],
    CONF_SOC: RIV["soc"],
    CONF_LIMIT: RIV["limit"],
    CONF_PLUGGED: RIV["plugged"],
    CONF_LOCATION: RIV["location"],
    CONF_CONTROL_STATE: RIV["control_state"],
    CONF_MIN_A: 8,
    CONF_MAX_A: 48,
    CONF_STEP_A: 2,
    CONF_CAPACITY_KWH: 135.0,
}
TESLA: dict[str, Any] = {
    CONF_CAR_ID: "tesla",
    CONF_KIND: "tesla",
    CONF_NAME: "Roxxo",
    CONF_SWITCH: TES["switch"],
    CONF_CURRENT: TES["current"],
    CONF_SOC: TES["soc"],
    CONF_LIMIT: TES["limit"],
    CONF_PLUGGED: TES["plugged"],
    CONF_LOCATION: TES["location"],
    CONF_MIN_A: 6,
    CONF_MAX_A: 48,
    CONF_STEP_A: 1,
    CONF_CAPACITY_KWH: 75.0,
}


def make_entry(
    *cars: dict[str, Any],
    options: dict[str, Any] | None = None,
    minor_version: int = ENTRY_MINOR_VERSION,
) -> MockConfigEntry:
    """The planner's entry with the given cars as subentries."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=ENTRY_TITLE,
        unique_id=DOMAIN,
        minor_version=minor_version,
        options=options or OPTIONS,
        subentries_data=[
            {
                "data": car,
                "subentry_type": SUBENTRY_CAR,
                "title": car[CONF_NAME],
                "unique_id": car[CONF_CAR_ID],
            }
            for car in cars
        ],
    )


# --------------------------------------------------------------------------- a synthetic site


def clear_sky_kw(h: float, peak: float = 11.0) -> float:
    if not 6.5 < h < 20.5:
        return 0.0
    return peak * math.sin(math.pi * (h - 6.5) / 14.0) ** 1.6


def house_kw(h: float, weekend: bool = False) -> float:
    kw = 0.6 + 1.6 * math.exp(-((h - 18.5) ** 2) / 3) + 0.8 * math.exp(-((h - 7.5) ** 2))
    return kw * (1.15 if weekend else 1.0)


def detailed_forecast(day: date, ratio: float = 1.0) -> dict[str, Any]:
    """A Solcast sensor's attributes: 48 half hours with p10 / p50 / p90 in kW."""
    tz = dt_util.get_default_time_zone()
    rows = []
    for j in range(48):
        p50 = clear_sky_kw(j / 2 + 0.25) * ratio
        rows.append(
            {
                "period_start": datetime(
                    day.year, day.month, day.day, j // 2, 30 * (j % 2), tzinfo=tz
                ),
                "pv_estimate": round(p50, 4),
                "pv_estimate10": round(p50 * 0.75, 4),
                "pv_estimate90": round(p50 * 1.1, 4),
            }
        )
    return {"detailedForecast": rows}


def set_site(
    hass: HomeAssistant,
    *,
    pv_w: float = 7800,
    grid_w: float = -150,
    battery_w: float = 2900,
    soc: float = 72,
    wall_kw: float = 0.0,
    wall_connected: str = "on",
    forecast_ratio: float = 1.0,
) -> None:
    """The Sol-Ark, the Wall Connector and Solcast, as their integrations show them."""
    now = dt_util.now()
    today = now.date()
    w = {"unit_of_measurement": "W", "device_class": "power"}
    hass.states.async_set(E["pv_power"], str(pv_w), w)
    hass.states.async_set(E["grid_power"], str(grid_w), w)
    hass.states.async_set(E["battery_power"], str(battery_w), w)
    hass.states.async_set(E["battery_soc"], str(soc), {"unit_of_measurement": "%"})
    hass.states.async_set(E["battery_capacity"], "31.2", {"unit_of_measurement": "kWh"})
    hass.states.async_set(E["wall_power"], str(wall_kw), {"unit_of_measurement": "kW"})
    hass.states.async_set(E["wall_current"], str(round(wall_kw * 1000 / 240)), {})
    hass.states.async_set(E["wall_connected"], wall_connected)
    hass.states.async_set(E["wall_contactor"], "on" if wall_kw > 0 else "off")
    hass.states.async_set(E["forecast_today"], "52.1", detailed_forecast(today, forecast_ratio))
    hass.states.async_set(
        E["forecast_tomorrow"],
        "50.3",
        detailed_forecast(today + timedelta(days=1), forecast_ratio),
    )
    hass.states.async_set(E["forecast_last_polled"], (now - timedelta(minutes=40)).isoformat())


def set_rivian(
    hass: HomeAssistant,
    *,
    plugged: str = "on",
    home: str = "home",
    soc: float = 46,
    limit: float = 85,
    adapter_state: str = "idle",
    desired: bool | None = None,
    requested: float = 0,
) -> None:
    """The Rivian's telemetry (upstream integration) and the charge adapter's entities."""
    hass.states.async_set(RIV["soc"], str(soc), {"unit_of_measurement": "%"})
    hass.states.async_set(RIV["limit"], str(limit), {"unit_of_measurement": "%"})
    hass.states.async_set(RIV["plugged"], plugged)
    # a tracker's coordinates are never read; they must never reach the diagnostics
    hass.states.async_set(RIV["location"], home, {"latitude": 32.87336, "longitude": -117.22743})
    hass.states.async_set(RIV["switch"], "on" if desired else "off")
    hass.states.async_set(RIV["current"], str(requested), {"min": 0, "max": 80, "step": 1})
    hass.states.async_set(
        RIV["control_state"],
        adapter_state,
        {"desired_charge": desired, "requested_amps": requested, "lease_amps": None},
    )


def set_tesla(
    hass: HomeAssistant,
    *,
    plugged: str = "off",
    home: str = "home",
    soc: float = 63,
    limit: float = 90,
    charging: bool = False,
    amps: float = 32,
) -> None:
    """The Tesla as Tesla Fleet shows it (the car is named Roxxo)."""
    hass.states.async_set(TES["soc"], str(soc), {"unit_of_measurement": "%"})
    hass.states.async_set(TES["limit"], str(limit), {"min": 50, "max": 100})
    hass.states.async_set(TES["plugged"], plugged)
    hass.states.async_set(TES["location"], home, {"latitude": 32.87336, "longitude": -117.22743})
    hass.states.async_set(TES["switch"], "on" if charging else "off")
    hass.states.async_set(TES["current"], str(amps), {"min": 0, "max": 48, "step": 1})


class FakeCars:
    """Stand-ins for Tesla Fleet's and the Rivian adapter's services.

    ``install`` (after the planner has loaded the switch and number platforms) takes over
    ``switch.turn_on``, ``switch.turn_off`` and ``number.set_value``. A call for a real entity
    (the planner's own settings) goes to that entity. A call for a car's entity (a plain
    state here) is recorded, and the state updated as the real integration would: the switch
    turns on or off, the number shows the value; the adapter's control state also shows the
    request (a current set while no request stands starts charging, as the adapter does).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple[str, str, Any]] = []
        self.fail: str | None = None

    def install(self) -> None:
        for domain, service in (("switch", "turn_on"), ("switch", "turn_off")):
            self.hass.services.async_register(domain, service, self._handle)
        self.hass.services.async_register("number", "set_value", self._handle)

    def clear(self) -> None:
        self.calls.clear()

    async def _handle(self, call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        value = call.data.get("value")
        component = self.hass.data.get(DATA_INSTANCES, {}).get(call.domain)
        entity = component.get_entity(entity_id) if component else None
        if entity is not None:
            if call.service == "turn_on":
                await entity.async_turn_on()
            elif call.service == "turn_off":
                await entity.async_turn_off()
            else:
                await entity.async_set_native_value(float(value))
            return
        self.calls.append((call.domain, call.service, value if value is not None else entity_id))
        if self.fail:
            raise HomeAssistantError(self.fail)
        state = self.hass.states.get(entity_id)
        attrs = dict(state.attributes) if state else {}
        if call.domain == "switch":
            on = call.service == "turn_on"
            self.hass.states.async_set(entity_id, "on" if on else "off", attrs)
            self._adapter(entity_id, desired=on)
        else:
            self.hass.states.async_set(entity_id, str(value), attrs)
            self._adapter(entity_id, amps=value)

    def _adapter(self, entity_id: str, desired: bool | None = None, amps: Any = None) -> None:
        if entity_id not in (RIV["switch"], RIV["current"]):
            return
        state = self.hass.states.get(RIV["control_state"])
        attrs = dict(state.attributes) if state else {}
        if desired is not None:
            attrs["desired_charge"] = desired
        if amps is not None:
            attrs["requested_amps"] = amps
            if attrs.get("desired_charge") is None:
                attrs["desired_charge"] = True
        on = attrs.get("desired_charge")
        self.hass.states.async_set(RIV["control_state"], "lease_active" if on else "paused", attrs)
        self.hass.states.async_set(RIV["switch"], "on" if on else "off")


@pytest.fixture
def planner_hass(
    recorder_mock: Recorder, enable_custom_integrations: None, hass: HomeAssistant
) -> HomeAssistant:
    """Home Assistant with the recorder and this repository's integrations."""
    return hass


@pytest.fixture
def fake_cars(planner_hass: HomeAssistant) -> FakeCars:
    return FakeCars(planner_hass)


async def setup_free_hours(hass: HomeAssistant, start: str = "20:00", end: str = "06:00") -> None:
    """The owner's free hours as a Schedule helper (every day, start to end)."""
    days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    blocks = [{"from": "00:00:00", "to": f"{end}:00"}, {"from": f"{start}:00", "to": "24:00:00"}]
    assert await async_setup_component(
        hass,
        "schedule",
        {"schedule": {"free_hours": {"name": "Free hours"} | dict.fromkeys(days, blocks)}},
    )
    await hass.async_block_till_done()


async def remove_free_hours(hass: HomeAssistant) -> None:
    """Remove the Schedule helper, whose own timer would otherwise outlive the test."""
    component = hass.data.get(DATA_INSTANCES, {}).get("schedule")
    if component is not None and hass.states.get(FREE_HOURS) is not None:
        await component.async_remove_entity(FREE_HOURS)


async def import_statistics(hass: HomeAssistant, days: int = 14) -> None:
    """Hourly long-term statistics of the four power sensors for the last ``days`` days."""
    now = dt_util.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    rows: dict[str, list[dict[str, Any]]] = {
        E[k]: [] for k in ("pv_power", "grid_power", "battery_power", "wall_power")
    }
    t = start
    while t < now.replace(minute=0, second=0, microsecond=0):
        h = t.hour + 0.5
        pv = clear_sky_kw(h) * (0.8 if t.day % 3 == 0 else 1.0) * 1000.0
        house = house_kw(h, t.weekday() >= 5) * 1000.0
        battery = max(-9000.0, min(9000.0, pv - house))
        for key, value in (
            (E["pv_power"], pv),
            (E["grid_power"], house - pv + battery),
            (E["battery_power"], battery),
            (E["wall_power"], 0.0),
        ):
            rows[key].append(
                {"start": dt_util.as_utc(t), "mean": value, "min": value, "max": value}
            )
        t += timedelta(hours=1)
    for statistic_id, data in rows.items():
        unit = "kW" if statistic_id == E["wall_power"] else "W"
        async_import_statistics(
            hass,
            {
                "has_sum": False,
                "mean_type": StatisticMeanType.ARITHMETIC,
                "name": None,
                "source": "recorder",
                "statistic_id": statistic_id,
                "unit_class": "power",
                "unit_of_measurement": unit,
            },
            data,
        )
    await async_wait_recording_done(hass)


async def setup_entry(
    hass: HomeAssistant, entry: MockConfigEntry, cars: FakeCars | None = None
) -> PlannerCoordinator:
    """Set the planner up (in shadow mode, its default), then install the fake cars."""
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    if cars is not None:
        cars.install()
    return entry.runtime_data


async def turn(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    """Switch one of the planner's settings through its service."""
    await hass.services.async_call(
        "switch", "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True
    )
    # a setting plans again at once, or after the coordinator's 10-second debounce when one
    # changed just before: refresh now
    await hass.config_entries.async_entries(DOMAIN)[0].runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture
async def site(planner_hass: HomeAssistant, freezer: Any) -> AsyncGenerator[HomeAssistant]:
    """Noon on a sunny Monday: the site, both cars (the Rivian plugged in), the free hours
    and 14 days of statistics."""
    freezer.move_to(NOON_UTC)
    hass = planner_hass
    await import_statistics(hass)
    set_site(hass)
    set_rivian(hass)
    set_tesla(hass)
    await setup_free_hours(hass)
    yield hass
    await remove_free_hours(hass)


async def cycle(
    hass: HomeAssistant, coordinator: PlannerCoordinator, freezer: Any, minutes: float = 1
) -> None:
    """Let ``minutes`` pass and run one planning cycle."""
    freezer.tick(timedelta(minutes=minutes))
    for entity_id in (E["pv_power"], E["grid_power"], E["battery_power"], E["battery_soc"]):
        state = hass.states.get(entity_id)
        if state is not None:  # the Sol-Ark reports every few seconds
            hass.states.async_set(entity_id, state.state, state.attributes, force_update=True)
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


def entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    """An entity's ID from its unique ID."""
    from homeassistant.helpers import entity_registry as er

    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert found is not None, unique_id
    return found
