"""Solcast detailedForecast parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.solar_ev_planner.core import forecast_energy, parse_detailed_forecast

LA = ZoneInfo("America/Los_Angeles")


def _attrs(day: str, offset: str = "-07:00"):
    rows = []
    for j in range(48):
        h, m = divmod(j * 30, 60)
        rows.append(
            {
                "period_start": f"{day}T{h:02d}:{m:02d}:00{offset}",
                "pv_estimate": 2.0 if 7 <= h < 19 else 0.0,
                "pv_estimate10": 1.0 if 7 <= h < 19 else 0.0,
                "pv_estimate90": 2.5 if 7 <= h < 19 else 0.0,
            }
        )
    return {"detailedForecast": rows, "estimate": 24.0}


def test_parse_today_and_tomorrow_to_local_naive():
    slots = parse_detailed_forecast(_attrs("2026-10-25"), _attrs("2026-10-26"), tz=LA)
    assert len(slots) == 96
    assert slots[0].start == datetime(2026, 10, 25, 0, 0)
    assert slots[0].start.tzinfo is None
    assert slots[14].p50 == 2.0 and slots[14].p10 == 1.0 and slots[14].p90 == 2.5
    assert all(
        b.start - a.start == timedelta(minutes=30) for a, b in zip(slots, slots[1:], strict=False)
    )


def test_parse_keeps_aware_when_asked_and_converts_zone():
    utc = {"detailedForecast": [{"period_start": "2026-10-25T20:00:00+00:00", "pv_estimate": 3}]}
    (s,) = parse_detailed_forecast(utc, tz=LA, naive=False)
    assert s.start == datetime(2026, 10, 25, 13, 0, tzinfo=LA)
    assert s.start.utcoffset() == timedelta(hours=-7)


def test_missing_and_bad_values_fall_back_to_p50():
    rows = [
        {"period_start": datetime(2026, 6, 15, 12, tzinfo=UTC), "pv_estimate": "4.5"},
        {"period_start": "2026-06-15T12:30:00+00:00", "pv_estimate": 4.0, "pv_estimate10": None},
        {"period_start": "2026-06-15T13:00:00+00:00", "pv_estimate": 4.0, "pv_estimate90": "x"},
        {"pv_estimate": 9.9},  # no period_start: skipped
    ]
    slots = parse_detailed_forecast(rows, tz=UTC)
    assert [s.p50 for s in slots] == [4.5, 4.0, 4.0]
    assert slots[0].p10 == slots[0].p90 == 4.5
    assert slots[1].p10 == 4.0
    assert slots[2].p90 == 4.0


def test_duplicates_keep_later_argument_and_none_is_skipped():
    a = {"detailedForecast": [{"period_start": "2026-06-15T12:00:00", "pv_estimate": 1.0}]}
    b = {"detailedForecast": [{"period_start": "2026-06-15T12:00:00", "pv_estimate": 2.0}]}
    (s,) = parse_detailed_forecast(a, None, b)
    assert s.p50 == 2.0


def test_forecast_energy_prorates_partial_slots():
    slots = parse_detailed_forecast(_attrs("2026-10-25"), tz=LA)
    t0 = datetime(2026, 10, 25, 7, 15)
    assert abs(forecast_energy(slots, t0, t0 + timedelta(hours=1)) - 2.0) < 1e-9
    assert abs(forecast_energy(slots, t0, t0 + timedelta(hours=1), "p10") - 1.0) < 1e-9
