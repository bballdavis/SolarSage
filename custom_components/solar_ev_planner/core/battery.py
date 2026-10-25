"""Forward model of the home battery under the Sol-Ark's self-consumption dispatch.

    net = pv - house - car
    net > 0: the battery charges min(net, max charge, room), the rest is exported;
    net < 0: the battery discharges min(-net, max discharge, energy above the reserve), the
             rest is imported. Outside free hours, ANY car draw is served by the battery
             before the grid.
    free hours: the Sol-Ark's own settings charge the battery from the grid; the model fills
             it at its full rate (the sun first) until it is full and never discharges it, so
             the next paid hours start full whatever the day took.

The car in the model draws a steady ``p_kw`` while it is in its window and still needs energy:
during the commitment period whatever the sun gives, after it only the solar surplus (the
planner re-plans every 15 min, so a steady current is never held into a deficit for long).
With ``held`` it keeps its power after the commitment period too, as far as the sun and the
battery supply it without import (the safety rule cuts paid import within minutes).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .types import BatterySpec


@dataclass
class Step:
    t: datetime
    dt_h: float
    pv: float  # kW
    house: float  # kW
    paid: bool  # import costs money
    car_on: bool  # inside the car's daytime window
    commit: bool  # inside the commitment period: the car holds its power even from the battery
    price: float = 0.0  # import price in the step ($/kWh)
    refill: bool = False  # free hours: the Sol-Ark charges the battery from the grid


@dataclass
class Path:
    e: list[float]  # battery kWh at the END of each step
    car_kw: list[float]
    car_kwh: float
    paid_kwh: float  # paid import over the whole path
    export_unmet_kwh: float  # exported while the car (in its window) still wanted energy


def battery_kw(s: Step, net: float, e: float, bat: BatterySpec) -> float:
    """The battery's power in the step (+ charging) at level ``e`` kWh, with ``net`` = pv -
    house - car; the grid supplies ``battery_kw - net`` (+ import)."""
    dt, eff = s.dt_h, bat.efficiency
    room = max(0.0, bat.capacity_kwh - e) / (eff * dt)
    if s.refill:  # the full rate until full, the sun first and the grid for the rest
        return min(bat.max_charge_kw, room)
    if net >= 0:
        return min(net, bat.max_charge_kw, room)
    return -min(-net, bat.max_discharge_kw, max(0.0, e - bat.kwh(bat.reserve_pct)) * eff / dt)


def _store(e: float, b: float, s: Step, bat: BatterySpec) -> float:
    return e + (b * bat.efficiency if b > 0 else b / bat.efficiency) * s.dt_h


def simulate(
    steps: Sequence[Step],
    e0: float,
    p_kw: float,
    need_kwh: float,
    bat: BatterySpec,
    held: bool = False,
) -> Path:
    """Battery path with the car at a steady ``p_kw`` until ``need_kwh`` is in it."""
    e = e0
    got = paid = exp_unmet = 0.0
    es: list[float] = []
    cars: list[float] = []
    for s in steps:
        dt = s.dt_h
        car = 0.0
        if s.car_on and got < need_kwh:
            car = min(p_kw, (need_kwh - got) / dt)
            if not s.commit:
                room = max(0.0, s.pv - s.house)
                if held and not s.refill:
                    spare = (e - bat.kwh(bat.reserve_pct)) * bat.efficiency / dt
                    room += max(0.0, min(bat.max_discharge_kw, spare))
                car = min(car, room)
        net = s.pv - s.house - car
        b = battery_kw(s, net, e, bat)
        e = _store(e, b, s, bat)
        grid = b - net
        if grid < 0 and s.car_on and got + car * dt < need_kwh - 0.05:
            exp_unmet -= grid * dt
        elif grid > 0 and s.paid:
            paid += grid * dt
        got += car * dt
        es.append(e)
        cars.append(car)
    return Path(es, cars, got, paid, exp_unmet)


def simulate_profile(
    steps: Sequence[Step], e0: float, car_kw: Sequence[float], bat: BatterySpec
) -> list[float]:
    """Battery path (kWh at the end of each step) for a given car power per step."""
    e = e0
    out = []
    for s, car in zip(steps, car_kw, strict=False):
        e = _store(e, battery_kw(s, s.pv - s.house - car, e, bat), s, bat)
        out.append(e)
    return out


def lowest(pred, lo: float, hi: float, n: int = 12) -> float:
    """Smallest x in [lo, hi] with pred(x) true, for pred false-then-true (bisection)."""
    for _ in range(n):
        mid = (lo + hi) / 2
        if pred(mid):
            hi = mid
        else:
            lo = mid
    return hi
