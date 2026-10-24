"""Charging schedule normalization and canonical hashing."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from typing import Any

SCHEDULE_KEYS = {"weekDays", "startTime", "duration", "location", "amperage", "enabled"}
WEEK_DAYS = {
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
}


def normalize_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    """Validate and copy one API schedule without exposing mutable input."""
    if not isinstance(schedule, dict):
        raise TypeError("Each charging schedule must be an object")
    required = SCHEDULE_KEYS - schedule.keys()
    if required:
        raise ValueError(f"Charging schedule is missing fields: {', '.join(sorted(required))}")
    days = schedule["weekDays"]
    if not isinstance(days, list) or not days or any(day not in WEEK_DAYS for day in days):
        raise ValueError("weekDays must be a non-empty list of weekday names")
    if len(set(days)) != len(days):
        raise ValueError("weekDays must not contain duplicates")
    for key, minimum, maximum in (("startTime", 0, 1439), ("duration", 1, 1440)):
        value = schedule[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer in range {minimum}..{maximum}")
    amperage = schedule["amperage"]
    if (isinstance(amperage, bool) or not isinstance(amperage, int)
            or amperage < 8 or amperage > 48 or amperage % 2):
        raise ValueError("amperage must be an even integer in range 8..48")
    if not isinstance(schedule["enabled"], bool):
        raise TypeError("enabled must be a boolean")
    location = schedule["location"]
    if not isinstance(location, dict) or not {
        "latitude",
        "longitude",
    }.issubset(location):
        raise ValueError("location must contain latitude and longitude")
    for key in ("latitude", "longitude"):
        value = location[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"location.{key} must be a finite number")
    if not -90 <= location["latitude"] <= 90 or not -180 <= location["longitude"] <= 180:
        raise ValueError("location coordinates are out of range")
    result = deepcopy(schedule)
    result["weekDays"] = list(days)
    result["location"] = dict(location)
    return result


def normalize_schedules(schedules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and copy a complete schedule list."""
    if not isinstance(schedules, list):
        raise TypeError("schedules must be a list")
    return [normalize_schedule(schedule) for schedule in schedules]


def canonical_schedule_hash(schedules: list[dict[str, Any]]) -> str:
    """Return a stable SHA-256 hash for a schedule list."""
    normalized = normalize_schedules(schedules)
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def canonical_schedule_contract_hash(schedules: list[dict[str, Any]]) -> str:
    """Hash only caller-controlled fields, ignoring server-managed metadata."""
    normalized = normalize_schedules(schedules)
    controlled = [
        {
            "weekDays": schedule["weekDays"],
            "startTime": schedule["startTime"],
            "duration": schedule["duration"],
            "location": {
                "latitude": schedule["location"]["latitude"],
                "longitude": schedule["location"]["longitude"],
            },
            "amperage": schedule["amperage"],
            "enabled": schedule["enabled"],
        }
        for schedule in normalized
    ]
    payload = json.dumps(controlled, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def extract_schedules(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract and validate schedules from the Rivian response envelope."""
    try:
        schedules = payload["data"]["getVehicle"]["chargingSchedules"]
    except (KeyError, TypeError) as err:
        raise ValueError("Rivian response did not contain charging schedules") from err
    return normalize_schedules(schedules)
