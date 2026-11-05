"""Diagnostics for the Solar EV planner.

Entity IDs, settings, readings and decisions are included; coordinates and identifiers
(VIN, email, tokens) are redacted at any depth. The planner never reads a device tracker's
attributes, only its state (home or not).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data
from homeassistant.util import dt as dt_util

from .const import REDACT_KEYS
from .coordinator import PlannerConfigEntry


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PlannerConfigEntry
) -> dict[str, Any]:
    """The mapping, settings, the last cycle and the planner's memory."""
    coordinator = entry.runtime_data
    data = coordinator.data
    decision = data.decision if data else None
    now = dt_util.now()
    out: dict[str, Any] = {
        "entry": {"title": entry.title, "options": dict(entry.options)},
        "cars": {car_id: dict(car) for car_id, car in coordinator.cars.items()},
        "price_windows": coordinator.windows,
        "settings": coordinator.settings,
        "car_settings": coordinator.car_settings,
        "window_prices": coordinator.window_prices,
        "healthy": coordinator.healthy(now),
        "commanding": coordinator.active,
        "last_success": coordinator.last_success,
        "last_error": coordinator.last_error,
        "history": {
            "fetched_at": coordinator.history.fetched_at,
            "profile_days": coordinator.history.profile.n_days,
            "hourly_rows": coordinator.history.hourly_rows,
            "missing": list(coordinator.history.missing),
            "error": coordinator.history_error,
            "recent_buckets": len(coordinator.buffer.imported),
        },
        "schedules": coordinator.schedules,
        "pv_days": [asdict(d) for d in coordinator.pv_days],
        "runtime": {car_id: rt.to_dict() for car_id, rt in coordinator.runtime.items()},
        "planner_state": coordinator.planner.state_dict(),
        "tesla_spend": coordinator.spend.attributes(now),
    }
    if data is not None:
        out["last_cycle"] = {
            "now": data.now,
            "mode": data.mode,
            "reason": data.reason,
            "site": asdict(data.site),
            "identification": asdict(data.identification),
            "pv": data.pv,
            "cars": {car_id: asdict(view) for car_id, view in data.cars.items()},
        }
    if decision is not None:
        out["decision"] = {
            "mode": decision.mode,
            "car_id": decision.car_id,
            "reason": decision.reason,
            "shadow": decision.shadow,
            "safety": decision.safety,
            "commands": [asdict(c) for c in decision.commands],
            "next_change": asdict(decision.next_change) if decision.next_change else None,
            "debug": dict(decision.debug),
            "trajectory_points": len(decision.trajectory),
        }
    return async_redact_data(_jsonable(out), REDACT_KEYS)
