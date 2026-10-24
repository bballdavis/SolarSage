"""Strict parsing helpers for externally-provided solar forecasts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import math
from typing import Any


def forecast_energy_kwh(value: Any, unit: Any) -> float | None:
    """Return a finite, non-negative Wh/kWh forecast as kWh."""
    if isinstance(value, bool) or str(value).strip() in {"", "unknown", "unavailable"}:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric < 0:
        return None
    normalized_unit = str(unit).strip()
    if normalized_unit == "Wh":
        return numeric / 1000
    if normalized_unit == "kWh":
        return numeric
    return None


def forecast_attribute_value(state: Any, field: str) -> Any | None:
    """Return the selected field without falling back from a missing attribute."""
    if not isinstance(field, str) or field not in (
        "state",
        "estimate",
        "estimate10",
        "estimate90",
    ):
        return None
    if field == "state":
        return getattr(state, "state", None)
    attributes = getattr(state, "attributes", {})
    return attributes.get(field) if isinstance(attributes, Mapping) else None


def forecast_source_age_seconds(
    value: Any, now: datetime | None = None
) -> float | None:
    """Return an age only for a timezone-aware, non-future source timestamp."""
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return None
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None or reference.utcoffset() is None:
        return None
    age = (reference - timestamp).total_seconds()
    return age if math.isfinite(age) and age >= 0 else None
