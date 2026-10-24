"""Read-only input diagnostics for the solar charging status contract."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def measurement(
    *,
    label: str,
    entity_id: str | None,
    unit: str,
    age_seconds: float | None,
    max_age_seconds: int,
    status: str,
    normalized_value: float | bool | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "label": label,
        "entity_id": entity_id,
        "unit": unit,
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "max_age_seconds": max_age_seconds,
        "status": status,
    }
    if normalized_value is not None:
        result["normalized_value"] = normalized_value
    return result


def state_age(state: Any, now: datetime | None = None) -> float | None:
    if state is None:
        return None
    stamp = getattr(state, "last_reported", None) or state.last_updated
    return ((now or datetime.now(UTC)) - stamp).total_seconds()
