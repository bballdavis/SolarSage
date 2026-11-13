"""Reading Home Assistant states for the planner, and deciding which car is on the charger.

Freshness: an unchanged number keeps an old ``last_reported`` timestamp (the Sol-Ark battery
level, the Wall Connector's power at 0), so a value is never stale because of its own age. It
is stale when it is unavailable or unknown, or when the integration that provides it has gone
quiet: its config entry is not loaded, or (for the site's power sensors, which always move
together) none of them has reported for ``SITE_QUIET_AFTER``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_HOME,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State, split_entity_id
from homeassistant.helpers import entity_registry as er

from .const import OVERRIDE_AUTO, power_scale
from .core import PriceWindow, Tariff, WeeklySchedule
from .core.tariff import WEEKDAYS

UNAVAILABLE_STATES = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN, "", "none", "None"})


def available(state: State | None) -> bool:
    """Whether a state holds a value."""
    return state is not None and state.state not in UNAVAILABLE_STATES


def commandable(state: State | None) -> bool:
    """Whether a command entity can take a command: it exists and is not unavailable. An
    unknown state is fine: Tesla Fleet with its polling off has not read the car since a
    restart, but its switch and number still send (the planner refreshes it first)."""
    return state is not None and state.state != STATE_UNAVAILABLE


def state_float(state: State | None) -> float | None:
    """A state's numeric value, or None when unavailable or not a number."""
    if not available(state):
        return None
    assert state is not None
    try:
        value = float(state.state)
    except TypeError, ValueError:
        return None
    return value if value == value else None  # NaN


def state_on(state: State | None) -> bool | None:
    """on -> True, any other known state -> False, unavailable or unknown -> None."""
    if not available(state):
        return None
    assert state is not None
    return state.state == STATE_ON


def state_home(state: State | None) -> bool | None:
    """Whether a location entity says home: a device tracker or person at home, or a binary
    sensor that is on. None while unavailable or unknown."""
    if not available(state):
        return None
    assert state is not None
    if split_entity_id(state.entity_id)[0] == "binary_sensor":
        return state.state == STATE_ON
    return state.state == STATE_HOME


def power_kw(state: State | None, unit: str, invert: bool, volts: float = 240.0) -> float | None:
    """A power sensor in kW with the planner's sign. The sensor's own unit wins over the
    configured one when it has a power unit; a current sensor (A) counts at ``volts``."""
    value = state_float(state)
    if value is None:
        return None
    assert state is not None
    scale = power_scale(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT), volts)
    if scale is None:
        scale = power_scale(unit, volts) or 1.0
    value *= scale
    return -value if invert else value


def integration_quiet(hass: HomeAssistant, entity_id: str | None) -> bool:
    """Whether the integration behind an entity has stopped: its config entry is not loaded.

    An entity without a registry entry or config entry (a template, a YAML sensor) is never
    quiet by this test; it turns unavailable instead.
    """
    if not entity_id:
        return False
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or entry.config_entry_id is None:
        return False
    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
    return config_entry is None or config_entry.state is not ConfigEntryState.LOADED


def last_report(states: Iterable[State | None]) -> datetime | None:
    """The latest time any of the states reported."""
    times = [s.last_reported for s in states if s is not None]
    return max(times) if times else None


# --------------------------------------------------------------------------- the car on the charger


@dataclass(frozen=True)
class CarSignals:
    """What Home Assistant says about one car: its own cable or charger connection, and
    whether it is home (True when no location entity is mapped)."""

    car_id: str
    plugged: bool | None
    home: bool | None


@dataclass(frozen=True)
class Identification:
    """The car the planner may command, and why."""

    car_id: str | None
    reason: str
    candidates: tuple[str, ...] = ()
    wall_connected: bool | None = None
    override: str = OVERRIDE_AUTO


def identify_car(
    cars: Sequence[CarSignals], wall_connected: bool | None, override: str = OVERRIDE_AUTO
) -> Identification:
    """Which car is on the Wall Connector.

    A car is a candidate while its own plug sensor is on and it is home. The Wall Connector
    seeing no vehicle rules every car out (a car plugged in elsewhere is never commanded);
    while it is unavailable, or not mapped, the cars' own sensors decide. An override other
    than "auto" leaves only the car it names. Two candidates are ambiguous: the planner
    commands neither until the override names one.
    """
    if wall_connected is False:
        return Identification(
            None, "The Wall Connector sees no vehicle.", (), wall_connected, override
        )
    candidates = tuple(c.car_id for c in cars if c.plugged is True and c.home is True)
    if override != OVERRIDE_AUTO:
        if override not in candidates:
            return Identification(
                None,
                f"The override names {override}, which is not plugged in at home.",
                candidates,
                wall_connected,
                override,
            )
        return Identification(
            override, f"The override names {override}.", candidates, wall_connected, override
        )
    if len(candidates) == 1:
        return Identification(
            candidates[0],
            f"{candidates[0]} is plugged in at home"
            + (" and the Wall Connector sees a vehicle." if wall_connected else "."),
            candidates,
            wall_connected,
            override,
        )
    if candidates:
        return Identification(
            None,
            "More than one car is plugged in at home; set the charger car override.",
            candidates,
            wall_connected,
            override,
        )
    return Identification(
        None, "No car is plugged in at home.", candidates, wall_connected, override
    )


# --------------------------------------------------------------------------- tariff


def _hms(value: Any) -> str:
    """A Schedule helper time as HH:MM:SS; the end of the day (time.max, 23:59:59 and
    anything after it) as 24:00:00."""
    if isinstance(value, time):
        if value >= time(23, 59, 59):
            return "24:00:00"
        return value.strftime("%H:%M:%S")
    text = str(value)
    if text.startswith("23:59:59") or text.startswith("24:00"):
        return "24:00:00"
    return text.split(".")[0]


def normalize_schedule(config: Mapping[str, Any]) -> dict[str, list[dict[str, str]]]:
    """A Schedule helper's weekly config (from ``schedule.get_schedule``) as JSON-safe text,
    as ``WeeklySchedule.from_ha`` reads it. Custom data on a block is dropped."""
    out: dict[str, list[dict[str, str]]] = {}
    for day in WEEKDAYS:
        blocks = config.get(day) or []
        out[day] = [
            {"from": _hms(b["from"]), "to": _hms(b["to"])}
            for b in blocks
            if isinstance(b, Mapping) and "from" in b and "to" in b
        ]
    return out


@dataclass(frozen=True)
class WindowSpec:
    """An extra price level: a Schedule helper's weekly config and its prices."""

    config: Mapping[str, Any]
    price: float
    label: str
    export_price: float | None = None


def build_tariff(
    price: float,
    buyback: float,
    free_config: Mapping[str, Any] | None,
    windows: Sequence[WindowSpec] = (),
) -> Tariff:
    """The planner's tariff: the free hours first (import at $0), then the extra price
    windows in order; the first window that is on wins, otherwise ``price`` applies."""
    out: list[PriceWindow] = []
    if free_config is not None:
        out.append(PriceWindow(WeeklySchedule.from_ha(free_config), 0.0, "free"))
    out.extend(
        PriceWindow(WeeklySchedule.from_ha(w.config), w.price, w.label, w.export_price)
        for w in windows
    )
    return Tariff(price, buyback, tuple(out))
