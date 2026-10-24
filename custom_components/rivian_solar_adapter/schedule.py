"""Rivian charging schedule validation, hashing, and lease windows.

Ported from the earlier prototype (reference/ha-prototype-source/.../schedule.py).
This module has no Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

from copy import deepcopy
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
# Coordinates are compared at about 1 m so provider rounding is not a "change".
HASH_COORD_DIGITS = 5
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


def contract_hash(schedules: list[dict[str, Any]]) -> str:
    """Hash the caller-controlled fields of an already normalized list.

    Server-managed extras are ignored, so a readback that adds metadata still
    matches the write. Order is significant.
    """
    controlled = [
        {
            "weekDays": schedule["weekDays"],
            "startTime": schedule["startTime"],
            "duration": schedule["duration"],
            "location": {
                "latitude": round(float(schedule["location"]["latitude"]), HASH_COORD_DIGITS),
                "longitude": round(float(schedule["location"]["longitude"]), HASH_COORD_DIGITS),
            },
            "amperage": schedule["amperage"],
            "enabled": schedule["enabled"],
        }
        for schedule in schedules
    ]
    payload = json.dumps(controlled, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


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


def provider_hash(schedules: Any) -> str:
    """Normalize a provider list leniently and return its contract hash."""
    return contract_hash(normalize_schedules(schedules, strict=False))


def applied_amps(requested: float | int) -> int:
    """Map a requested current to a schedule amperage: even, 8-48 A, rounded down."""
    amps = int(math.floor(requested))
    amps -= amps % AMPS_STEP
    return max(AMPS_MIN, min(AMPS_MAX, amps))


def lease_window(
    now: datetime,
    *,
    active: bool,
    lease_minutes: int,
    skew_minutes: int,
    amps: int,
    location: dict[str, float],
) -> dict[str, Any]:
    """Build the single schedule the adapter writes while it owns the vehicle.

    ``now`` must be timezone-aware in the vehicle's local time, because the
    provider schedule uses local weekday and minutes after midnight.

    An active lease covers now: it starts ``skew_minutes`` ago (tolerating a
    vehicle clock that runs behind) and ends ``lease_minutes`` from now. If it is
    not renewed it lapses, and its next occurrence is a week later.

    A parked window ended ``skew_minutes`` ago, so no window covers now and the
    vehicle waits. It also recurs only a week later.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.replace(second=0, microsecond=0)
    if active:
        start = now - timedelta(minutes=skew_minutes)
        duration = skew_minutes + lease_minutes
    else:
        duration = lease_minutes
        start = now - timedelta(minutes=skew_minutes + duration)
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
