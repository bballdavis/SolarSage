"""House-use history and the live power buffer.

History: 14 days of hourly long-term statistics (``statistics_during_period``, mean) of the
four power sensors give the house-load profile; the last 24 h of 5-minute statistics seed
today's solar and the recent house load after a restart. Both are read in the recorder's
executor and the profile is built in an executor job, never on the event loop.

Live: the coordinator adds a reading of the four powers every minute. The buffer keeps the
5-minute means the core's safety rules and house model need, and today's solar energy. Where
neither live readings nor 5-minute statistics cover a period (after a restart, or a recorder
without short-term statistics), today's hourly means fill in, and for today's solar the
forecast itself does last, so a gap is neutral evidence for the forecast correction.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_POWER,
    CONF_GRID_POWER,
    CONF_PV_POWER,
    CONF_VOLTS,
    CONF_WALL_POWER,
    DEFAULT_POWER_UNITS,
    HISTORY_DAYS,
    POWER_KEYS,
    RECENT_HOURS,
    invert_key,
    power_scale,
    unit_key,
)
from .core import (
    LoadProfile,
    LoadSample,
    build_load_profile,
    house_load_kw,
    samples_from_statistics,
)

_LOGGER = logging.getLogger(__name__)

FIVE = timedelta(minutes=5)
HOUR = timedelta(hours=1)

# kW of pv, grid, battery and wall connector over one 5-minute period
Powers = tuple[float, float, float, float]


def bucket_start(t: datetime) -> datetime:
    """The start of the 5-minute period holding t."""
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)


@dataclass
class History:
    """What the recorder gave at the last refresh."""

    profile: LoadProfile
    buckets: dict[datetime, Powers] = field(default_factory=dict)  # 5-min means, last 24 h
    hours: dict[datetime, Powers] = field(default_factory=dict)  # hourly means, last 26 h
    fetched_at: datetime | None = None
    hourly_rows: int = 0
    missing: tuple[str, ...] = ()  # power sensors without statistics


def empty_history(today: date) -> History:
    """A flat default profile until the recorder has answered (the core widens its spread)."""
    return History(build_load_profile((), today))


def _rows_kw(rows: list[Mapping[str, Any]], scale: float, invert: bool) -> list[dict[str, Any]]:
    """Recorder rows as local times and kW with the planner's sign."""
    sign = -1.0 if invert else 1.0
    out = []
    for row in rows:
        start, mean = row.get("start"), row.get("mean")
        if start is None or mean is None:
            continue
        if isinstance(start, (int, float)):
            local = dt_util.as_local(dt_util.utc_from_timestamp(float(start)))
        elif isinstance(start, datetime):
            local = dt_util.as_local(start)
        else:
            continue
        out.append({"start": local, "mean": float(mean) * scale * sign})
    return out


def _scale(options: Mapping[str, Any], key: str, metadata: Mapping[str, Any]) -> float:
    """kW per unit of a statistic: the recorder's unit, else the configured unit."""
    meta = metadata.get(options.get(key) or "")
    unit = meta[1].get("unit_of_measurement") if meta else None
    volts = float(options.get(CONF_VOLTS) or 240.0)
    scale = power_scale(unit, volts)
    if scale is None:
        scale = power_scale(options.get(unit_key(key), DEFAULT_POWER_UNITS[key]), volts) or 1.0
    return scale


def _converted(
    options: Mapping[str, Any],
    stats: Mapping[str, list[Mapping[str, Any]]],
    metadata: Mapping[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], tuple[str, ...]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    missing = []
    for key in POWER_KEYS:
        entity_id = options.get(key)
        raw = list(stats.get(entity_id, [])) if entity_id else []
        rows[key] = _rows_kw(
            raw, _scale(options, key, metadata), bool(options.get(invert_key(key)))
        )
        if not rows[key]:
            missing.append(key)
    if not rows[CONF_WALL_POWER] and rows[CONF_PV_POWER]:
        # No Wall Connector history: count it as idle. The house then includes any car
        # charging, which only makes the profile more cautious.
        rows[CONF_WALL_POWER] = [{"start": r["start"], "mean": 0.0} for r in rows[CONF_PV_POWER]]
    return rows, tuple(missing)


def build_history(
    options: Mapping[str, Any],
    hourly: Mapping[str, list[Mapping[str, Any]]],
    short: Mapping[str, list[Mapping[str, Any]]],
    metadata: Mapping[str, Any],
    today: date,
    now: datetime,
) -> History:
    """The load profile from hourly rows and the 5-minute buckets from short-term rows.
    Pure; runs in an executor job."""
    rows, missing = _converted(options, hourly, metadata)
    samples = samples_from_statistics(
        rows[CONF_PV_POWER],
        rows[CONF_GRID_POWER],
        rows[CONF_BATTERY_POWER],
        rows[CONF_WALL_POWER],
        pv_scale=1.0,
        grid_scale=1.0,
        battery_scale=1.0,
        wall_scale=1.0,
        minutes=60.0,
    )
    profile = build_load_profile(samples, today, days=HISTORY_DAYS)
    srows, _ = _converted(options, short, metadata)
    since = now - timedelta(hours=RECENT_HOURS + 2)
    return History(
        profile,
        _joined(srows, since),
        _joined(rows, since),
        now,
        len(rows[CONF_PV_POWER]),
        missing,
    )


def _joined(rows: Mapping[str, list[dict[str, Any]]], since: datetime) -> dict[datetime, Powers]:
    """The periods where all four powers have a mean."""
    index = [
        {r["start"]: r["mean"] for r in rows[key] if r["start"] >= since} for key in POWER_KEYS
    ]
    return {
        t: (index[0][t], index[1][t], index[2][t], index[3][t])
        for t in set(index[0]) & set(index[1]) & set(index[2]) & set(index[3])
    }


async def async_load_history(
    hass: HomeAssistant, options: Mapping[str, Any], now: datetime
) -> History:
    """Read the recorder (in its executor) and build the history (in an executor job)."""
    ids = {options[key] for key in POWER_KEYS if options.get(key)}
    today = now.date()
    start = dt_util.as_utc(
        datetime.combine(today - timedelta(days=HISTORY_DAYS), datetime.min.time(), now.tzinfo)
    )
    end = dt_util.as_utc(now)
    recorder = get_instance(hass)
    hourly = await recorder.async_add_executor_job(
        statistics_during_period, hass, start, end, ids, "hour", None, {"mean"}
    )
    short = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        end - timedelta(hours=RECENT_HOURS) - FIVE,
        end,
        ids,
        "5minute",
        None,
        {"mean"},
    )
    metadata = await recorder.async_add_executor_job(lambda: get_metadata(hass, statistic_ids=ids))
    return await hass.async_add_executor_job(
        build_history, dict(options), hourly, short, metadata, today, now
    )


class PowerBuffer:
    """Minute readings of the four powers, kept as 5-minute means for 26 hours, plus the
    recorder's 5-minute and hourly means for what the readings do not cover."""

    def __init__(self) -> None:
        self.samples: deque[tuple[datetime, Powers]] = deque(maxlen=60)
        self._live: dict[datetime, list[float]] = {}  # sums of pv, grid, battery, wall; count
        self.imported: dict[datetime, Powers] = {}  # 5-minute statistics
        self.hours: dict[datetime, Powers] = {}  # hourly statistics

    def load(self, history: History) -> None:
        self.imported = dict(history.buckets)
        self.hours = dict(history.hours)

    def add(self, t: datetime, powers: Powers) -> None:
        self.samples.append((t, powers))
        acc = self._live.setdefault(bucket_start(t), [0.0, 0.0, 0.0, 0.0, 0.0])
        for i, v in enumerate(powers):
            acc[i] += v
        acc[4] += 1
        cut = t - timedelta(hours=RECENT_HOURS + 2)
        for store in (self._live, self.imported, self.hours):
            for key in [k for k in store if k < cut]:
                del store[key]

    def mean(self, now: datetime, minutes: float) -> Powers | None:
        """Mean of the minute readings over the last ``minutes``."""
        since = now - timedelta(minutes=minutes)
        rows = [p for t, p in self.samples if t > since]
        if not rows:
            return None
        n = len(rows)
        return (
            sum(r[0] for r in rows) / n,
            sum(r[1] for r in rows) / n,
            sum(r[2] for r in rows) / n,
            sum(r[3] for r in rows) / n,
        )

    def _buckets(self) -> dict[datetime, Powers]:
        """5-minute means: live readings win over statistics."""
        out = dict(self.imported)
        for t, acc in self._live.items():
            n = acc[4]
            out[t] = (acc[0] / n, acc[1] / n, acc[2] / n, acc[3] / n)
        return out

    def house_recent(self, now: datetime) -> list[LoadSample]:
        """House load (pv + grid - battery - wall) over the last 24 hours: 5-minute means,
        and hourly means for whole hours no 5-minute mean covers."""
        since = now - timedelta(hours=RECENT_HOURS)
        buckets = self._buckets()
        out = [
            LoadSample(t, house_load_kw(*p), 5.0) for t, p in buckets.items() if since <= t < now
        ]
        covered = {t.replace(minute=0) for t in buckets}
        out += [
            LoadSample(t, house_load_kw(*p), 60.0)
            for t, p in self.hours.items()
            if since <= t and t + HOUR <= now and t not in covered
        ]
        return sorted(out, key=lambda s: s.start)

    def pv_today_kwh(
        self, now: datetime, fallback: Callable[[datetime, datetime], float] | None = None
    ) -> float:
        """Solar energy since local midnight: 5-minute means, else today's hourly means, else
        ``fallback(t0, t1)`` (the forecast) for what nothing measured covers."""
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        buckets = self._buckets()
        total, t = 0.0, midnight
        while t < now:
            end = min(t + FIVE, now)
            hours = (end - t).total_seconds() / 3600.0
            hour = t.replace(minute=0)
            if t in buckets:
                total += buckets[t][0] * hours
            elif hour in self.hours and hour + HOUR <= now:
                total += self.hours[hour][0] * hours
            elif fallback is not None:
                total += fallback(t, end)
            t += FIVE
        return total
