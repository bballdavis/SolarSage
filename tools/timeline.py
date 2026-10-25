"""A snapshot (snapshot.py) as time series, and the planner inputs at any moment of it.

All times here are naive local wall-clock times in the snapshot's time zone, as the planner
core expects.

Powers (kW) and the battery level come as 5-min means, from the best source available:
5-min recorder statistics, else the state history (time-weighted), else hourly statistics.
The house load is ``pv + grid - battery - wall connector`` (``house_load_kw``).

**Forecasts in force.** At each moment the replay needs the Solcast half hours as they were
known then. In order of preference, per day:

1. ``recorded``: the latest issue recorded by ``snapshot record-forecasts`` (or found in the
   history, if your recorder keeps ``detailedForecast``) issued at or before that moment;
2. ``current``: the forecast in the snapshot itself, from its issue time on;
3. ``totals``: an approximation. HA records each day's totals (``forecast_today`` /
   ``forecast_tomorrow`` state with ``estimate10`` / ``estimate90``) even though it drops the
   half hours. The totals in force are spread over the day with a clear-sky shape: per half
   hour, the highest actual solar of the days within a week either side (their envelope),
   normalised. So the day's level, its spread and its timing are right, but not when clouds
   come; Solcast does not know that either, and the planner corrects the rest of the day
   from actual production as it would live;
4. ``actual``: with no forecast history at all, the day's own actual total with p10/p90 at
   0.75x/1.1x and the same shape. This is a hindsight forecast: it flatters the planner,
   and replays mark it.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
import math
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

from tools.config import TO_KW

FIVE = timedelta(minutes=5)
HALF = timedelta(minutes=30)
ROLES = ("pv_power", "grid_power", "battery_power", "wall_power", "battery_soc")
UNAVAILABLE = ("unavailable", "unknown", "", None)
DEFAULT_UNITS = {
    "pv_power": "W",
    "grid_power": "W",
    "battery_power": "W",
    "wall_power": "kW",
    "battery_soc": "%",
}
ACTUAL_SPREAD = (0.75, 1.1)  # p10, p90 of the hindsight forecast, as fractions of p50


def floor5(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)


def _num(v: Any) -> float | None:
    if v in UNAVAILABLE:
        return None
    try:
        x = float(v)
    except TypeError, ValueError:
        return None
    return x if math.isfinite(x) else None


@dataclass(frozen=True)
class DayForecast:
    day: date
    slots: tuple[Any, ...]  # planner_core.ForecastSlot, 48 or fewer
    issued_at: datetime | None
    source: str


class Timeline:
    """The snapshot's series. ``core`` is the loaded planner_core package."""

    def __init__(self, snap: Mapping[str, Any], core: ModuleType) -> None:
        self.core = core
        self.snap = snap
        self.tz = ZoneInfo(snap.get("time_zone") or "UTC")
        rng = snap["range"]
        self.start = self.local(rng["start"])
        self.end = self.local(rng["end"])
        m = snap["mapping"]
        self.site: dict[str, str] = m["site"]
        self.cars: dict[str, dict[str, str]] = m.get("cars", {})
        self.units = dict(DEFAULT_UNITS) | dict(m.get("units") or {})
        self.history: dict[str, tuple[list[datetime], list[Any], list[Any]]] = {}
        for eid, rows in (snap.get("history") or {}).items():
            rows = [r for r in rows if r and r[0]]
            self.history[eid] = (
                [self.local(r[0]) for r in rows],
                [r[1] for r in rows],
                [r[2] if len(r) > 2 else None for r in rows],
            )
        self.bins: dict[str, dict[datetime, float]] = {r: {} for r in ROLES}
        self.minutes: dict[datetime, float] = {}  # 5 or 60: the sample length behind a bin
        self._build_bins()
        self.forecasts = Forecasts(self)

    # ------------------------------------------------------------------ time and states

    def local(self, v: str | datetime) -> datetime:
        t = datetime.fromisoformat(v) if isinstance(v, str) else v
        if t.tzinfo is not None:
            t = t.astimezone(self.tz).replace(tzinfo=None)
        return t

    def state_at(self, eid: str | None, t: datetime) -> Any:
        """The state in force at t (the last change at or before t), None if unknown."""
        if not eid or eid not in self.history:
            return None
        times, states, _ = self.history[eid]
        i = bisect_right(times, t)
        return states[i - 1] if i else None

    def number_at(self, eid: str | None, t: datetime) -> float | None:
        return _num(self.state_at(eid, t))

    # ------------------------------------------------------------------ powers

    def _scale(self, role: str, eid: str) -> float:
        if role == "battery_soc":
            return 1.0
        unit = (self.snap.get("statistics_units") or {}).get(eid) or self.units.get(role)
        return TO_KW.get(unit or "", TO_KW[DEFAULT_UNITS[role]])

    def _build_bins(self) -> None:
        stats = self.snap.get("statistics") or {}
        for role in ROLES:
            eid = self.site.get(role)
            if not eid:
                continue
            k = self._scale(role, eid)
            out = self.bins[role]
            for r in (stats.get("hour") or {}).get(eid, ()):
                t0 = self.local(r["start"])
                for j in range(12):
                    out[t0 + j * FIVE] = float(r["mean"]) * k
                    self.minutes.setdefault(t0 + j * FIVE, 60.0)
            for t, v in self._history_bins(eid, k).items():
                out[t] = v
                self.minutes[t] = 5.0
            for r in (stats.get("5minute") or {}).get(eid, ()):
                t = self.local(r["start"])
                out[t] = float(r["mean"]) * k
                self.minutes[t] = 5.0

    def _history_bins(self, eid: str, k: float) -> dict[datetime, float]:
        """Time-weighted 5-min means of a numeric state history (kW)."""
        if eid not in self.history:
            return {}
        times, states, _ = self.history[eid]
        out: dict[datetime, float] = {}
        acc: dict[datetime, list[float]] = {}
        for i, t in enumerate(times):
            v = _num(states[i])
            end = times[i + 1] if i + 1 < len(times) else self.end
            if v is None or end <= t:
                continue
            u = t
            while u < end:
                b = floor5(u)
                seg_end = min(end, b + FIVE)
                a = acc.setdefault(b, [0.0, 0.0])
                sec = (seg_end - u).total_seconds()
                a[0] += v * k * sec
                a[1] += sec
                u = seg_end
        for b, (e, s) in acc.items():
            if s >= 150:  # at least half the bin seen
                out[b] = e / s
        return out

    def power(self, role: str, b: datetime) -> float | None:
        """Mean of the 5-min bin starting at b (kW, or % for the battery level)."""
        return self.bins[role].get(b)

    def house(self, b: datetime) -> float | None:
        vals = [self.power(r, b) for r in ("pv_power", "grid_power", "battery_power", "wall_power")]
        if any(v is None for v in vals):
            return None
        return self.core.house_load_kw(*vals)

    def samples(self) -> list[Any]:
        """House-load samples of every bin with all four powers (planner LoadSample)."""
        out = []
        for b in sorted(self.bins["pv_power"]):
            h = self.house(b)
            if h is not None:
                out.append(self.core.LoadSample(b, h, 5.0))
        return out

    def pv_day_kwh(self, d: date, until: datetime | None = None) -> float | None:
        """Actual solar of a day (to ``until``); None when a fifth of it is missing."""
        t = datetime.combine(d, time(0))
        end = min(until or t + timedelta(days=1), t + timedelta(days=1))
        tot, seen, n = 0.0, 0, 0
        while t < end:
            n += 1
            v = self.power("pv_power", t)
            if v is not None:
                tot += v * 5 / 60
                seen += 1
            t += FIVE
        return tot if n and seen >= 0.8 * n else None

    # ------------------------------------------------------------------ cars and tariff

    def tariff(self) -> Any:
        c, m = self.core, self.snap["mapping"].get("tariff") or {}
        price, buyback = float(m.get("price", 0.32)), float(m.get("buyback", 0.0))
        if weekly := self.snap.get("free_hours"):
            sched = c.WeeklySchedule.from_ha(weekly)
        elif fb := m.get("free_hours_fallback"):
            sched = c.WeeklySchedule.daily(fb[0], fb[1])
        else:
            return c.Tariff(price, buyback)
        return c.Tariff(price, buyback, (c.PriceWindow(sched, 0.0, "free"),))

    def battery(self, t: datetime) -> Any:
        m = dict(self.snap["mapping"].get("battery") or {})
        cap = self.number_at(self.site.get("battery_capacity"), t)
        if cap is not None and 5 <= cap <= 200:
            m["capacity_kwh"] = cap
        keys = (
            "capacity_kwh",
            "max_charge_kw",
            "max_discharge_kw",
            "efficiency",
            "reserve_pct",
            "holds_in_free_hours",
        )
        return self.core.BatterySpec(**{k: m[k] for k in keys if k in m})

    def car_spec(self, car_id: str) -> Any:
        car = self.cars[car_id]
        base = self.core.TESLA if car.get("kind") == "tesla" else self.core.RIVIAN_R1S
        return replace(base, name=car.get("name") or base.name)

    def car_plugged(self, car_id: str, t: datetime) -> bool:
        """On the Wall Connector: it sees a vehicle, and this car's cable is in at home."""
        car = self.cars[car_id]
        return (
            self.state_at(self.site.get("wall_connected"), t) == "on"
            and self.state_at(car.get("plugged"), t) == "on"
            and self.state_at(car.get("location"), t) == "home"
        )

    def plans(self, car_id: str, t: datetime) -> tuple[Any, ...]:
        out = []
        for p in (self.snap["mapping"].get("plans") or {}).get(car_id, ()):
            rp = self.core.RepeatingPlan(
                float(p["target_pct"]), p.get("at", "07:30"), tuple(p.get("weekdays", range(5)))
            )
            out += rp.deadlines(t)
        return tuple(out)


class Forecasts:
    """The forecast in force at any moment (see the module docstring for the sources)."""

    def __init__(self, tl: Timeline) -> None:
        self.tl = tl
        self.issues: list[tuple[datetime, str, dict[date, list[Any]]]] = []
        fc = tl.snap.get("forecast") or {}
        for issue in fc.get("issues") or ():
            self._add(issue.get("issued_at"), "recorded", issue.get("slots") or ())
        for role in ("forecast_today", "forecast_tomorrow"):
            eid = tl.site.get(role)
            if eid and eid in tl.history:
                times, _, attrs = tl.history[eid]
                for t, a in zip(times, attrs, strict=True):
                    rows = (a or {}).get("detailedForecast")
                    if rows:
                        slots = [
                            [
                                r.get("period_start"),
                                r.get("pv_estimate10"),
                                r.get("pv_estimate"),
                                r.get("pv_estimate90"),
                            ]
                            for r in rows
                        ]
                        self._add(t, "recorded", slots)
        cur = fc.get("current") or {}
        if cur.get("slots"):
            self._add(cur.get("issued_at") or tl.end, "current", cur["slots"])
        # on a tie the recorded issue comes last, so it is the one found
        self.issues.sort(key=lambda x: (x[0], x[1] != "current"))
        self._issue_times = [i[0] for i in self.issues]
        self._shape_cache: dict[date, list[float]] = {}

    def _add(self, issued: Any, source: str, rows: Sequence[Sequence[Any]]) -> None:
        if issued is None:
            return
        by_day: dict[date, list[Any]] = {}
        for r in rows:
            try:
                start = self.tl.local(str(r[0]))
                p10, p50, p90 = (float(x) if x is not None else None for x in r[1:4])
            except TypeError, ValueError, IndexError:
                continue
            p50 = p50 or 0.0
            p10 = min(p50, p10 if p10 is not None else p50)
            p90 = max(p50, p90 if p90 is not None else p50)
            by_day.setdefault(start.date(), []).append(
                self.tl.core.ForecastSlot(start, p10, p50, p90)
            )
        if by_day:
            self.issues.append((self.tl.local(issued), source, by_day))

    def day(self, d: date, at: datetime) -> DayForecast:
        """Day ``d``'s half hours as known at ``at``."""
        i = bisect_right(self._issue_times, at)
        while i > 0:
            i -= 1
            issued, source, by_day = self.issues[i]
            slots = by_day.get(d)
            if slots and len(slots) >= 40:
                return DayForecast(d, tuple(sorted(slots, key=lambda s: s.start)), issued, source)
        totals = self._totals(d, at)
        if totals is not None:
            changed, p10, p50, p90 = totals
            # issued at the last poll before ``at`` (the totals may not have moved since)
            polled = self.tl.state_at(self.tl.site.get("forecast_last_polled"), at)
            try:
                issued = self.tl.local(polled) if polled not in UNAVAILABLE else changed
            except TypeError, ValueError:
                issued = changed
            return DayForecast(d, self._spread(d, p10, p50, p90), issued, "totals")
        actual = self.tl.pv_day_kwh(d)
        if actual is None:  # a day outside the data: the nearest day's actual
            known = [
                x for x in (self.tl.pv_day_kwh(d - timedelta(days=k)) for k in range(1, 8)) if x
            ]
            actual = known[0] if known else 0.0
        lo, hi = ACTUAL_SPREAD
        return DayForecast(d, self._spread(d, actual * lo, actual, actual * hi), None, "actual")

    def _totals(self, d: date, at: datetime) -> tuple[datetime, float, float, float] | None:
        """The day's recorded totals in force at ``at``: today's sensor on day d, else the
        tomorrow sensor from the day before."""
        for role, offset in (("forecast_today", 0), ("forecast_tomorrow", 1)):
            # walk back to the newest entry written on the right day
            eid = self.tl.site.get(role)
            if not eid or eid not in self.tl.history:
                continue
            times, states, attrs = self.tl.history[eid]
            i = bisect_right(times, at)
            while i > 0:
                i -= 1
                t = times[i]
                if t.date() + timedelta(days=offset) != d:
                    if t.date() + timedelta(days=offset) < d:
                        break
                    continue
                a = attrs[i] or {}
                p50 = _num(a.get("estimate")) or _num(states[i])
                if p50 is None:
                    continue
                p10 = _num(a.get("estimate10")) or p50
                p90 = _num(a.get("estimate90")) or p50
                return t, min(p10, p50), p50, max(p90, p50)
        return None

    def shape(self, d: date) -> list[float]:
        """Clear-sky fractions of the day's energy per half hour (sum 1)."""
        if d in self._shape_cache:
            return self._shape_cache[d]
        env = [0.0] * 48
        for k in range(-7, 8):
            day0 = datetime.combine(d + timedelta(days=k), time(0))
            for j in range(48):
                vals = [self.tl.power("pv_power", day0 + j * HALF + m * FIVE) for m in range(6)]
                vals = [v for v in vals if v is not None]
                if len(vals) >= 3:
                    env[j] = max(env[j], sum(vals) / len(vals))
        if sum(env) <= 0:  # no solar data at all: a plain 07:00-18:00 arch
            env = [max(0.0, math.sin(math.pi * (j / 2 + 0.25 - 7) / 11)) for j in range(48)]
        tot = sum(env)
        out = [e / tot for e in env]
        self._shape_cache[d] = out
        return out

    def _spread(self, d: date, p10: float, p50: float, p90: float) -> tuple[Any, ...]:
        day0 = datetime.combine(d, time(0))
        slot = self.tl.core.ForecastSlot
        return tuple(
            slot(day0 + j * HALF, 2 * f * p10, 2 * f * p50, 2 * f * p90)
            for j, f in enumerate(self.shape(d))
        )

    def at(self, t: datetime) -> tuple[list[Any], datetime | None, str]:
        """Today's and tomorrow's slots in force at t, the issue time and today's source."""
        today = self.day(t.date(), t)
        tomorrow = self.day(t.date() + timedelta(days=1), t)
        issued = today.issued_at or tomorrow.issued_at
        return list(today.slots) + list(tomorrow.slots), issued, today.source

    def pv_days(self, before: date, days: int = 14) -> list[Any]:
        """Past days' actual solar against the forecast in force that morning (07:00); days
        with only a hindsight forecast are left out."""
        out = []
        for k in range(days, 0, -1):
            d = before - timedelta(days=k)
            actual = self.tl.pv_day_kwh(d)
            if actual is None:
                continue
            fc = self.day(d, datetime.combine(d, time(7)))
            if fc.source == "actual":
                continue
            total = sum(s.p50 for s in fc.slots) / 2
            out.append(self.tl.core.PvDay(d, actual, total))
        return out
