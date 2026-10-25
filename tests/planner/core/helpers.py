"""Test builders: a synthetic but realistic site (no harness, no Home Assistant)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import math

from custom_components.solar_ev_planner.core import (
    RIVIAN_R1S,
    TESLA,
    BatterySpec,
    CarPlan,
    CarState,
    ForecastSlot,
    LoadSample,
    Measured,
    Settings,
    Snapshot,
    Tariff,
    build_load_profile,
)

MONDAY = date(2026, 6, 15)

# hourly weekday / weekend house profile (kW), like the harness's
WEEKDAY = [0.55] * 5 + [0.7, 1.4, 2.0, 1.6, 0.9, 0.9, 0.9, 1.1, 1.1, 1.3, 1.3, 1.3, 2.2, 2.8]
WEEKDAY += [2.6, 2.0, 1.4, 0.9, 0.7]
WEEKEND = [0.6] + [0.55] * 4 + [0.6, 0.9, 1.4, 2.0, 1.7, 1.3, 1.3, 1.4, 1.3, 1.3, 1.3, 1.4]
WEEKEND += [2.2, 2.8, 2.6, 2.1, 1.5, 1.0, 0.75]


def clear_sky(t: datetime, peak: float = 11.0, rise: float = 6.5, sset: float = 20.5) -> float:
    h = t.hour + t.minute / 60.0
    if h <= rise or h >= sset:
        return 0.0
    return peak * math.sin(math.pi * (h - rise) / (sset - rise)) ** 1.6


def forecast(day: date, ratio: float = 1.0, spread: float = 0.15, peak: float = 11.0, **kw):
    """96 half hours (day and the next) of a clear-sky forecast x ratio."""
    out = []
    t = datetime.combine(day, datetime.min.time())
    for _ in range(96):
        p50 = clear_sky(t + timedelta(minutes=15), peak, **kw) * ratio
        out.append(
            ForecastSlot(t, p50 * math.exp(-1.2816 * spread), p50, p50 * math.exp(1.2816 * spread))
        )
        t += timedelta(minutes=30)
    return out


def house_samples(today: date, days: int = 14, scale: float = 1.0) -> list[LoadSample]:
    """5-min house samples for the days before ``today`` (weekday / weekend shapes)."""
    out = []
    for k in range(days, 0, -1):
        d = today - timedelta(days=k)
        prof = WEEKEND if d.weekday() >= 5 else WEEKDAY
        t = datetime.combine(d, datetime.min.time())
        for i in range(288):
            out.append(LoadSample(t + timedelta(minutes=5 * i), prof[i // 12] * scale, 5.0))
    return out


def recent_samples(now: datetime, scale: float = 1.0) -> list[LoadSample]:
    out = []
    t = now - timedelta(hours=24)
    while t < now:
        prof = WEEKEND if t.weekday() >= 5 else WEEKDAY
        out.append(LoadSample(t, prof[t.hour] * scale, 5.0))
        t += timedelta(minutes=5)
    return out


def pv_so_far(now: datetime, ratio: float = 1.0) -> float:
    t = datetime.combine(now.date(), datetime.min.time())
    e = 0.0
    while t < now:
        e += clear_sky(t + timedelta(minutes=2.5)) * ratio / 12.0
        t += timedelta(minutes=5)
    return e


def rivian(soc: float = 40.0, plugged: bool = True, since: datetime | None = None, **kw):
    return CarState("rivian", RIVIAN_R1S, soc, 85.0, plugged, since, **kw)


def tesla(soc: float = 40.0, plugged: bool = False, since: datetime | None = None, **kw):
    return CarState("tesla", TESLA, soc, 90.0, plugged, since, **kw)


def snapshot(
    now: datetime,
    soc: float = 60.0,
    cars=None,
    actual_ratio: float = 1.0,
    fc_ratio: float = 1.0,
    tariff: Tariff | None = None,
    house_scale: float = 1.0,
    measured: Measured | None = None,
    **kw,
) -> Snapshot:
    """A sunny summer site at ``now``: forecast x fc_ratio, actual production x actual_ratio."""
    today = now.date()
    fc = forecast(today, fc_ratio)
    pv_now = clear_sky(now) * actual_ratio
    if measured is None:
        measured = Measured(pv_now, 0.0, 0.0, 0.0, soc, None)
    return Snapshot(
        now=now,
        soc_pct=soc,
        battery=BatterySpec(),
        tariff=tariff or Tariff.free_nights(),
        forecast=fc,
        load=build_load_profile(house_samples(today), today),
        house_recent=recent_samples(now, house_scale),
        pv_today_kwh=pv_so_far(now, actual_ratio),
        pv_recent_kw=pv_now,
        measured=measured,
        cars=tuple(cars if cars is not None else (rivian(since=now - timedelta(hours=1)),)),
        forecast_issued_at=now - timedelta(hours=1),
        **kw,
    )


__all__ = [
    "MONDAY",
    "CarPlan",
    "Settings",
    "clear_sky",
    "forecast",
    "house_samples",
    "recent_samples",
    "rivian",
    "snapshot",
    "tesla",
]
