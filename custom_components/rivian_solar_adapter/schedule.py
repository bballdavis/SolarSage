"""Rivian charging schedule validation, hashing, and lease windows.

Ported from the earlier prototype (reference/ha-prototype-source/.../schedule.py).
This module has no Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
from typing import Any

WEEK_DAYS: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
SCHEDULE_KEYS = frozenset({"weekDays", "startTime", "duration", "location", "amperage", "enabled"})

AMPS_MIN = 8
AMPS_MAX = 48
AMPS_STEP = 2
MINUTES_PER_DAY = 1440
# Provider schedules are read leniently: they are restored exactly as read.
PROVIDER_MAX_DURATION = 7 * MINUTES_PER_DAY
PROVIDER_MAX_AMPS = 80
# Locations within this many degrees (about 10 m) are the same place, so
# provider rounding or float storage is never mistaken for a change.
LOCATION_TOLERANCE_DEG = 1e-4
# Coordinates written in a lease window.
WRITE_COORD_DIGITS = 6


def normalize_schedule(schedule: dict[str, Any], *, strict: bool) -> dict[str, Any]:
    """Validate one schedule and return a copy.

    ``strict`` applies the limits this adapter writes (even 8-48 A, at most one
    day). Non-strict validation is for schedules read from the provider, which
    are only restored, never modified.
    """
    if not isinstance(schedule, dict):
        raise TypeError("Each charging schedule must be an object")
    if missing := SCHEDULE_KEYS - schedule.keys():
        raise ValueError(f"Charging schedule is missing fields: {', '.join(sorted(missing))}")
    days = schedule["weekDays"]
    if not isinstance(days, list) or not days or any(day not in WEEK_DAYS for day in days):
        raise ValueError("weekDays must be a non-empty list of weekday names")
    if len(set(days)) != len(days):
        raise ValueError("weekDays must not contain duplicates")
    max_duration = MINUTES_PER_DAY if strict else PROVIDER_MAX_DURATION
    for key, minimum, maximum in (
        ("startTime", 0, MINUTES_PER_DAY - 1),
        ("duration", 1, max_duration),
    ):
        value = schedule[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer in range {minimum}..{maximum}")
    amperage = schedule["amperage"]
    if isinstance(amperage, bool) or not isinstance(amperage, int):
        raise ValueError("amperage must be an integer")
    if strict:
        if not AMPS_MIN <= amperage <= AMPS_MAX or amperage % AMPS_STEP:
            raise ValueError(f"amperage must be an even integer in range {AMPS_MIN}..{AMPS_MAX}")
    elif not 0 <= amperage <= PROVIDER_MAX_AMPS:
        raise ValueError(f"amperage must be in range 0..{PROVIDER_MAX_AMPS}")
    if not isinstance(schedule["enabled"], bool):
        raise TypeError("enabled must be a boolean")
    location = schedule["location"]
    if not isinstance(location, dict) or not {"latitude", "longitude"}.issubset(location):
        raise ValueError("location must contain latitude and longitude")
    for key in ("latitude", "longitude"):
        value = location[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"location.{key} must be a finite number")
    if not -90 <= location["latitude"] <= 90 or not -180 <= location["longitude"] <= 180:
        raise ValueError("location coordinates are out of range")
    result = deepcopy(schedule)
    result["weekDays"] = list(days)
    result["location"] = dict(location)
    return result


def normalize_schedules(schedules: Any, *, strict: bool) -> list[dict[str, Any]]:
    """Validate and copy a complete schedule list, preserving its order."""
    if not isinstance(schedules, list):
        raise TypeError("schedules must be a list")
    return [normalize_schedule(schedule, strict=strict) for schedule in schedules]


@dataclass(frozen=True)
class Fingerprint:
    """Identity of a schedule list for comparing provider states.

    ``key`` hashes every controlled field except location, with weekdays and
    the list itself in canonical order, so neither order nor coordinates can
    be recovered from it or make two equal lists differ. Locations are kept
    separately, in the same canonical order, and compared within
    ``LOCATION_TOLERANCE_DEG``.
    """

    key: str
    locations: tuple[tuple[float, float], ...]

    def matches(self, other: Fingerprint | None) -> bool:
        """Whether two lists describe the same schedules."""
        if other is None or self.key != other.key or len(self.locations) != len(other.locations):
            return False
        return all(
            abs(a[0] - b[0]) <= LOCATION_TOLERANCE_DEG
            and abs(a[1] - b[1]) <= LOCATION_TOLERANCE_DEG
            for a, b in zip(self.locations, other.locations, strict=True)
        )

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "locations": [list(location) for location in self.locations]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fingerprint:
        return cls(data["key"], tuple((float(lat), float(lon)) for lat, lon in data["locations"]))


def fingerprint(schedules: list[dict[str, Any]]) -> Fingerprint:
    """Fingerprint an already normalized list."""
    items = []
    for schedule in schedules:
        controlled = {
            "weekDays": sorted(schedule["weekDays"], key=WEEK_DAYS.index),
            "startTime": schedule["startTime"],
            "duration": schedule["duration"],
            "amperage": schedule["amperage"],
            "enabled": schedule["enabled"],
        }
        location = (
            float(schedule["location"]["latitude"]),
            float(schedule["location"]["longitude"]),
        )
        items.append((json.dumps(controlled, sort_keys=True, separators=(",", ":")), location))
    items.sort()
    payload = json.dumps([item[0] for item in items], separators=(",", ":"))
    return Fingerprint(
        hashlib.sha256(payload.encode()).hexdigest(), tuple(item[1] for item in items)
    )


def provider_fingerprint(schedules: Any) -> Fingerprint:
    """Normalize a provider list leniently and fingerprint it."""
    return fingerprint(normalize_schedules(schedules, strict=False))


def to_provider_input(schedules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce normalized schedules to the fields the write mutation accepts."""
    return [
        {
            "weekDays": list(schedule["weekDays"]),
            "startTime": schedule["startTime"],
            "duration": schedule["duration"],
            "location": {
                "latitude": schedule["location"]["latitude"],
                "longitude": schedule["location"]["longitude"],
            },
            "amperage": schedule["amperage"],
            "enabled": schedule["enabled"],
        }
        for schedule in schedules
    ]


def applied_amps(requested: float | int) -> int:
    """Map a requested current to a schedule amperage: even, 8-48 A, rounded down."""
    amps = int(math.floor(requested))
    amps -= amps % AMPS_STEP
    return max(AMPS_MIN, min(AMPS_MAX, amps))


def lease_window(
    local_now: datetime,
    *,
    active: bool,
    lease_minutes: int,
    skew_minutes: int,
    amps: int,
    location: dict[str, float],
) -> dict[str, Any]:
    """Build the single schedule the adapter writes while it owns the vehicle.

    ``local_now`` must be timezone-aware in the vehicle's local time, because
    the provider schedule uses the local weekday and minutes after midnight.
    Arithmetic on it is wall-clock arithmetic, which is what the vehicle uses.

    An active lease covers now with ``skew_minutes`` to spare at both ends, so a
    vehicle clock that differs from Home Assistant's by up to that much still
    sees it cover now. It ends ``lease_minutes`` plus the skew from now. If it
    is not renewed it lapses, and its next occurrence is a week later.

    A parked window ended ``skew_minutes`` ago, so no window covers now and the
    vehicle waits. It also recurs only a week later.
    """
    if local_now.tzinfo is None:
        raise ValueError("local_now must be timezone-aware")
    local_now = local_now.replace(second=0, microsecond=0)
    if active:
        start = local_now - timedelta(minutes=skew_minutes)
        duration = lease_minutes + 2 * skew_minutes
    else:
        duration = lease_minutes
        start = local_now - timedelta(minutes=skew_minutes + duration)
    return {
        "weekDays": [WEEK_DAYS[start.weekday()]],
        "startTime": start.hour * 60 + start.minute,
        "duration": duration,
        "location": {
            "latitude": round(float(location["latitude"]), WRITE_COORD_DIGITS),
            "longitude": round(float(location["longitude"]), WRITE_COORD_DIGITS),
        },
        "amperage": amps,
        "enabled": True,
    }
