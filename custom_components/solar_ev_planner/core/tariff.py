"""Electricity plans: weekly on/off schedules (HA Schedule helpers) plus prices.

The owner paints each price level as a Schedule helper (for example "Free" and "Peak") and sets
a price for it; every other hour costs the normal price, and exported solar earns the buyback.
``WeeklySchedule.from_ha`` reads a helper's weekly config (the ``schedule.get_schedule``
response or the helper's stored config); ``Tariff`` turns schedules and prices into the price
at any time. Free nights, time of use, flat and free-weekend plans are all just settings:

    free = WeeklySchedule.daily("20:00", "06:00")
    Tariff(0.32, 0.0, (PriceWindow(free, 0.0, label="free"),))      # the owner's plan today
    Tariff(0.32, 0.0)                                               # flat
    Tariff(0.11, 0.05, (PriceWindow(WeeklySchedule.daily("16:00", "21:00"), 0.38, "peak"),))
    Tariff(0.32, 0.0, (PriceWindow(WeeklySchedule.days("00:00", "24:00", (5, 6)), 0.0),))

Times are local wall-clock times; datetimes may be naive local or aware local (only their
wall-clock fields are read).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Protocol

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SCAN = timedelta(minutes=5)  # resolution of next_change (schedule blocks are whole minutes)


def minutes(hm: str | time) -> int:
    """'HH:MM', 'HH:MM:SS' or a time -> minutes after midnight; '24:00' and '23:59:59' (the
    HA Schedule helper's end of day) both mean 1440."""
    if isinstance(hm, time):
        if (hm.hour, hm.minute, hm.second) == (23, 59, 59):
            return 1440
        return hm.hour * 60 + hm.minute
    parts = [int(x) for x in hm.split(":")]
    h, m = parts[0], parts[1] if len(parts) > 1 else 0
    s = parts[2] if len(parts) > 2 else 0
    if (h, m, s) == (23, 59, 59):
        return 1440
    return h * 60 + m


class TariffLike(Protocol):
    """What the planner reads from a tariff (``Tariff`` below, or any equivalent)."""

    def import_price(self, t: datetime) -> float: ...

    def export_price(self, t: datetime) -> float: ...

    def is_free(self, t: datetime) -> bool: ...


@dataclass(frozen=True)
class WeeklySchedule:
    """On-blocks per weekday: ``blocks[d]`` = ((start_min, end_min), ...) for weekday d
    (Monday = 0), 0 <= start < end <= 1440. A block that crosses midnight is stored as two
    blocks (the evening part on its own day, the morning part on the next)."""

    blocks: tuple[tuple[tuple[int, int], ...], ...]

    @classmethod
    def daily(cls, start: str, end: str) -> WeeklySchedule:
        """The same window every day; ``end <= start`` wraps past midnight."""
        return cls.days(start, end, tuple(range(7)))

    @classmethod
    def days(cls, start: str, end: str, weekdays: Iterable[int]) -> WeeklySchedule:
        """A window that STARTS on the given weekdays (Monday = 0); wraps when end <= start."""
        a, b = minutes(start), minutes(end)
        out: list[list[tuple[int, int]]] = [[] for _ in range(7)]
        for d in weekdays:
            if a < b:
                out[d].append((a, b))
            else:
                if a < 1440:
                    out[d].append((a, 1440))
                if b > 0:
                    out[(d + 1) % 7].append((0, b))
        return cls(tuple(tuple(sorted(x)) for x in out))

    @classmethod
    def from_ha(cls, config: Mapping[str, Any]) -> WeeklySchedule:
        """An HA Schedule helper's weekly config, e.g. ``{"monday": [{"from": "20:00:00",
        "to": "23:59:59"}], "tuesday": [{"from": "00:00:00", "to": "06:00:00"}, ...]}``.
        Also accepts the ``schedule.get_schedule`` response for one entity. Touching blocks
        (20:00-24:00 then 00:00-06:00) behave as one night."""
        out: list[list[tuple[int, int]]] = [[] for _ in range(7)]
        for d, name in enumerate(WEEKDAYS):
            for blk in config.get(name, ()) or ():
                a, b = minutes(str(blk["from"])), minutes(str(blk["to"]))
                if b > a:
                    out[d].append((a, b))
        return cls(tuple(tuple(sorted(x)) for x in out))

    @classmethod
    def never(cls) -> WeeklySchedule:
        return cls(((),) * 7)

    def contains(self, t: datetime) -> bool:
        m = t.hour * 60 + t.minute + t.second / 60.0
        return any(a <= m < b for a, b in self.blocks[t.weekday()])


@dataclass(frozen=True)
class PriceWindow:
    """A painted price level: while ``schedule`` is on, import costs ``import_price`` and
    export earns ``export_price`` (None = the tariff's buyback)."""

    schedule: WeeklySchedule
    import_price: float
    label: str = ""
    export_price: float | None = None


@dataclass(frozen=True)
class Tariff:
    """$/kWh. The first window that is on wins; otherwise the normal price applies."""

    price: float = 0.32  # normal import price outside every window
    buyback: float = 0.0  # what exported solar earns outside windows with their own
    windows: tuple[PriceWindow, ...] = ()
    default_label: str = "paid"

    @classmethod
    def free_nights(
        cls, start: str = "20:00", end: str = "06:00", price: float = 0.32, buyback: float = 0.0
    ) -> Tariff:
        return cls(price, buyback, (PriceWindow(WeeklySchedule.daily(start, end), 0.0, "free"),))

    def _window(self, t: datetime) -> PriceWindow | None:
        for w in self.windows:
            if w.schedule.contains(t):
                return w
        return None

    def import_price(self, t: datetime) -> float:
        w = self._window(t)
        return w.import_price if w is not None else self.price

    def export_price(self, t: datetime) -> float:
        w = self._window(t)
        if w is not None and w.export_price is not None:
            return w.export_price
        return self.buyback

    def is_free(self, t: datetime) -> bool:
        return self.import_price(t) <= 0.0

    def label(self, t: datetime) -> str:
        w = self._window(t)
        return (w.label or "window") if w is not None else self.default_label

    def min_price(self) -> float:
        return min([self.price] + [w.import_price for w in self.windows])

    def next_change(self, t: datetime, horizon: timedelta = timedelta(days=8)) -> datetime | None:
        """First time after t (5-min resolution) where the import or export price changes."""
        key = (self.import_price(t), self.export_price(t))
        u = t.replace(second=0, microsecond=0) - timedelta(minutes=t.minute % 5) + SCAN
        while u < t + horizon:
            if (self.import_price(u), self.export_price(u)) != key:
                return u
            u += SCAN
        return None


def next_flip(t: datetime, pred, horizon: timedelta) -> datetime | None:
    """First time after t (5-min grid) where ``pred`` differs from ``pred(t)``."""
    v0 = pred(t)
    u = t.replace(second=0, microsecond=0) - timedelta(minutes=t.minute % 5) + SCAN
    end = t + horizon
    while u < end:
        if pred(u) != v0:
            return u
        u += SCAN
    return None
