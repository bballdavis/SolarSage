"""Tariffs from schedule windows plus prices."""

from __future__ import annotations

from datetime import datetime

from custom_components.solar_ev_planner.core import PriceWindow, Tariff, WeeklySchedule
from custom_components.solar_ev_planner.core.tariff import minutes, next_flip


def test_minutes_handles_ha_end_of_day():
    assert minutes("20:00") == 1200
    assert minutes("06:00:00") == 360
    assert minutes("23:59:59") == 1440
    assert minutes("24:00") == 1440


def test_daily_window_wraps_midnight():
    s = WeeklySchedule.daily("20:00", "06:00")
    assert s.contains(datetime(2026, 6, 15, 20, 0))
    assert s.contains(datetime(2026, 6, 16, 5, 59))
    assert not s.contains(datetime(2026, 6, 16, 6, 0))
    assert not s.contains(datetime(2026, 6, 15, 19, 59))


def test_owner_free_nights_prices():
    t = Tariff.free_nights("20:00", "06:00", 0.32, 0.0)
    assert t.import_price(datetime(2026, 6, 15, 12)) == 0.32
    assert t.import_price(datetime(2026, 6, 15, 21)) == 0.0
    assert t.is_free(datetime(2026, 6, 16, 3))
    assert t.export_price(datetime(2026, 6, 15, 12)) == 0.0
    assert t.label(datetime(2026, 6, 15, 21)) == "free"
    assert t.min_price() == 0.0


def test_from_ha_schedule_config_merges_touching_blocks():
    # how the HA Schedule helper stores a 20:00-06:00 night: evening and morning blocks
    cfg = {
        day: [{"from": "00:00:00", "to": "06:00:00"}, {"from": "20:00:00", "to": "23:59:59"}]
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    }
    s = WeeklySchedule.from_ha(cfg)
    t = Tariff(0.32, 0.0, (PriceWindow(s, 0.0, "free"),))
    assert t.is_free(datetime(2026, 6, 15, 23, 59, 30))
    assert t.is_free(datetime(2026, 6, 16, 0, 0))
    # the free period runs on from 20:00 to 06:00 without a break at midnight
    assert next_flip(datetime(2026, 6, 15, 20, 0), t.is_free, _h(24)) == datetime(2026, 6, 16, 6)


def test_time_of_use_and_flat_and_free_weekend():
    tou = Tariff(0.11, 0.05, (PriceWindow(WeeklySchedule.daily("16:00", "21:00"), 0.38, "peak"),))
    assert tou.import_price(datetime(2026, 6, 15, 17)) == 0.38
    assert tou.import_price(datetime(2026, 6, 15, 22)) == 0.11
    assert not tou.is_free(datetime(2026, 6, 15, 22))
    assert tou.next_change(datetime(2026, 6, 15, 12, 3)) == datetime(2026, 6, 15, 16)
    flat = Tariff(0.32, 0.0)
    assert not flat.is_free(datetime(2026, 6, 15, 2))
    assert flat.next_change(datetime(2026, 6, 15, 2)) is None
    wk = Tariff(0.32, 0.0, (PriceWindow(WeeklySchedule.days("00:00", "24:00", (5, 6)), 0.0),))
    assert wk.is_free(datetime(2026, 6, 20, 13))  # Saturday
    assert not wk.is_free(datetime(2026, 6, 22, 0, 5))  # Monday


def test_window_on_given_weekdays_wraps_into_next_day():
    s = WeeklySchedule.days("22:00", "02:00", (4,))  # Friday night only
    assert s.contains(datetime(2026, 6, 19, 23))  # Friday
    assert s.contains(datetime(2026, 6, 20, 1))  # Saturday morning
    assert not s.contains(datetime(2026, 6, 18, 23))  # Thursday


def test_first_window_wins_and_export_override():
    peak = PriceWindow(WeeklySchedule.daily("16:00", "21:00"), 0.40, "peak", export_price=0.10)
    free = PriceWindow(WeeklySchedule.daily("20:00", "06:00"), 0.0, "free")
    t = Tariff(0.32, 0.02, (peak, free))
    assert t.import_price(datetime(2026, 6, 15, 20, 30)) == 0.40  # peak listed first
    assert t.export_price(datetime(2026, 6, 15, 17)) == 0.10
    assert t.export_price(datetime(2026, 6, 15, 12)) == 0.02


def _h(n: int):
    from datetime import timedelta

    return timedelta(hours=n)
