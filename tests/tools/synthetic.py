"""A synthetic site in a FakeHass: weeks of Sol-Ark statistics, a few days of history, the
Solcast attributes, both cars, Rivian schedule control and the free-hours Schedule helper.

Made-up values under the owner's entity ids (tools/config.example.json); no real data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
import math
from typing import Any
from zoneinfo import ZoneInfo

from fake_hass import FakeHass

TZ = ZoneInfo("America/Los_Angeles")
FIVE = timedelta(minutes=5)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

E = {
    "pv": "sensor.sol_ark_inverter_pv_power",
    "grid": "sensor.sol_ark_inverter_grid_power",
    "battery": "sensor.sol_ark_inverter_battery_power",
    "soc": "sensor.sol_ark_inverter_battery_state_of_charge",
    "capacity": "sensor.sol_ark_battery_capacity_estimate",
    "wall": "sensor.tesla_wall_connector_total_power",
    "wall_current": "sensor.garage_tesla_wall_connector_vehicle_current",
    "wall_connected": "binary_sensor.tesla_wall_connector_vehicle_connected",
    "wall_contactor": "binary_sensor.tesla_wall_connector_contactor_closed",
    "fc_today": "sensor.solcast_pv_forecast_forecast_today",
    "fc_tomorrow": "sensor.solcast_pv_forecast_forecast_tomorrow",
    "fc_remaining": "sensor.solcast_pv_forecast_forecast_remaining_today",
    "fc_polled": "sensor.solcast_pv_forecast_api_last_polled",
    "free": "schedule.free_hours",
    "r_soc": "sensor.r1s_battery_state_of_charge",
    "r_limit": "sensor.r1s_battery_state_of_charge_limit",
    "r_plugged": "binary_sensor.r1s_charger_connection",
    "r_location": "device_tracker.r1s_location",
    "r_switch": "switch.r1s_schedule_control_solar_charging",
    "r_current": "number.r1s_schedule_control_solar_current",
    "r_state": "sensor.r1s_schedule_control_control_state",
    "r_status": "sensor.r1s_schedule_control_charger_status",
    "r_renewal": "switch.r1s_schedule_control_lease_renewal",
    "r_restore": "button.r1s_schedule_control_restore_owner_schedule",
    "r_read": "button.r1s_schedule_control_read_schedules",
    "t_soc": "sensor.tesla_battery_level",
    "t_limit": "number.tesla_charge_limit",
    "t_plugged": "binary_sensor.tesla_charge_cable",
    "t_location": "device_tracker.tesla_location",
    "t_switch": "switch.tesla_charge",
    "t_current": "number.tesla_charge_current",
    "t_charging": "sensor.tesla_charging",
}


def clear_sky_kw(h: float, peak: float = 9.0) -> float:
    """A smooth October-ish solar day, sunrise 07:00, sunset 18:30 (kW)."""
    if not 7.0 < h < 18.5:
        return 0.0
    return peak * math.sin(math.pi * (h - 7.0) / 11.5) ** 1.6


def house_kw(t: datetime) -> float:
    h = t.hour + t.minute / 60
    kw = 0.6 + 1.5 * math.exp(-((h - 19.0) ** 2) / 3) + 0.7 * math.exp(-((h - 7.5) ** 2))
    return kw * (1.15 if t.weekday() >= 5 else 1.0)


@dataclass
class Site:
    """What was generated, for assertions."""

    now: datetime  # aware local
    start: datetime  # first local midnight with history
    soc: dict[datetime, float] = field(default_factory=dict)
    pv: dict[datetime, float] = field(default_factory=dict)
    car_plugged_at: datetime | None = None


def _cloud(d: date) -> float:
    return (0.55, 1.0, 0.8, 1.0, 0.35, 0.9, 1.0)[d.toordinal() % 7]


def detailed(d: date, scale: float = 0.9) -> list[dict[str, Any]]:
    rows = []
    for j in range(48):
        t = datetime.combine(d, time(j // 2, 30 * (j % 2)), tzinfo=TZ)
        p50 = round(clear_sky_kw(j / 2 + 0.25) * scale, 4)
        rows.append(
            {
                "period_start": t.isoformat(),
                "pv_estimate": p50,
                "pv_estimate10": round(p50 * 0.7, 4),
                "pv_estimate90": round(p50 * 1.15, 4),
            }
        )
    return rows


def _day_attrs(d: date, scale: float = 0.9) -> dict[str, Any]:
    rows = detailed(d, scale)
    tot = lambda k: round(sum(r[k] for r in rows) / 2, 2)  # noqa: E731
    return {
        "unit_of_measurement": "kWh",
        "estimate": tot("pv_estimate"),
        "estimate10": tot("pv_estimate10"),
        "estimate90": tot("pv_estimate90"),
        "dayname": d.strftime("%A"),
        "dataCorrect": True,
        "detailedForecast": rows,
        "detailedForecast_abcd_1234_ef56_7890": rows,  # a per-site breakdown (site id key)
    }


def build_site(
    fake: FakeHass,
    now: datetime,
    *,
    history_days: int = 2,
    stat_days: int = 16,
    grid_sign: int = 1,
    battery_sign: int = 1,
    rivian_plug_hour: float | None = 10.0,
    rivian_amps: int = 16,
    schedule_control_dry_run: bool = True,
) -> Site:
    """Fill ``fake`` with a site that ran from ``stat_days`` before ``now`` (aware local).
    The Rivian plugs in on the last day at ``rivian_plug_hour`` and charges at
    ``rivian_amps`` (as its owner's schedule would); the Tesla is away."""
    now = now.astimezone(TZ)
    fake.clock = lambda: now.astimezone(UTC)
    t = datetime.combine(now.date() - timedelta(days=stat_days), time(0), tzinfo=TZ)
    hist_from = datetime.combine(now.date() - timedelta(days=history_days), time(0), tzinfo=TZ)
    site = Site(now, hist_from)
    plug = None
    if rivian_plug_hour is not None:
        plug = datetime.combine(now.date(), time(0), tzinfo=TZ) + timedelta(hours=rivian_plug_hour)
        site.car_plugged_at = plug
    soc, cap = 55.0, 31.0
    r_soc = 48.0
    five: dict[str, list[dict[str, Any]]] = {E[k]: [] for k in ("pv", "grid", "battery", "wall")}
    five[E["soc"]] = []
    while t < now:
        h = t.hour + t.minute / 60 + 2.5 / 60
        pv = clear_sky_kw(h) * _cloud(t.date()) * (0.85 + 0.15 * math.sin(t.minute / 9.5))
        house = house_kw(t)
        charging = plug is not None and t >= plug and r_soc < 80
        car = rivian_amps * 0.24 if charging else 0.0
        if charging:
            r_soc += car * 0.9 * (5 / 60) / 135 * 100
        net = pv - house - car
        free = t.hour >= 20 or t.hour < 6
        if net >= 0:
            bat = min(net, 9.0, (100 - soc) / 100 * cap * 12)
        elif free:
            bat = 0.0
        else:
            bat = -min(-net, 9.0, max(0.0, soc - 10) / 100 * cap * 12)
        soc = min(100.0, max(10.0, soc + bat * (5 / 60) / cap * 100))
        grid = house + car - pv + bat
        site.soc[t] = soc
        site.pv[t] = pv
        start_ms = t.timestamp() * 1000

        def row(v: float, start_ms: float = start_ms) -> dict[str, float]:
            return {"start": start_ms, "end": start_ms + 300000, "mean": v}

        five[E["pv"]].append(row(pv * 1000))
        five[E["grid"]].append(row(grid_sign * grid * 1000))
        five[E["battery"]].append(row(battery_sign * bat * 1000))
        five[E["wall"]].append(row(car))
        five[E["soc"]].append(row(soc))
        if t >= hist_from:
            when = t + FIVE
            fake.set_state(E["pv"], round(pv * 1000), {"unit_of_measurement": "W"}, when=when)
            fake.set_state(
                E["grid"], round(grid_sign * grid * 1000), {"unit_of_measurement": "W"}, when=when
            )
            fake.set_state(
                E["battery"],
                round(battery_sign * bat * 1000),
                {"unit_of_measurement": "W"},
                when=when,
            )
            fake.set_state(E["soc"], round(soc), {"unit_of_measurement": "%"}, when=when)
            fake.set_state(E["wall"], round(car, 2), {"unit_of_measurement": "kW"}, when=when)
            fake.set_state(
                E["wall_current"],
                rivian_amps if charging else 0,
                {"unit_of_measurement": "A"},
                when=when,
            )
            fake.set_state(E["wall_contactor"], "on" if charging else "off", {}, when=when)
            plugged = plug is not None and t >= plug
            fake.set_state(E["wall_connected"], "on" if plugged else "off", {}, when=when)
            fake.set_state(E["r_plugged"], "on" if plugged else "off", {}, when=when)
            fake.set_state(E["r_soc"], round(r_soc), {"unit_of_measurement": "%"}, when=when)
        t += FIVE
    # recorder tables: 5-min rows for the last 10 days, hourly rows for all
    cut = (now - timedelta(days=10)).timestamp() * 1000
    for sid, rows in five.items():
        fake.statistics["5minute"][sid] = [r for r in rows if r["start"] >= cut]
        hourly = []
        for i in range(0, len(rows) - 11, 12):
            chunk = rows[i : i + 12]
            hourly.append(
                {
                    "start": chunk[0]["start"],
                    "end": chunk[0]["start"] + 3600000,
                    "mean": sum(r["mean"] for r in chunk) / 12,
                }
            )
        fake.statistics["hour"][sid] = hourly
    fake.stat_units = {E["pv"]: "W", E["grid"]: "W", E["battery"]: "W", E["wall"]: "kW"}
    fake.stat_units[E["soc"]] = "%"
    _static(fake, now, hist_from, schedule_control_dry_run)
    return site


def _static(fake: FakeHass, now: datetime, hist_from: datetime, dry_run: bool) -> None:
    today = now.date()
    fake.set_state(E["capacity"], 31.2, {"unit_of_measurement": "kWh"}, when=hist_from)
    # Solcast: polled every 2 h from 06:00; history keeps the totals, not the half hours
    d = hist_from.date()
    while d <= today:
        for hour in range(6, 19, 2):
            when = datetime.combine(d, time(hour, 3), tzinfo=TZ)
            if when > now:
                break
            attrs = _day_attrs(d, 0.9 if d != today else 0.95)
            fake.set_state(E["fc_polled"], when.astimezone(UTC).isoformat(), {}, when=when)
            fake.set_state(E["fc_today"], attrs["estimate"], attrs, when=when, record=False)
            fake.history.setdefault(E["fc_today"], []).append(
                {
                    **fake.states[E["fc_today"]],
                    "attributes": {k: v for k, v in attrs.items() if "detailed" not in k},
                }
            )
            nxt = _day_attrs(d + timedelta(days=1))
            fake.set_state(E["fc_tomorrow"], nxt["estimate"], nxt, when=when, record=False)
            fake.history.setdefault(E["fc_tomorrow"], []).append(
                {
                    **fake.states[E["fc_tomorrow"]],
                    "attributes": {k: v for k, v in nxt.items() if "detailed" not in k},
                }
            )
        d += timedelta(days=1)
    fake.set_state(
        E["fc_remaining"],
        12.5,
        {"unit_of_measurement": "kWh", "estimate": 12.5, "estimate10": 9.1, "estimate90": 14.0},
    )
    # cars
    loc = {"latitude": 32.87341, "longitude": -117.22749, "gps_accuracy": 5, "source_type": "gps"}
    fake.set_state(E["r_location"], "home", dict(loc, friendly_name="R1S"), when=hist_from)
    fake.set_state(E["r_limit"], 80, {"unit_of_measurement": "%"}, when=hist_from)
    fake.set_state(E["t_location"], "Office", dict(loc, friendly_name="Tesla"), when=hist_from)
    fake.set_state(E["t_soc"], 63, {"unit_of_measurement": "%"}, when=hist_from)
    fake.set_state(
        E["t_limit"], 90, {"unit_of_measurement": "%", "min": 50, "max": 100}, when=hist_from
    )
    fake.set_state(E["t_plugged"], "off", {}, when=hist_from)
    fake.set_state(E["t_switch"], "off", {}, when=hist_from)
    fake.set_state(
        E["t_current"],
        16,
        {"unit_of_measurement": "A", "min": 0, "max": 48, "step": 1},
        when=hist_from,
    )
    fake.set_state(E["t_charging"], "disconnected", {"device_class": "enum"}, when=hist_from)
    for key in ("t_soc", "t_limit", "t_plugged", "t_switch", "t_current", "t_charging"):
        fake.register(E[key], "tesla_fleet")
    fake.register(E["t_location"], "tesla_fleet")
    # Rivian schedule control
    fake.set_state(E["r_switch"], "off", {}, when=hist_from)
    fake.set_state(E["r_current"], 16, {"unit_of_measurement": "A", "min": 0, "max": 80})
    fake.set_state(E["r_renewal"], "on", {})
    fake.set_state(E["r_restore"], "unknown", {})
    fake.set_state(E["r_read"], "unknown", {})
    fake.set_state(E["r_status"], "b", {"options": ["a", "b", "c"]})
    fake.set_state(
        E["r_state"], "dry_run" if dry_run else "idle", schedule_control_attrs(dry_run=dry_run)
    )
    for key in ("r_switch", "r_current", "r_state", "r_status", "r_renewal", "r_restore", "r_read"):
        fake.register(E[key], "solar_ev_planner", f"test-vehicle-0001_{key}")
    # the free-hours Schedule helper
    week = {
        name: [{"from": "00:00:00", "to": "06:00:00"}, {"from": "20:00:00", "to": "23:59:59"}]
        for name in WEEKDAYS
    }
    fake.schedules = [{"id": "free_hours", "name": "Free hours", "icon": "mdi:cash", **week}]
    fake.register(E["free"], "schedule", "free_hours")
    free = now.hour >= 20 or now.hour < 6
    fake.set_state(E["free"], "on" if free else "off", {"friendly_name": "Free hours"})


def schedule_control_attrs(**over: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "phase": "idle",
        "dry_run": True,
        "renewal_enabled": True,
        "handed_back": False,
        "desired_charge": None,
        "requested_amps": 16,
        "lease_active": None,
        "lease_amps": None,
        "lease_written_at": None,
        "lease_expires_at": None,
        "owner_schedule_count": None,
        "provider_schedule_count": 2,
        "fault_code": None,
        "fault_message": None,
        "last_write_at": None,
        "start_blocked_by": None,
        "request_unconfirmed": False,
        "request_held": False,
        "dry_run_target": {"start_time": 600, "end_time": 625, "amperage": 8},
        "observation": {"plugged": True, "home": True, "at_charger": True},
        "last_provider_read": None,
        "last_provider_read_at": None,
    }
    attrs.update(over)
    return attrs
