"""Forward model of the home battery under the Sol-Ark's self-consumption dispatch.

    net = pv - house - car
    net > 0: the battery charges min(net, max charge, room), the rest is exported;
    net < 0: the battery discharges min(-net, max discharge, energy above the reserve), the
             rest is imported. While it holds (free hours, Sol-Ark TOU slot with grid as the
             source) it does not discharge at all. Outside a hold, ANY car draw is served by
             the battery before the grid.

The car in the model draws a steady ``p_kw`` while it is in its window and still needs energy:
during the commitment period whatever the sun gives, after it only the solar surplus (the
planner re-plans every 15 min, so a steady current is never held into a deficit for long).
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
    hold: bool  # the battery holds (free hours and the inverter is set to hold)
    paid: bool  # import costs money
    car_on: bool  # inside the car's daytime window
    commit: bool  # inside the commitment period: the car holds its power even from the battery
    price: float = 0.0  # import price in the step ($/kWh)


@dataclass
class Path:
    e: list[float]  # battery kWh at the END of each step
    car_kw: list[float]
    car_kwh: float
    paid_kwh: float  # paid import over the whole path
    export_unmet_kwh: float  # exported while the car (in its window) still wanted energy


def simulate(
    steps: Sequence[Step], e0: float, p_kw: float, need_kwh: float, bat: BatterySpec
) -> Path:
    """Battery path with the car at a steady ``p_kw`` until ``need_kwh`` is in it."""
    e, eff = e0, bat.efficiency
    e_min, e_max = bat.kwh(bat.reserve_pct), bat.capacity_kwh
    got = paid = exp_unmet = 0.0
    es: list[float] = []
    cars: list[float] = []
    for s in steps:
        dt = s.dt_h
        car = 0.0
        if s.car_on and got < need_kwh:
            car = min(p_kw, (need_kwh - got) / dt)
            if not s.commit:
                car = min(car, max(0.0, s.pv - s.house))
        net = s.pv - s.house - car
        if net >= 0:
            ch = min(net, bat.max_charge_kw, max(0.0, e_max - e) / (eff * dt))
            e += ch * eff * dt
            if s.car_on and got + car * dt < need_kwh - 0.05:
                exp_unmet += (net - ch) * dt
        else:
            dis = 0.0
            if not s.hold:
                dis = min(-net, bat.max_discharge_kw, max(0.0, e - e_min) * eff / dt)
                e -= dis / eff * dt
            if s.paid:
                paid += (-net - dis) * dt
        got += car * dt
        es.append(e)
        cars.append(car)
    return Path(es, cars, got, paid, exp_unmet)


def simulate_profile(
    steps: Sequence[Step], e0: float, car_kw: Sequence[float], bat: BatterySpec
) -> list[float]:
    """Battery path (kWh at the end of each step) for a given car power per step."""
    e, eff = e0, bat.efficiency
    e_min, e_max = bat.kwh(bat.reserve_pct), bat.capacity_kwh
    out = []
    for s, car in zip(steps, car_kw, strict=False):
        net = s.pv - s.house - car
        if net >= 0:
            e += min(net, bat.max_charge_kw, max(0.0, e_max - e) / (eff * s.dt_h)) * eff * s.dt_h
        elif not s.hold:
            e -= min(-net, bat.max_discharge_kw, max(0.0, e - e_min) * eff / s.dt_h) / eff * s.dt_h
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
