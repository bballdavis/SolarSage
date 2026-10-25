"""Solcast forecasts as the HA Solcast integration publishes them.

``sensor.solcast_pv_forecast_forecast_today`` and ``..._forecast_tomorrow`` carry a
``detailedForecast`` attribute: a list of half-hour periods

    {"period_start": "2026-10-25T13:30:00-07:00", "pv_estimate": 3.21,
     "pv_estimate10": 2.05, "pv_estimate90": 3.87}

with mean kW over the half hour (p50, p10, p90). Today's list keeps the periods that have
already passed with their last forecast values, which is what the planner's Bayesian
correction compares today's actual production against. ``parse_detailed_forecast`` turns one
or more such attribute dicts into sorted ``ForecastSlot``s in local time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, tzinfo
from typing import Any

from .types import ForecastSlot


def _when(v: Any, tz: tzinfo | None, naive: bool) -> datetime:
    t = datetime.fromisoformat(v) if isinstance(v, str) else v
    if not isinstance(t, datetime):
        raise ValueError(f"period_start is not a time: {v!r}")
    if t.tzinfo is not None:
        t = t.astimezone(tz) if tz is not None else t.astimezone()
        if naive:
            t = t.replace(tzinfo=None)
    elif tz is not None and not naive:
        t = t.replace(tzinfo=tz)
    return t


def _kw(row: Mapping[str, Any], key: str, default: float) -> float:
    v = row.get(key)
    if v is None:
        return default
    try:
        x = float(v)
    except TypeError, ValueError:
        return default
    return x if x == x and x >= 0.0 else default  # NaN or negative -> default


def parse_detailed_forecast(
    *attributes: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
    tz: tzinfo | None = None,
    naive: bool = True,
) -> list[ForecastSlot]:
    """Half-hour ``ForecastSlot``s from Solcast sensor attributes (today, tomorrow, ...).

    Each argument is a sensor's attribute dict (its ``detailedForecast`` list is read) or the
    list itself; None is skipped. ``tz``: the local zone to convert aware timestamps to (None =
    the process's local zone); ``naive=True`` drops the tzinfo afterwards (the planner then
    works in naive local time; pass ``naive=False`` to keep aware local datetimes). Missing
    p10/p90 fall back to p50; duplicate periods keep the later argument's values."""
    by_start: dict[datetime, ForecastSlot] = {}
    for a in attributes:
        if a is None:
            continue
        rows = a.get("detailedForecast", ()) if isinstance(a, Mapping) else a
        for row in rows or ():
            if "period_start" not in row:
                continue
            t = _when(row["period_start"], tz, naive)
            p50 = _kw(row, "pv_estimate", 0.0)
            p10 = min(p50, _kw(row, "pv_estimate10", p50))
            p90 = max(p50, _kw(row, "pv_estimate90", p50))
            by_start[t] = ForecastSlot(t, p10, p50, p90)
    return [by_start[k] for k in sorted(by_start)]


def forecast_energy(
    slots: Iterable[ForecastSlot], t0: datetime, t1: datetime, which: str = "p50"
) -> float:
    """kWh of a forecast over [t0, t1) (partial slots pro-rated)."""
    tot = 0.0
    for s in slots:
        a, b = max(t0, s.start), min(t1, s.end)
        if b > a:
            tot += getattr(s, which) * (b - a).total_seconds() / 3600.0
    return tot
