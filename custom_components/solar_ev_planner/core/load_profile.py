"""House load (without the car): samples from recorder statistics, and the 30-min profile.

There is no house-load sensor; the house is what the Sol-Ark and the Wall Connector leave:

    house = pv + grid - battery - wall connector      (grid + import, battery + charging)

``samples_from_statistics`` joins recorder statistics rows of those four sensors (5-min
short-term statistics or hourly long-term statistics, each row ``{"start": ..., "mean": ...}``)
into ``LoadSample``s. ``build_load_profile`` turns 14 days of samples into the profile the
planner uses: one 30-min profile for weekdays and one for weekends, each a recency-weighted
mean of past days (half-life ``half_life_days``; days of the other type count
``other_type_weight``), plus the day-to-day spread of the energy still to come from any time
of day (for the Gaussian caution). Nothing seasonal is fixed: a jump in use (air conditioning)
enters through the recency weights, and the planner's 24-h level and 2-h offset on top.

The recorder keeps short-term (5-min) statistics for 10 days and hourly statistics for good;
either works (an hourly mean fills both half hours of its hour). The integration may also keep
its own rolling store of 30-min means and pass those as 30-min samples.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from typing import Any

BINS = 48
BIN = timedelta(minutes=30)


@dataclass(frozen=True)
class LoadSample:
    """Mean house load ``kw`` over [start, start + minutes)."""

    start: datetime
    kw: float
    minutes: float = 5.0


def house_load_kw(pv_kw: float, grid_kw: float, battery_kw: float, wall_kw: float) -> float:
    """House load without the EV (kW): pv + grid - battery - wall connector, never negative."""
    return max(0.0, pv_kw + grid_kw - battery_kw - wall_kw)


def _start(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        x = float(v)
        return datetime.fromtimestamp(x / 1000.0 if x > 1e11 else x).replace(tzinfo=None)
    return datetime.fromisoformat(str(v))


def samples_from_statistics(
    pv: Iterable[Mapping[str, Any]],
    grid: Iterable[Mapping[str, Any]],
    battery: Iterable[Mapping[str, Any]],
    wall: Iterable[Mapping[str, Any]],
    *,
    pv_scale: float = 0.001,
    grid_scale: float = 0.001,
    battery_scale: float = 0.001,
    wall_scale: float = 1.0,
    minutes: float | None = None,
) -> list[LoadSample]:
    """House-load samples from recorder statistics rows of the four power sensors.

    Rows are what ``statistics_during_period`` returns per statistic id: mappings with
    ``start`` (datetime, epoch seconds or ms, or ISO text) and ``mean``. Scales convert each
    sensor to kW (the Sol-Ark sensors are W, the Wall Connector's total power is kW). Only
    periods where all four sensors have a mean are kept. ``minutes`` is the statistics period
    (None = inferred from the row spacing: 5 for short-term, 60 for long-term)."""

    def index(rows: Iterable[Mapping[str, Any]], scale: float) -> dict[datetime, float]:
        out = {}
        for r in rows:
            m = r.get("mean")
            if m is None or "start" not in r:
                continue
            try:
                out[_start(r["start"])] = float(m) * scale
            except TypeError, ValueError:
                continue
        return out

    a, b, c, d = (
        index(pv, pv_scale),
        index(grid, grid_scale),
        index(battery, battery_scale),
        index(wall, wall_scale),
    )
    keys = sorted(set(a) & set(b) & set(c) & set(d))
    if minutes is None:
        gaps = [(y - x).total_seconds() / 60.0 for x, y in zip(keys, keys[1:], strict=False)]
        gaps = [g for g in gaps if g > 0]
        minutes = min(gaps) if gaps else 60.0
    return [LoadSample(k, house_load_kw(a[k], b[k], c[k], d[k]), minutes) for k in keys]


def bin_days(samples: Iterable[LoadSample]) -> dict[date, list[float | None]]:
    """Time-weighted mean kW per local day and 30-min bin (None where nothing was seen)."""
    acc: dict[tuple[date, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for s in samples:
        t, end = s.start, s.start + timedelta(minutes=s.minutes)
        while t < end:
            b = (t.hour * 60 + t.minute) // 30
            edge = t.replace(minute=(t.minute // 30) * 30, second=0, microsecond=0) + BIN
            seg = (min(end, edge) - t).total_seconds() / 3600.0
            if seg <= 0:
                break
            x = acc[(t.date(), b)]
            x[0] += s.kw * seg
            x[1] += seg
            t = min(end, edge)
    out: dict[date, list[float | None]] = {}
    for (d, b), (e, h) in acc.items():
        out.setdefault(d, [None] * BINS)[b] = e / h if h > 0 else None
    return out


@dataclass(frozen=True)
class LoadProfile:
    """Recency-weighted weekday / weekend 30-min profile of the house load (kW)."""

    today: date
    days: tuple[tuple[date, tuple[float, ...]], ...]  # usable past days, 48 bins each (kW)
    weekday: tuple[float, ...]
    weekend: tuple[float, ...]
    half_life_days: float = 5.0
    other_type_weight: float = 0.1

    @property
    def n_days(self) -> int:
        return len(self.days)

    def weight(self, d: date) -> float:
        """Recency weight of a past day (1 for yesterday's age 1 halves every half-life)."""
        return 0.5 ** ((self.today - d).days / self.half_life_days)

    def bins_for(self, d: date) -> tuple[float, ...]:
        return self.weekend if d.weekday() >= 5 else self.weekday

    def kw_at(self, t: datetime) -> float:
        return self.bins_for(t.date())[(t.hour * 60 + t.minute) // 30]

    def energy_kwh(self, t0: datetime, t1: datetime) -> float:
        tot, t = 0.0, t0
        while t < t1:
            edge = t.replace(minute=(t.minute // 30) * 30, second=0, microsecond=0) + BIN
            u = min(t1, edge)
            tot += self.kw_at(t) * (u - t).total_seconds() / 3600.0
            t = u
        return tot

    def _loo_factors(self) -> dict[date, float]:
        """Leave-one-out factor per day: a day's residual against a profile that does not
        contain it is its in-sample residual x W / (W - w_day) (honest, out-of-sample)."""
        out = {}
        for d, _ in self.days:
            tot = sum(_type_weight(self, x, d) for x, _ in self.days)
            own = self.weight(d)
            out[d] = tot / (tot - own) if tot - own > 1e-9 else 1.0
        return out

    def spread_kwh(self, t0: datetime, n_bins: int) -> float:
        """Spread (kWh, one standard deviation) of the house energy over the n_bins half
        hours from t0: for every past day, what the house used from this time of day on minus
        its profile; the recency-weighted RMS of those. Day-to-day level changes, appliance
        runs and noise all show up in it. Without history: 30 % of the expected energy."""
        if not self.days:
            return 0.3 * self.energy_kwh(t0, t0 + n_bins * BIN)
        j0 = (t0.hour * 60 + t0.minute) // 30
        bins = dict(self.days)
        loo = self._loo_factors()
        num = wsum = 0.0
        for d, _ in self.days:
            r, dd, j, ok = 0.0, d, j0, True
            for _ in range(n_bins):
                b = bins.get(dd)
                if b is None:
                    ok = False
                    break
                r += (b[j] - self.bins_for(dd)[j]) * 0.5
                j += 1
                if j == BINS:
                    dd, j = dd + timedelta(days=1), 0
            if ok:
                w = self.weight(d)
                r *= loo.get(d, 1.0)
                num += w * r * r
                wsum += w
        if wsum <= 0:
            return 0.3 * self.energy_kwh(t0, t0 + n_bins * BIN)
        return math.sqrt(num / wsum)


def _type_weight(prof: LoadProfile, d: date, of: date) -> float:
    same = (d.weekday() >= 5) == (of.weekday() >= 5)
    return prof.weight(d) * (1.0 if same else prof.other_type_weight)


def build_load_profile(
    samples: Iterable[LoadSample],
    today: date,
    *,
    days: int = 14,
    half_life_days: float = 5.0,
    other_type_weight: float = 0.1,
    min_coverage: float = 0.9,
    default_kw: float = 1.2,
) -> LoadProfile:
    """The weekday and weekend 30-min profiles from the ``days`` complete days before
    ``today``. A day counts when at least ``min_coverage`` of its half hours have data (gaps
    are filled from the same half hour of the other days). With no usable day the profile is
    flat at ``default_kw`` and the spread is wide, until history accrues."""
    binned = bin_days(samples)
    first = today - timedelta(days=days)
    kept = {
        d: b
        for d, b in binned.items()
        if first <= d < today and sum(x is not None for x in b) >= min_coverage * BINS
    }
    prof = LoadProfile(today, (), (), (), half_life_days, other_type_weight)

    def mean_profile(weekend: bool) -> tuple[float, ...]:
        out = []
        for j in range(BINS):
            num = wsum = 0.0
            for d, b in kept.items():
                if b[j] is None:
                    continue
                same = (d.weekday() >= 5) == weekend
                w = prof.weight(d) * (1.0 if same else other_type_weight)
                num += w * b[j]
                wsum += w
            out.append(num / wsum if wsum > 0 else default_kw)
        return tuple(out)

    wk, we = mean_profile(False), mean_profile(True)
    full = tuple(
        (
            d,
            tuple(
                x if x is not None else (we if d.weekday() >= 5 else wk)[j]
                for j, x in enumerate(kept[d])
            ),
        )
        for d in sorted(kept)
    )
    return LoadProfile(today, full, wk, we, half_life_days, other_type_weight)
