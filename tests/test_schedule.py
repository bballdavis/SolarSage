"""Tests for schedule validation, hashing, and lease windows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.solar_ev_planner.rivian.schedule import (
    MINUTES_PER_DAY,
    WEEK_DAYS,
    all_week_window,
    applied_amps,
    fingerprint,
    lease_window,
    normalize_schedule,
    normalize_schedules,
    overlapping_week_days,
    parked_ahead_window,
    provider_fingerprint,
    to_provider_input,
)

CDT = timezone(timedelta(hours=-5))
LA = ZoneInfo("America/Los_Angeles")
HOME = {"latitude": 1.0, "longitude": 2.0}


def schedule(**overrides):
    base = {
        "weekDays": ["Monday", "Friday"],
        "startTime": 60,
        "duration": 120,
        "location": dict(HOME),
        "amperage": 16,
        "enabled": True,
    }
    return base | overrides


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amperage", 15),
        ("amperage", 6),
        ("amperage", 50),
        ("weekDays", ["Monday", "Monday"]),
        ("weekDays", []),
        ("weekDays", ["Funday"]),
        ("startTime", 1440),
        ("duration", 0),
        ("duration", 1441),
        ("enabled", 1),
        ("location", {"latitude": 91, "longitude": 0}),
    ],
)
def test_strict_validation_rejects(field, value):
    with pytest.raises((ValueError, TypeError)):
        normalize_schedule(schedule(**{field: value}), strict=True)


def test_lenient_validation_accepts_provider_values_strict_rejects():
    provider = schedule(amperage=15, duration=10080)
    assert normalize_schedule(provider, strict=False)["amperage"] == 15
    with pytest.raises(ValueError):
        normalize_schedule(provider, strict=True)


def test_missing_field_rejected_even_leniently():
    data = schedule()
    del data["enabled"]
    with pytest.raises(ValueError, match="missing"):
        normalize_schedule(data, strict=False)


def test_null_list_rejected():
    with pytest.raises(TypeError):
        normalize_schedules(None, strict=False)


def test_normalize_copies_and_keeps_extras():
    data = schedule(id="server")
    result = normalize_schedule(data, strict=True)
    result["location"]["latitude"] = 5
    assert data["location"]["latitude"] == 1.0
    assert result["id"] == "server"


def test_fingerprint_ignores_extras_key_order_list_order_and_weekday_order():
    a, b = schedule(), schedule(weekDays=["Saturday"], amperage=8)
    with_extra = dict(reversed(list((a | {"id": "x"}).items())))
    days_reordered = a | {"weekDays": ["Friday", "Monday"]}
    reference = provider_fingerprint([a, b])
    assert reference.matches(provider_fingerprint([with_extra, b]))
    assert reference.matches(provider_fingerprint([b, a]))
    assert reference.matches(provider_fingerprint([days_reordered, b]))
    assert not provider_fingerprint([a]).matches(provider_fingerprint([a | {"amperage": 18}]))
    assert not provider_fingerprint([a]).matches(provider_fingerprint([a, b]))


def test_fingerprint_compares_locations_with_tolerance():
    def at(lat, lon):
        return provider_fingerprint([schedule(location={"latitude": lat, "longitude": lon})])

    # Values straddling a rounding boundary still match (about 1 m apart).
    assert at(30.123455, -97.7654321).matches(at(30.12346, -97.765432))
    assert at(30.123455, -97.7654321).matches(at(30.123455047607422, -97.7654321))
    # About 100 m away is a different place.
    assert not at(30.1234, -97.7654).matches(at(30.1244, -97.7654))


def test_fingerprint_key_contains_no_location():
    near = provider_fingerprint([schedule(location={"latitude": 1.0, "longitude": 2.0})])
    far = provider_fingerprint([schedule(location={"latitude": 40.0, "longitude": -80.0})])
    assert near.key == far.key
    assert not near.matches(far)


def test_fingerprint_round_trips_through_dict():
    fp = provider_fingerprint([schedule(), schedule(amperage=8)])
    assert type(fp).from_dict(fp.to_dict()) == fp


def test_to_provider_input_strips_extras():
    normalized = normalize_schedules([schedule(id="x")], strict=False)
    assert to_provider_input(normalized) == [schedule()]
    assert fingerprint(normalized).matches(provider_fingerprint(to_provider_input(normalized)))


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(13, 12), (12, 12), (7, 8), (0, 8), (50, 48), (8.9, 8), (47.5, 46)],
)
def test_applied_amps(requested, expected):
    assert applied_amps(requested) == expected


def test_active_lease_covers_now():
    now = datetime(2026, 10, 24, 13, 7, 42, tzinfo=CDT)  # Saturday
    window = lease_window(
        now, active=True, lease_minutes=20, skew_minutes=5, amps=16, location=HOME
    )
    assert window == {
        "weekDays": ["Saturday"],
        "startTime": 13 * 60 + 2,
        "duration": 30,
        "location": HOME,
        "amperage": 16,
        "enabled": True,
    }
    normalize_schedule(window, strict=True)


def test_parked_window_ended_before_now():
    now = datetime(2026, 10, 24, 13, 7, tzinfo=CDT)
    window = lease_window(
        now, active=False, lease_minutes=20, skew_minutes=5, amps=16, location=HOME
    )
    assert window["weekDays"] == ["Saturday"]
    assert window["startTime"] + window["duration"] == 13 * 60 + 2


def test_windows_across_midnight_use_start_weekday():
    now = datetime(2026, 10, 25, 0, 3, tzinfo=CDT)  # Sunday 00:03
    active = lease_window(now, active=True, lease_minutes=20, skew_minutes=5, amps=8, location=HOME)
    assert active["weekDays"] == ["Saturday"]
    assert active["startTime"] == 23 * 60 + 58
    parked = lease_window(
        now, active=False, lease_minutes=20, skew_minutes=5, amps=8, location=HOME
    )
    assert parked["weekDays"] == ["Saturday"]
    assert parked["startTime"] == 23 * 60 + 38


def test_naive_time_rejected():
    with pytest.raises(ValueError):
        lease_window(
            datetime(2026, 1, 1),
            active=True,
            lease_minutes=20,
            skew_minutes=5,
            amps=8,
            location=HOME,
        )


# Fixed windows (one per free night) -----------------------------------------------


def fixed_window(now: datetime, end: datetime, *, lease_minutes: int = 20, amps: int = 48):
    return lease_window(
        now,
        active=True,
        lease_minutes=lease_minutes,
        skew_minutes=5,
        amps=amps,
        location=HOME,
        end=end,
    )


def test_fixed_window_runs_from_the_skew_ago_to_its_end_across_midnight():
    now = datetime(2026, 10, 23, 20, 0, 42, tzinfo=LA)  # Friday, the free hours begin
    end = datetime(2026, 10, 24, 6, 0, tzinfo=LA)  # Saturday, they end
    window = fixed_window(now, end)
    assert window == {
        "weekDays": ["Friday"],
        "startTime": 19 * 60 + 55,
        "duration": 10 * 60 + 5,
        "location": HOME,
        "amperage": 48,
        "enabled": True,
    }
    assert (window["startTime"] + window["duration"]) % MINUTES_PER_DAY == 6 * 60
    normalize_schedule(window, strict=True)
    # Its length comes from the end alone, not from the lease length.
    assert fixed_window(now, end, lease_minutes=5) == window


def test_fixed_window_has_no_padding_at_its_end():
    now = datetime(2026, 10, 24, 13, 7, 42, tzinfo=CDT)
    lease = lease_window(now, active=True, lease_minutes=20, skew_minutes=5, amps=16, location=HOME)
    # The same 20 minutes as a fixed window: the skew before now, nothing after the end
    # (past it the vehicle would charge in paid hours). The end's seconds are dropped.
    window = fixed_window(now, datetime(2026, 10, 24, 13, 27, 59, tzinfo=CDT), amps=16)
    assert window["startTime"] == lease["startTime"] == 13 * 60 + 2
    assert (lease["duration"], window["duration"]) == (30, 25)
    assert window["startTime"] + window["duration"] == 13 * 60 + 27


def test_fixed_window_across_fall_back_ends_at_its_end_on_the_wall_clock():
    # Saturday 20:00 PDT to Sunday 06:00 PST is 11 hours, but the vehicle counts minutes on
    # its local wall clock.
    end = datetime(2026, 11, 1, 6, 0, tzinfo=LA)
    window = fixed_window(datetime(2026, 10, 31, 20, 0, tzinfo=LA), end)
    assert window["weekDays"] == ["Saturday"]
    assert (window["startTime"], window["duration"]) == (19 * 60 + 55, 10 * 60 + 5)
    # Rewritten at the second 01:00 (PST), it still ends at 06:00.
    rewritten = fixed_window(datetime(2026, 11, 1, 1, 0, fold=1, tzinfo=LA), end)
    assert rewritten["weekDays"] == ["Sunday"]
    assert (rewritten["startTime"], rewritten["duration"]) == (55, 5 * 60 + 5)


@pytest.mark.parametrize(
    ("end_minutes", "duration"), [(1, 6), (MINUTES_PER_DAY - 5, MINUTES_PER_DAY)]
)
def test_fixed_window_lasts_from_a_minute_up_to_a_day(end_minutes, duration):
    now = datetime(2026, 10, 23, 20, 0, tzinfo=LA)
    window = fixed_window(now, now + timedelta(minutes=end_minutes))
    assert window["duration"] == duration
    normalize_schedule(window, strict=True)


@pytest.mark.parametrize("end_minutes", [MINUTES_PER_DAY - 4, 2 * MINUTES_PER_DAY, -5, -6])
def test_fixed_window_must_end_within_a_day_after_it_starts(end_minutes):
    # The window starts 5 minutes (the skew) before now: more than a day after that, or
    # not after it at all, cannot be written as one window.
    now = datetime(2026, 10, 23, 20, 0, tzinfo=LA)
    with pytest.raises(ValueError, match="within a day"):
        fixed_window(now, now + timedelta(minutes=end_minutes))


def test_end_does_not_change_a_parked_window():
    now = datetime(2026, 10, 24, 13, 7, tzinfo=CDT)
    parked = lease_window(
        now, active=False, lease_minutes=20, skew_minutes=5, amps=8, location=HOME
    )
    assert (
        lease_window(
            now,
            active=False,
            lease_minutes=20,
            skew_minutes=5,
            amps=8,
            location=HOME,
            end=now + timedelta(hours=3),
        )
        == parked
    )


def test_all_week_window_covers_every_day_all_day():
    window = all_week_window(amps=24, location={"latitude": 1.23456789, "longitude": 2.0})
    assert window == {
        "weekDays": list(WEEK_DAYS),
        "startTime": 0,
        "duration": MINUTES_PER_DAY,
        "location": {"latitude": 1.234568, "longitude": 2.0},
        "amperage": 24,
        "enabled": True,
    }
    assert normalize_schedule(window, strict=True) == window


def test_parked_ahead_window_starts_ahead_on_the_local_clock():
    now = datetime(2026, 10, 24, 18, 37, 42, tzinfo=CDT)  # Saturday
    window, start = parked_ahead_window(
        now, ahead=timedelta(hours=12), duration_minutes=60, amps=8, location=HOME
    )
    assert start == datetime(2026, 10, 25, 6, 37, tzinfo=CDT)
    assert window == {
        "weekDays": ["Sunday"],
        "startTime": 6 * 60 + 37,
        "duration": 60,
        "location": HOME,
        "amperage": 8,
        "enabled": True,
    }
    with pytest.raises(ValueError):
        parked_ahead_window(
            now.replace(tzinfo=None),
            ahead=timedelta(hours=12),
            duration_minutes=60,
            amps=8,
            location=HOME,
        )


@pytest.mark.parametrize(
    ("days", "shared"),
    [
        ([["Monday", "Friday"], ["Saturday"]], []),
        ([["Monday", "Friday"], ["Friday", "Sunday"], ["Sunday"]], ["Friday", "Sunday"]),
        ([list(WEEK_DAYS)], []),
        ([], []),
    ],
)
def test_overlapping_week_days(days, shared):
    schedules = [schedule(weekDays=d, enabled=i % 2 == 0) for i, d in enumerate(days)]
    assert overlapping_week_days(schedules) == shared
