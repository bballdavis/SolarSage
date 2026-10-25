"""Reading states (units, signs, freshness), the Schedule helper's config, the tariff, and the
history: recorder rows to the load profile, and the live power buffer."""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, time, timedelta

from homeassistant.core import State
from homeassistant.util import dt as dt_util
import pytest

from custom_components.solar_ev_planner.const import DEFAULT_SITE_OPTIONS
from custom_components.solar_ev_planner.history import PowerBuffer, build_history
from custom_components.solar_ev_planner.inputs import (
    WindowSpec,
    build_tariff,
    normalize_schedule,
    power_kw,
    state_float,
    state_home,
    state_on,
)

TZ = dt_util.get_time_zone("US/Pacific")
MONDAY = datetime(2026, 6, 15, 12, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _local_zone() -> Generator[None]:
    """Home Assistant's zone, as the recorder's rows are converted to it."""
    before = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(TZ)
    yield
    dt_util.set_default_time_zone(before)


def st(entity_id: str, value: str, **attrs: object) -> State:
    return State(entity_id, value, attrs)


def test_power_units_and_signs() -> None:
    assert power_kw(st("sensor.p", "7800", unit_of_measurement="W"), "kW", False) == 7.8
    assert power_kw(st("sensor.p", "3.8", unit_of_measurement="kW"), "W", False) == 3.8
    assert power_kw(st("sensor.p", "1500"), "W", False) == 1.5  # no unit: the configured one
    assert power_kw(st("sensor.p", "1.5"), "kW", True) == -1.5  # inverted sign
    assert power_kw(st("sensor.p", "unavailable"), "W", False) is None
    assert power_kw(None, "W", False) is None


def test_states() -> None:
    assert state_float(st("sensor.x", "nan")) is None
    assert state_float(st("sensor.x", "unknown")) is None
    assert state_on(st("binary_sensor.x", "off")) is False
    assert state_on(st("binary_sensor.x", "unavailable")) is None
    assert state_home(st("device_tracker.car", "home")) is True
    assert state_home(st("device_tracker.car", "work")) is False
    assert state_home(st("device_tracker.car", "unknown")) is None
    assert state_home(st("binary_sensor.car_home", "on")) is True


def test_schedule_config_from_the_service() -> None:
    """``schedule.get_schedule`` returns time objects, with time.max as the end of a day."""
    config = {
        "monday": [
            {"from": time(0, 0), "to": time(6, 0)},
            {"from": time(20, 0), "to": time.max, "data": {"note": "x"}},
        ],
        "tuesday": [{"from": "20:00:00", "to": "23:59:59.999999"}],
    }
    out = normalize_schedule(config)
    assert out["monday"] == [
        {"from": "00:00:00", "to": "06:00:00"},
        {"from": "20:00:00", "to": "24:00:00"},
    ]
    assert out["tuesday"] == [{"from": "20:00:00", "to": "24:00:00"}]
    assert out["sunday"] == []


def test_tariffs_free_nights_peak_and_flat() -> None:
    days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    free = {
        d: [{"from": "00:00:00", "to": "06:00:00"}, {"from": "20:00:00", "to": "24:00:00"}]
        for d in days
    }
    peak = {d: [{"from": "16:00:00", "to": "21:00:00"}] for d in days}
    tariff = build_tariff(0.32, 0.0, free, [WindowSpec(peak, 0.45, "peak", 0.1)])
    assert tariff.is_free(MONDAY.replace(hour=23)) and tariff.is_free(MONDAY.replace(hour=5))
    assert tariff.import_price(MONDAY) == 0.32
    assert tariff.import_price(MONDAY.replace(hour=17)) == 0.45
    assert tariff.export_price(MONDAY.replace(hour=17)) == 0.1
    assert tariff.import_price(MONDAY.replace(hour=20, minute=30)) == 0.0  # free hours win
    flat = build_tariff(0.32, 0.05, None)
    assert not flat.windows and flat.export_price(MONDAY) == 0.05


def _rows(start: datetime, hours: int, value: float, step: timedelta) -> list[dict]:
    return [
        {"start": (start + k * step).timestamp(), "mean": value}
        for k in range(int(hours * timedelta(hours=1) / step))
    ]


def test_history_from_recorder_rows() -> None:
    """Hourly rows in the recorder's units (W, kW from its metadata), the battery inverted."""
    now = MONDAY
    start = datetime(2026, 6, 1, 0, 0, tzinfo=TZ)
    hour = timedelta(hours=1)
    options = DEFAULT_SITE_OPTIONS | {
        "pv_power": "sensor.pv",
        "grid_power": "sensor.grid",
        "battery_power": "sensor.battery",
        "battery_power_invert": True,
        "wall_power": "sensor.wall",
    }
    hours = 14 * 24 + 12
    hourly = {
        "sensor.pv": _rows(start, hours, 2000.0, hour),
        "sensor.grid": _rows(start, hours, 0.0, hour),
        "sensor.battery": _rows(start, hours, 500.0, hour),  # discharging 0.5 kW, inverted
        "sensor.wall": _rows(start, hours, 0.0, hour),
    }
    five = timedelta(minutes=5)
    short = {k: _rows(now - timedelta(hours=24), 24, v[0]["mean"], five) for k, v in hourly.items()}
    metadata = {
        "sensor.pv": (1, {"unit_of_measurement": "W"}),
        "sensor.grid": (2, {"unit_of_measurement": "W"}),
        "sensor.battery": (3, {"unit_of_measurement": "W"}),
        "sensor.wall": (4, {"unit_of_measurement": "kW"}),
    }
    history = build_history(options, hourly, short, metadata, now.date(), now)
    assert history.profile.n_days == 14
    # house = pv + grid - battery - wall = 2 + 0 - (-0.5) - 0
    assert history.profile.kw_at(now) == pytest.approx(2.5)
    assert len(history.buckets) == 24 * 12
    assert history.buckets[max(history.buckets)][2] == pytest.approx(-0.5)
    assert history.hours and history.missing == ()


def test_history_without_wall_connector_statistics() -> None:
    now = MONDAY
    start = datetime(2026, 6, 1, tzinfo=TZ)
    options = DEFAULT_SITE_OPTIONS | {
        "pv_power": "sensor.pv",
        "grid_power": "sensor.grid",
        "battery_power": "sensor.battery",
        "wall_power": "sensor.wall",
    }
    hour = timedelta(hours=1)
    hourly = {k: _rows(start, 14 * 24, 1000.0, hour) for k in ("sensor.pv", "sensor.grid")}
    hourly["sensor.battery"] = _rows(start, 14 * 24, 0.0, hour)
    history = build_history(options, hourly, {}, {}, now.date(), now)
    assert history.missing == ("wall_power",)
    assert history.profile.n_days == 14
    assert history.profile.kw_at(now) == pytest.approx(2.0)


def test_power_buffer_means_and_todays_solar() -> None:
    buffer = PowerBuffer()
    t = MONDAY.replace(hour=11)
    for k in range(60):
        buffer.add(t + timedelta(minutes=k), (6.0, -1.0, 2.0, 0.0))
    now = t + timedelta(minutes=60)
    assert buffer.mean(now, 5) == (6.0, -1.0, 2.0, 0.0)
    recent = buffer.house_recent(now)
    assert len(recent) == 12 and recent[0].kw == pytest.approx(3.0)
    # only 11:00-12:00 is measured: the rest of the morning comes from the fallback
    assert buffer.pv_today_kwh(now) == pytest.approx(6.0)
    assert buffer.pv_today_kwh(now, lambda a, b: 1.0 * (b - a).total_seconds() / 3600) == (
        pytest.approx(6.0 + 11.0)
    )
    # today's hourly statistics fill the gap before the fallback does
    buffer.hours = {MONDAY.replace(hour=h): (2.0, 0.0, 0.0, 0.0) for h in range(11)}
    assert buffer.pv_today_kwh(now, lambda a, b: 99.0) == pytest.approx(6.0 + 22.0)
    assert len(buffer.house_recent(now)) == 12 + 11
