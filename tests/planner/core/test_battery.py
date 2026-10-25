"""The home battery's forward model: free hours refill it, paid hours draw on it."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.solar_ev_planner.core import BatterySpec
from custom_components.solar_ev_planner.core.battery import Step, simulate, simulate_profile

NINE_PM = datetime(2026, 6, 15, 21, 0)


def free_steps(n: int, *, pv: float = 0.0, house: float = 1.0, refill: bool = True):
    """``n`` quarter hours with no car (free hours unless ``refill`` is off)."""
    return [
        Step(
            t=NINE_PM + timedelta(minutes=15 * k),
            dt_h=0.25,
            pv=pv,
            house=house,
            paid=not refill,
            car_on=False,
            commit=False,
            refill=refill,
        )
        for k in range(n)
    ]


def test_free_hours_charge_the_battery_from_the_grid_at_the_full_rate():
    bat = BatterySpec()  # 31 kWh, 9 kW, 92% each way
    path = simulate(free_steps(40), bat.kwh(30), 0.0, 0.0, bat)
    assert path.e[3] - bat.kwh(30) == pytest.approx(9 * 0.92)  # the first hour
    assert path.e[-1] == pytest.approx(bat.capacity_kwh)  # full well before 6 AM
    assert path.paid_kwh == 0.0  # the house and the battery run from free import


def test_a_full_battery_neither_charges_nor_discharges_in_free_hours():
    bat = BatterySpec()
    e = simulate_profile(free_steps(8), bat.capacity_kwh, [0.0] * 8, bat)
    assert e == pytest.approx([bat.capacity_kwh] * 8)


def test_the_sun_counts_toward_the_refill():
    bat = BatterySpec()
    steps = free_steps(4, pv=6.0, house=1.0)  # a free daytime hour (a free weekend)
    path = simulate(steps, bat.kwh(50), 0.0, 0.0, bat)
    assert path.e[-1] == pytest.approx(bat.kwh(50) + 9 * 0.92)  # still the 9 kW limit


def test_outside_free_hours_the_battery_serves_the_house():
    bat = BatterySpec()
    e = simulate_profile(free_steps(8, refill=False), bat.kwh(50), [0.0] * 8, bat)
    assert e[-1] == pytest.approx(bat.kwh(50) - 2.0 / 0.92)  # 1 kW for 2 h
