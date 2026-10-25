"""Tests for schedule validation, hashing, and lease windows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.rivian_schedule_control.schedule import (
    applied_amps,
    fingerprint,
    lease_window,
    normalize_schedule,
    normalize_schedules,
    provider_fingerprint,
    to_provider_input,
)

CDT = timezone(timedelta(hours=-5))
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
