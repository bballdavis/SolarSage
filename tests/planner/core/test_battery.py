"""The home battery's forward model in free hours: refill, hold and normal."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.solar_ev_planner.core import BatterySpec
from custom_components.solar_ev_planner.core.battery import Step, simulate, simulate_profile

NINE_PM = datetime(2026, 6, 15, 21, 0)


def free_steps(n: int, *, refill: bool, hold: bool, pv: float = 0.0, house: float = 1.0):
    """``n`` free quarter hours with no car."""
    return [
        Step(
            t=NINE_PM + timedelta(minutes=15 * k),
            dt_h=0.25,
            pv=pv,
            house=house,
            hold=hold,
            paid=False,
            car_on=False,
            commit=False,
            refill=refill,
        )
        for k in range(n)
    ]


def test_refill_charges_from_the_grid_at_the_full_rate_without_paid_import():
    bat = BatterySpec()  # 31 kWh, 9 kW, 92% each way
    path = simulate(free_steps(40, refill=True, hold=True), bat.kwh(30), 0.0, 0.0, bat)
    assert path.e[3] - bat.kwh(30) == pytest.approx(9 * 0.92)  # the first hour
    assert path.e[-1] == pytest.approx(bat.capacity_kwh)  # full well before 6 AM
    assert path.paid_kwh == 0.0


def test_refill_stops_at_its_level_and_then_holds():
    bat = BatterySpec(refill_to_pct=80)
    e = simulate_profile(free_steps(40, refill=True, hold=True), bat.kwh(30), [0.0] * 40, bat)
    assert e[-1] == pytest.approx(bat.kwh(80))
    above = simulate_profile(free_steps(8, refill=True, hold=True), bat.kwh(90), [0.0] * 8, bat)
    assert above == pytest.approx([bat.kwh(90)] * 8)


def test_the_sun_still_fills_the_battery_above_the_refill_level():
    bat = BatterySpec(refill_to_pct=80)
    steps = free_steps(4, refill=True, hold=True, pv=6.0, house=1.0)
    e = simulate_profile(steps, bat.kwh(79.9), [0.0] * 4, bat)
    assert e[-1] == pytest.approx(bat.kwh(79.9) + 4 * 5.0 * 0.92 * 0.25)


def test_hold_keeps_the_level_and_normal_serves_the_house():
    bat = BatterySpec()
    hold = simulate_profile(free_steps(8, refill=False, hold=True), bat.kwh(50), [0.0] * 8, bat)
    normal = simulate_profile(free_steps(8, refill=False, hold=False), bat.kwh(50), [0.0] * 8, bat)
    assert hold[-1] == pytest.approx(bat.kwh(50))
    assert normal[-1] == pytest.approx(bat.kwh(50) - 2.0 / 0.92)  # 1 kW for 2 h
