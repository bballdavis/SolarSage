"""House-load samples from recorder statistics, and the 14-day profile."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from custom_components.solar_ev_planner.core import (
    LoadSample,
    build_load_profile,
    house_load_kw,
    samples_from_statistics,
)
from custom_components.solar_ev_planner.core.load_profile import bin_days

from .helpers import MONDAY, WEEKDAY, WEEKEND, house_samples


def test_house_load_formula_and_clamp():
    # pv 5 kW, exporting 1 kW, battery charging 2 kW, car 1.5 kW -> house 0.5 kW
    assert house_load_kw(5.0, -1.0, 2.0, 1.5) == 0.5
    assert house_load_kw(0.0, 0.0, 0.0, 1.0) == 0.0


def test_samples_from_statistics_units_and_join():
    t0 = datetime(2026, 6, 15, 12)
    rows = lambda vals: [  # noqa: E731
        {"start": (t0 + timedelta(minutes=5 * i)).timestamp(), "mean": v}
        for i, v in enumerate(vals)
    ]
    pv = rows([6000.0, 6000.0, 6000.0])  # W
    grid = rows([-1000.0, -500.0, 0.0])  # W, negative = export
    batt = rows([2000.0, 2000.0, 2000.0])  # W, positive = charging
    wall = [{"start": (t0 + timedelta(minutes=5 * i)).isoformat(), "mean": 1.5} for i in (0, 1)]
    out = samples_from_statistics(pv, grid, batt, wall)
    assert [s.kw for s in out] == [1.5, 2.0]  # the third period has no wall reading
    assert out[0].start == t0 and out[0].minutes == 5.0


def test_hourly_statistics_fill_both_half_hours():
    t0 = datetime(2026, 6, 15, 0)
    rows = [{"start": t0 + timedelta(hours=h), "mean": 1000.0 * (1 + h % 2)} for h in range(24)]
    zero = [{"start": r["start"], "mean": 0.0} for r in rows]
    out = samples_from_statistics(rows, zero, zero, zero)
    assert out[0].minutes == 60.0
    b = bin_days(out)[t0.date()]
    assert b[0] == b[1] == 1.0 and b[2] == b[3] == 2.0
    assert all(x is not None for x in b)


def test_profile_keeps_weekdays_and_weekends_apart():
    p = build_load_profile(house_samples(MONDAY), MONDAY)
    assert p.n_days == 14
    # 18:00 weekday 2.8 kW vs weekend 2.8; 07:00 weekday 2.0 vs weekend 1.4
    wk7 = p.kw_at(datetime(2026, 6, 15, 7, 0))
    we7 = p.kw_at(datetime(2026, 6, 20, 7, 0))
    assert abs(wk7 - (0.9 * WEEKDAY[7] + 0.1 * 0)) < 0.25  # weekday shape dominates
    assert we7 < wk7
    assert abs(we7 - WEEKEND[7]) < 0.2
    # the day's energy: weekday ~ sum of the hourly profile
    e = p.energy_kwh(datetime(2026, 6, 15), datetime(2026, 6, 16))
    assert abs(e - sum(WEEKDAY)) < 1.5


def test_profile_is_recency_weighted():
    thursday = date(2026, 6, 18)
    old = house_samples(thursday, days=14, scale=1.0)
    # Monday to Wednesday used 50 % more (air conditioning)
    new = [
        LoadSample(s.start, s.kw * (1.5 if s.start.date() >= thursday - timedelta(days=3) else 1.0))
        for s in old
    ]
    p_old = build_load_profile(old, thursday)
    p_new = build_load_profile(new, thursday)
    t = datetime(2026, 6, 18, 15)
    ratio = p_new.kw_at(t) / p_old.kw_at(t)
    # 3 of 10 weekdays, but the recent ones weigh more: well above the plain 1.15
    assert 1.2 < ratio < 1.4


def test_incomplete_days_are_skipped_and_empty_history_is_flat_and_wide():
    s = [x for x in house_samples(MONDAY, days=2) if x.start.hour < 12]  # half days only
    p = build_load_profile(s, MONDAY)
    assert p.n_days == 0
    assert p.kw_at(datetime(2026, 6, 15, 18)) == 1.2
    t0 = datetime(2026, 6, 15, 12)
    assert abs(p.spread_kwh(t0, 4) - 0.3 * 1.2 * 2.0) < 1e-9


def test_spread_grows_with_day_to_day_variation():
    flat = build_load_profile(house_samples(MONDAY), MONDAY)
    noisy = [
        LoadSample(s.start, s.kw * (1.3 if s.start.day % 2 else 0.7), s.minutes)
        for s in house_samples(MONDAY)
    ]
    wavy = build_load_profile(noisy, MONDAY)
    t0 = datetime(2026, 6, 15, 12)
    assert flat.spread_kwh(t0, 16) < 0.2
    assert wavy.spread_kwh(t0, 16) > 2.0


def test_today_is_never_part_of_its_own_profile():
    today = date(2026, 6, 17)
    s = house_samples(today, days=3) + [
        LoadSample(datetime(2026, 6, 17, 1) + timedelta(minutes=5 * i), 9.0) for i in range(100)
    ]
    p = build_load_profile(s, today)
    assert max(p.weekday) < 3.0
