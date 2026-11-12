"""What the planner's dashboard card shows, as one JSON-safe snapshot.

The card subscribes to it over the websocket (``websocket_api``) and gets a fresh one after
every planning cycle and every settings change. Everything here is already in the entities;
the snapshot adds what entities cannot carry well: the half-hourly forecast, the battery path,
the free hours ahead and the map from each setting to its entity, so the card needs no
configuration. Charts of the past (solar so far, what the car drew) come from the recorder:
``sources`` names the sensors and how to turn their states into kW.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    CAUTION_OPTIONS,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_CHARGER_MAX_A,
    CONF_CONTROL_STATE,
    CONF_FREE_HOURS,
    CONF_GRID_POWER,
    CONF_KIND,
    CONF_MAX_A,
    CONF_NAME,
    CONF_PV_POWER,
    CONF_VOLTS,
    CONF_WALL_CURRENT,
    CONF_WALL_POWER,
    DEFAULT_POWER_UNITS,
    DOMAIN,
    POWER_SCALE,
    invert_key,
    unit_key,
)
from .coordinator import CarView, PlannerCoordinator
from .core.types import CAUTION_LEVELS

# How far ahead the free hours are looked for, and in what steps.
FREE_LOOKAHEAD = timedelta(hours=30)
FREE_STEP = timedelta(minutes=15)
# How far back the charts show the free hours already passed.
FREE_LOOKBACK = timedelta(hours=24)


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def _r(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def entity_map(hass: HomeAssistant, coordinator: PlannerCoordinator) -> dict[str, Any]:
    """Each planner entity's key -> entity ID: ``planner`` for the planner's own, ``cars``
    per car. Unique IDs are ``<entry>_<key>`` and ``<entry>_<car>_<key>``."""
    entry_id = coordinator.config_entry.entry_id
    planner: dict[str, str] = {}
    cars: dict[str, dict[str, str]] = {car_id: {} for car_id in coordinator.cars}
    registry = er.async_get(hass)
    for entry in er.async_entries_for_config_entry(registry, entry_id):
        if entry.platform != DOMAIN or not entry.translation_key:
            continue
        key = entry.translation_key
        rest = entry.unique_id.removeprefix(f"{entry_id}_")
        car_id = next((c for c in cars if rest == f"{c}_{key}"), None)
        if car_id is not None:
            cars[car_id][key] = entry.entity_id
        elif rest == key:
            planner[key] = entry.entity_id
    return {"planner": planner, "cars": cars}


def _source(hass: HomeAssistant, options: dict[str, Any], key: str) -> dict[str, Any] | None:
    """A power sensor and the factor that turns its state into the planner's kW."""
    entity_id = options.get(key)
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    own = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None
    scale = POWER_SCALE.get(own) if isinstance(own, str) else None
    if scale is None:
        scale = POWER_SCALE.get(options.get(unit_key(key), DEFAULT_POWER_UNITS[key]), 1.0)
    return {"entity_id": entity_id, "scale": -scale if options.get(invert_key(key)) else scale}


def _free_windows(
    coordinator: PlannerCoordinator, now: datetime, back: timedelta = timedelta(0)
) -> list[dict[str, str]]:
    """The free hours from now to FREE_LOOKAHEAD, as start and end times. With ``back``, the
    ones from ``now - back`` up to now instead (the charts' past hours)."""
    tariff, problem = coordinator._tariff()  # noqa: SLF001 - the same tariff the cycle uses
    if tariff is None or problem is not None:
        return []
    begin, end = (now - back, now) if back else (now, now + FREE_LOOKAHEAD)
    windows: list[dict[str, str]] = []
    start: datetime | None = None
    t = begin.replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % 15)
    while t <= end:
        free = tariff.is_free(t) and (not back or t < end)
        if free and start is None:
            start = max(t, begin)
        elif not free and start is not None:
            windows.append({"start": start.isoformat(), "end": t.isoformat()})
            start = None
        t += FREE_STEP
    if start is not None:
        windows.append(
            {"start": start.isoformat(), "end": (min(t, end) if back else t).isoformat()}
        )
    return windows


def _free_hours_problem(
    coordinator: PlannerCoordinator, windows: list[dict[str, str]]
) -> str | None:
    """Why the card has no free hours to show, in words the owner can act on."""
    entity_id = coordinator.options.get(CONF_FREE_HOURS)
    if not entity_id:
        return "No free-hours schedule is mapped. Choose one under SolarSage → Configure."
    if coordinator.hass.states.get(entity_id) is None:
        return f"The free-hours schedule {entity_id} does not exist in Home Assistant."
    if coordinator.schedules.get(entity_id) is None:
        return f"The free-hours schedule {entity_id} could not be read yet."
    if not windows:
        return f"The schedule {entity_id} has no free hours in the next 30 hours."
    return None


def _setup(hass: HomeAssistant, coordinator: PlannerCoordinator) -> dict[str, Any]:
    """What is installed around the planner, so the card can say what is missing."""
    entries = hass.config_entries.async_entries(DOMAIN)
    return {
        "planner_entries": sum(1 for e in entries if "vehicle_id" not in e.data),
        "rivian_controllers": [
            {"title": e.title, "state": e.state.value} for e in entries if "vehicle_id" in e.data
        ],
        "legacy_rivian": bool(hass.config_entries.async_entries("rivian_schedule_control")),
        "car_kinds": sorted({str(car[CONF_KIND]) for car in coordinator.cars.values()}),
    }


def _car(coordinator: PlannerCoordinator, car_id: str, view: CarView | None) -> dict[str, Any]:
    car = coordinator.cars[car_id]
    out: dict[str, Any] = {
        "car_id": car_id,
        "name": car[CONF_NAME],
        "kind": car[CONF_KIND],
        "control_state_entity": car.get(CONF_CONTROL_STATE),
        # the current it gets in free hours: its own maximum, capped by the Wall Connector
        "max_a": min(int(car[CONF_MAX_A]), int(coordinator.options.get(CONF_CHARGER_MAX_A, 48))),
        "settings": dict(coordinator.car_settings.get(car_id, {})),
    }
    if view is None:
        return out
    standing = view.standing
    o = view.outlook
    return out | {
        "soc": _r(view.soc_pct, 1),
        "limit": _r(view.limit_pct, 0),
        "plugged": view.plugged,
        "home": view.home,
        "on_charger": view.on_charger,
        "stale": view.stale,
        "planned_a": view.planned_a,
        "want_a": view.want_a,
        "command": view.command,
        "reason": view.reason,
        "commanded_a": view.commanded_a,
        "charging_wanted": view.charging_wanted,
        "standing": (
            {
                "charging": standing.charging,
                "amps": standing.amps,
                "schedule_control_state": standing.schedule_control_state,
            }
            if standing
            else None
        ),
        "waiting": view.blocked,
        "last_error": view.last_error,
        "outlook": (
            {
                "summary": o.summary,
                "need_kwh": _r(o.need_kwh, 1),
                "target": _r(o.target_pct, 0),
                "by": _iso(o.by),
                "solar_kwh": _r(o.solar_kwh, 1),
                "free_kwh": _r(o.free_kwh, 1),
                "paid_kwh": _r(o.paid_kwh, 1),
                "short_kwh": _r(o.short_kwh, 1),
            }
            if o
            else None
        ),
    }


def snapshot(hass: HomeAssistant, coordinator: PlannerCoordinator) -> dict[str, Any]:
    """The card's view of the planner now."""
    now = dt_util.now()
    data = coordinator.data
    decision = data.decision if data else None
    options = coordinator.options
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = midnight + timedelta(days=2)
    site = data.site if data else None
    nxt = decision.next_change if decision else None
    caution = str(coordinator.settings.get("caution"))
    free_windows = _free_windows(coordinator, now)
    return {
        "now": now.isoformat(),
        "planned_at": _iso(data.now if data else None),
        "mode": data.mode if data else None,
        "reason": data.reason if data else None,
        "enabled": coordinator.enabled,
        "shadow": coordinator.shadow,
        "active": coordinator.active,
        "healthy": coordinator.healthy(now),
        "last_success": _iso(coordinator.last_success),
        "last_error": coordinator.last_error,
        "history_error": coordinator.history_error,
        "safety": decision.safety if decision else None,
        "car_on_charger": data.identification.car_id if data else None,
        "identification": data.identification.reason if data else None,
        "tonight_car": decision.debug.get("tonight_car") if decision else None,
        "free_until": _iso(decision.free_until if decision else None),
        "next_change": (
            {"at": nxt.at.isoformat(), "amps": nxt.amps, "why": nxt.why} if nxt else None
        ),
        "volts": float(options.get(CONF_VOLTS) or 240.0),
        "site": (
            {
                "pv_kw": _r(site.pv_kw),
                "grid_kw": _r(site.grid_kw),
                "battery_kw": _r(site.battery_kw),
                "wall_kw": _r(site.wall_kw),
                "house_kw": _r(site.house_kw),
                "soc": _r(site.soc_pct, 1),
                "capacity_kwh": _r(site.capacity_kwh, 1),
                "wall_current": _r(site.wall_current_a, 1),
                "wall_connected": site.wall_connected,
                "contactor_closed": site.contactor_closed,
                "problem": site.problem,
            }
            if site
            else None
        ),
        "pv": dict(data.pv) if data else {},
        "forecast": [
            {
                "t": s.start.isoformat(),
                "p10": _r(s.p10, 3),
                "p50": _r(s.p50, 3),
                "p90": _r(s.p90, 3),
            }
            for s in coordinator.forecast
            if midnight <= s.start < horizon
        ],
        "trajectory": [
            {
                "t": p.t.isoformat(),
                "soc": _r(p.soc_pct, 1),
                "expected": _r(p.soc_expected_pct, 1),
                "no_car": _r(p.soc_no_car_pct, 1),
                "pv_kw": _r(p.pv_expected_kw),
                "car_kw": _r(p.car_kw),
                "price": _r(p.price, 3),
            }
            for p in (decision.trajectory if decision else ())
        ],
        "free_windows": free_windows,
        "free_windows_past": _free_windows(coordinator, now, FREE_LOOKBACK),
        "free_hours_entity": options.get(CONF_FREE_HOURS),
        "free_hours_problem": _free_hours_problem(coordinator, free_windows),
        "setup": _setup(hass, coordinator),
        "settings": dict(coordinator.settings),
        "caution_levels": {key: CAUTION_LEVELS[label] for key, label in CAUTION_OPTIONS.items()},
        "caution_quantile": CAUTION_LEVELS.get(CAUTION_OPTIONS.get(caution, ""), None),
        "cars": [
            _car(coordinator, car_id, data.cars.get(car_id) if data else None)
            for car_id in coordinator.cars
        ],
        "spend": (
            coordinator.spend.attributes(now) | {"spent": round(coordinator.spend.spent(now), 3)}
            if coordinator.spend.teslas
            else None
        ),
        "sources": {
            "pv": _source(hass, options, CONF_PV_POWER),
            "wall": _source(hass, options, CONF_WALL_POWER),
            "grid": _source(hass, options, CONF_GRID_POWER),
            "battery": _source(hass, options, CONF_BATTERY_POWER),
            "soc": (
                {"entity_id": options[CONF_BATTERY_SOC], "scale": 1.0}
                if options.get(CONF_BATTERY_SOC)
                else None
            ),
            "wall_current": (
                {"entity_id": options[CONF_WALL_CURRENT], "scale": 1.0}
                if options.get(CONF_WALL_CURRENT)
                else None
            ),
        },
        "entities": entity_map(hass, coordinator),
    }
