"""Planner decisions on synthetic sites: modes, the floor rule, rate limits, safety, plans,
outputs, persistence and determinism."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest

from custom_components.solar_ev_planner.core import (
    Measured,
    Planner,
    PriceWindow,
    Settings,
    Tariff,
    WeeklySchedule,
)

from .helpers import CarPlan, rivian, snapshot, tesla

NOON = datetime(2026, 6, 15, 12, 0)  # a Monday


def _plan(settings: Settings | None = None) -> Planner:
    return Planner(settings or Settings(shadow=False))


# --------------------------------------------------------------------------- modes


def test_empty_charger_and_planner_off_send_nothing(settings):
    snap = snapshot(NOON, cars=[rivian(plugged=False), tesla(plugged=False)])
    d = _plan(settings).decide(snap)
    assert d.mode == "empty" and d.car_id is None
    assert [c.amps for c in d.commands] == [None, None]
    assert d.trajectory  # the battery outlook is shown even without a car
    off = _plan(replace(settings, enabled=False)).decide(snapshot(NOON))
    assert off.mode == "off" and off.amps is None


def test_sunny_day_full_battery_charges_from_solar(settings):
    d = _plan(settings).decide(snapshot(NOON, soc=90.0))
    assert d.mode == "day"
    assert d.amps is not None and d.amps >= 8
    assert d.reason.startswith("Solar charging at") and d.reason.endswith(".")
    assert d.reason.count(". ") == 0  # one sentence


def test_low_battery_on_a_poor_day_gives_the_car_nothing(settings):
    snap = snapshot(NOON, soc=25.0, actual_ratio=0.3, fc_ratio=0.3, house_scale=1.5)
    d = _plan(settings).decide(snap)
    assert d.mode == "day" and d.amps == 0
    assert "floor" in d.reason or "No room" in d.reason


def test_free_hours_charge_at_full_current_from_their_start(settings):
    for now in (datetime(2026, 6, 15, 20, 5), datetime(2026, 6, 15, 23, 0)):
        car = rivian(soc=70.0, since=now - timedelta(minutes=5))
        d = _plan(settings).decide(snapshot(now, soc=50.0, cars=[car]))
        assert d.mode == "night" and d.amps == 48
        assert d.reason == "Free hours: charging at 48 A to the 85% limit."
        assert d.next_change is not None and d.next_change.amps == 0  # at the limit
        assert d.next_change.why == "the R1S reaches its limit"
    # the Wall Connector's own maximum caps it
    d = _plan(settings).decide(snapshot(now, soc=50.0, charger_max_a=32))
    assert d.amps == 32


def test_free_hours_send_full_current_once(settings):
    p = _plan(settings)
    now = datetime(2026, 6, 15, 21, 0)
    car = rivian(soc=60.0, since=now - timedelta(minutes=5))
    assert p.decide(snapshot(now, soc=50.0, cars=[car])).amps == 48
    # the car holds 48 A: nothing is repeated (a Tesla the owner stopped from its app stays
    # stopped; the executor re-sends only a request Rivian schedule control dropped)
    car = replace(car, commanded_a=48)
    d = p.decide(snapshot(now + timedelta(minutes=1), soc=50.0, cars=[car]))
    assert d.mode == "night" and d.amps is None
    assert d.reason == "Free hours: charging at 48 A to the 85% limit."


def test_the_next_change_at_the_free_start_is_full_current(settings):
    now = datetime(2026, 6, 15, 17, 0)
    car = rivian(soc=40.0, since=now - timedelta(hours=3), commanded_a=40)
    snap = snapshot(now, soc=90.0, cars=[car], tariff=Tariff.free_nights("18:00", "06:00"))
    d = _plan(settings).decide(snap)
    assert d.mode == "day" and d.amps is not None and 0 < d.amps < 48
    assert d.next_change is not None
    assert (d.next_change.at, d.next_change.amps) == (datetime(2026, 6, 15, 18, 0), 48)
    assert d.next_change.why == "the free hours start"


def test_a_safety_cut_never_raises_the_plan(settings):
    # the first paid-hours decision comes late (a restart at 06:07) with the car still at the
    # free hours' 48 A and the grid importing: the plan's 0 A goes at once, not a cut to 32 A
    p = _plan(settings)
    night = datetime(2026, 6, 16, 5, 55)
    car = rivian(soc=60.0, since=night - timedelta(hours=8))
    assert p.decide(snapshot(night, soc=100.0, cars=[car])).amps == 48
    car = replace(car, commanded_a=48)
    m = Measured(pv_kw=0.0, grid_kw=3.5, battery_kw=-9.0, car_kw=11.5, soc_pct=98.0)
    d = p.decide(snapshot(datetime(2026, 6, 16, 6, 7), soc=98.0, cars=[car], measured=m))
    assert d.mode == "day" and d.amps == 0 and d.safety == "paid_import"


def test_the_day_uses_the_battery_the_free_hours_refill(settings):
    snap = snapshot(NOON, soc=55.0, fc_ratio=0.8, actual_ratio=0.8)
    free = _plan(settings).decide(snap)
    flat = _plan(settings).decide(replace(snap, tariff=Tariff(0.32, 0.0)))  # no free hours
    assert free.amps is not None and flat.amps is not None and free.amps > flat.amps
    assert "before the free hours refill it" in free.reason
    at = {p.t: p.soc_pct for p in free.trajectory}
    # on the cautious day the battery gives the car what it holds above the wanted level
    # (the floor) when the free hours start, and the Sol-Ark has it full by their end
    assert at[datetime(2026, 6, 15, 20, 0)] == pytest.approx(
        settings.wanted_at_free_start_pct, abs=1
    )
    assert at[datetime(2026, 6, 16, 6, 0)] == pytest.approx(100.0)
    # a higher wanted level keeps more in the battery and gives the car less
    keep = _plan(replace(settings, wanted_at_free_start_pct=60.0)).decide(snap)
    assert keep.amps is not None and keep.amps < free.amps
    kept = {p.t: p.soc_pct for p in keep.trajectory}
    assert kept[datetime(2026, 6, 15, 20, 0)] >= 59.0


def test_daytime_solar_off_waits(settings):
    d = _plan(replace(settings, daytime_solar=False)).decide(snapshot(NOON, soc=90.0))
    assert d.amps == 0 and "Daytime solar charging is off" in d.reason


def test_full_and_stale_cars_are_not_commanded(settings):
    full = replace(rivian(since=NOON - timedelta(hours=1)), soc_pct=85.0)
    assert _plan(settings).decide(snapshot(NOON, cars=[full])).amps is None
    stale = rivian(since=NOON - timedelta(hours=1), stale=True)
    d = _plan(settings).decide(snapshot(NOON, cars=[stale]))
    assert d.mode == "stale" and d.amps is None


def test_only_the_plugged_car_gets_a_command(settings):
    cars = [rivian(plugged=False), tesla(plugged=True, since=NOON - timedelta(hours=1))]
    d = _plan(settings).decide(snapshot(NOON, soc=90.0, cars=cars))
    assert d.car_id == "tesla"
    assert d.command_for("rivian").amps is None
    assert d.command_for("tesla").amps is not None


# --------------------------------------------------------------------------- slow loop


def test_rate_limit_holds_between_changes(settings):
    p = _plan(settings)
    car = rivian(since=NOON - timedelta(hours=2))
    d1 = p.decide(snapshot(NOON, soc=90.0, cars=[car]))
    assert d1.amps and d1.amps > 0
    # five minutes later the plan wants much less (clouds, battery lower): held
    car2 = replace(car, commanded_a=d1.amps)
    t2 = NOON + timedelta(minutes=5)
    d2 = p.decide(snapshot(t2, soc=31.0, cars=[car2], actual_ratio=0.3, fc_ratio=0.3))
    if d2.amps is None:
        assert d2.reason.startswith("Holding")
    else:  # a decrease the battery rules require is still at most max_step_a
        assert d1.amps - d2.amps <= settings.max_step_a


# --------------------------------------------------------------------------- safety


def test_paid_import_while_charging_cuts_the_car(settings):
    p = _plan(settings)
    car = rivian(since=NOON - timedelta(hours=2), commanded_a=40)
    p.decide(snapshot(NOON - timedelta(minutes=30), soc=60.0, cars=[car]))
    p.last_change = NOON - timedelta(hours=1)
    m = Measured(pv_kw=2.0, grid_kw=4.0, battery_kw=-3.0, car_kw=9.6, soc_pct=60.0)
    for k in range(3):  # import seen at 12:00, 12:05, 12:10 -> beyond the 5-min tolerance
        d = p.decide(snapshot(NOON + timedelta(minutes=5 * k), soc=60.0, cars=[car], measured=m))
    assert d.safety == "paid_import"
    assert d.amps is not None and d.amps < 40
    assert "Paid grid import" in d.reason


def test_battery_at_floor_discharging_into_car_cuts(settings):
    p = _plan(settings)
    car = rivian(since=NOON - timedelta(hours=2), commanded_a=24)
    p.decide(snapshot(NOON - timedelta(minutes=30), soc=40.0, cars=[car]))
    p.last_change = NOON - timedelta(hours=1)
    m = Measured(pv_kw=3.0, grid_kw=0.0, battery_kw=-4.0, car_kw=5.8, soc_pct=29.5)
    d = p.decide(snapshot(NOON, soc=29.5, cars=[car], measured=m))
    assert d.safety == "battery_floor" and d.amps is not None and d.amps < 24


# --------------------------------------------------------------------------- plans


def test_plan_inside_solar_window_pushes_now(settings):
    plan = CarPlan(80.0, datetime(2026, 6, 15, 16, 0))
    car = tesla(soc=30.0, plugged=True, since=NOON - timedelta(hours=1), plans=(plan,))
    d = _plan(settings).decide(snapshot(NOON, soc=50.0, cars=[car], actual_ratio=0.5))
    assert d.amps == 48
    assert d.reason.startswith("Plan 80% by 16:00")
    o = next(o for o in d.outlooks if o.car_id == "tesla")
    assert o.by == plan.deadline and o.need_kwh > 30


def test_plan_met_by_free_hours_waits(settings):
    now = datetime(2026, 6, 15, 18, 0)
    plan = CarPlan(60.0, datetime(2026, 6, 16, 7, 30))
    car = rivian(soc=50.0, since=now - timedelta(minutes=10), plans=(plan,))
    d = _plan(settings).decide(snapshot(now, soc=60.0, cars=[car]))
    assert not d.reason.startswith("Plan")
    o = d.outlooks[0]
    assert o.free_kwh > 0 and o.paid_kwh == 0 and o.short_kwh == 0


# --------------------------------------------------------------------------- outputs


def test_trajectory_outlook_and_next_change(settings):
    d = _plan(settings).decide(snapshot(datetime(2026, 6, 15, 10, 0), soc=60.0))
    tr = d.trajectory
    assert len(tr) > 40
    assert tr[0].t == datetime(2026, 6, 15, 10, 15)
    # the caution path never lies above the expected day
    assert all(p.soc_pct <= p.soc_expected_pct + 0.5 for p in tr)
    assert all(0.0 <= p.soc_pct <= 100.0 for p in tr)
    assert {p.price for p in tr} == {0.0, 0.32}
    o = d.outlooks[0]
    assert o.summary and o.need_kwh > 0
    assert d.next_change is not None and d.next_change.at > d.now
    assert d.debug["tonight_car"] == "rivian"


def test_shadow_mode_tracks_a_virtual_car():
    p = Planner(Settings(shadow=True))
    car = rivian(since=NOON - timedelta(hours=1), commanded_a=0)
    d1 = p.decide(snapshot(NOON, soc=90.0, cars=[car]))
    assert d1.shadow and d1.amps and d1.amps > 0
    # the real car never changed (nothing was sent), yet the planner holds its own plan
    d2 = p.decide(snapshot(NOON + timedelta(minutes=5), soc=90.0, cars=[car]))
    assert d2.amps is None or d2.amps == d1.amps
    assert p.shadow_a == d1.amps


# --------------------------------------------------------------------------- determinism, state


def _key(d):
    return (d.mode, d.amps, d.reason, tuple((p.t, p.soc_pct) for p in d.trajectory))


def test_deterministic(settings):
    snap = snapshot(NOON, soc=70.0)
    assert _key(_plan(settings).decide(snap)) == _key(_plan(settings).decide(snap))


def test_state_round_trip_keeps_the_rate_limiter(settings):
    p = _plan(settings)
    car = rivian(since=NOON - timedelta(hours=1))
    d = p.decide(snapshot(NOON, soc=90.0, cars=[car]))
    blob = json.dumps(p.state_dict())
    q = _plan(settings)
    q.load_state(json.loads(blob))
    assert q.last_change == p.last_change and q.session == p.session
    car2 = replace(car, commanded_a=d.amps)
    t = NOON + timedelta(minutes=5)
    a = p.decide(snapshot(t, soc=90.0, cars=[car2]))
    b = q.decide(snapshot(t, soc=90.0, cars=[car2]))
    assert (a.amps, a.reason) == (b.amps, b.reason)


def test_aware_local_datetimes_work(settings):
    tz = ZoneInfo("America/Los_Angeles")
    now = NOON.replace(tzinfo=tz)
    snap = snapshot(NOON, soc=90.0)
    snap = replace(
        snap,
        now=now,
        forecast=[replace(s, start=s.start.replace(tzinfo=tz)) for s in snap.forecast],
        house_recent=[replace(s, start=s.start.replace(tzinfo=tz)) for s in snap.house_recent],
        cars=(rivian(since=now - timedelta(hours=1)),),
        forecast_issued_at=now - timedelta(hours=1),
    )
    d = _plan(settings).decide(snap)
    assert d.mode == "day" and d.amps and d.trajectory[0].t.tzinfo is tz


def test_flat_and_tou_tariffs_run(settings):
    flat = Tariff(0.32, 0.0)
    d = _plan(settings).decide(snapshot(NOON, soc=90.0, tariff=flat))
    assert d.mode == "day" and d.amps is not None
    tou = Tariff(0.11, 0.05, (PriceWindow(WeeklySchedule.daily("16:00", "21:00"), 0.38, "peak"),))
    d2 = _plan(settings).decide(snapshot(datetime(2026, 6, 15, 23), soc=50.0, tariff=tou))
    assert d2.mode == "day"  # nothing is free on TOU: no night mode


# --------------------------------------------------------------------------- more slow-loop rules


def _running(p: Planner, car, soc: float = 95.0, measured=None):
    """A session that started 30 min ago, with the last change an hour back."""
    p.decide(snapshot(NOON - timedelta(minutes=30), soc=soc, cars=[car], measured=measured))
    p.last_change = NOON - timedelta(hours=1)


def test_large_increase_moves_max_step_and_says_so(settings):
    p = _plan(settings)
    car = rivian(since=NOON - timedelta(hours=2), commanded_a=8)
    _running(p, car)
    d = p.decide(snapshot(NOON, soc=95.0, cars=[car]))
    want = d.debug["want_a"]
    assert want >= 20
    assert d.amps == 14  # 8 A + the 6 A largest change
    assert d.reason.startswith(f"Solar charging at 14 A (on the way to {want} A):")
    assert d.next_change is not None and d.next_change.amps == want


def test_small_change_inside_deadband_is_ignored(settings):
    p = _plan(settings)
    car = rivian(since=NOON - timedelta(hours=2))
    _running(p, car)
    want = p.decide(snapshot(NOON, soc=95.0, cars=[car])).debug["want_a"]
    near = replace(car, commanded_a=want - 2)
    p.last_change = NOON - timedelta(hours=1)
    d = p.decide(snapshot(NOON + timedelta(minutes=5), soc=95.0, cars=[near]))
    assert d.amps is None and "deadband" in d.reason


def test_full_battery_exporting_allows_an_early_increase(settings):
    p = _plan(settings)
    car = rivian(since=NOON - timedelta(hours=2), commanded_a=10)
    m = Measured(pv_kw=9.0, grid_kw=-3.0, battery_kw=0.0, car_kw=2.4, soc_pct=99.0)
    _running(p, car, soc=99.0, measured=m)
    p.last_change = NOON - timedelta(minutes=10)  # inside the 15-min interval
    d = p.decide(snapshot(NOON, soc=99.0, cars=[car], measured=m))
    assert d.amps == 16
    # without the export the same call holds
    q = _plan(settings)
    quiet = Measured(pv_kw=9.0, grid_kw=0.0, battery_kw=3.0, car_kw=2.4, soc_pct=90.0)
    _running(q, car, soc=90.0, measured=quiet)
    q.last_change = NOON - timedelta(minutes=10)
    d2 = q.decide(snapshot(NOON, soc=90.0, cars=[car], measured=quiet))
    assert d2.amps is None and d2.reason.startswith("Holding 10 A until 12:05")


def test_charging_when_free_hours_begin_carries_on(settings):
    p = _plan(settings)
    t = datetime(2026, 6, 15, 19, 55)
    car = rivian(soc=40.0, since=t - timedelta(hours=3), commanded_a=16)
    p.decide(snapshot(t, soc=80.0, cars=[car]))
    d = p.decide(snapshot(t + timedelta(minutes=5), soc=80.0, cars=[car]))
    assert d.mode == "night" and d.amps == 48
    assert d.reason.startswith("Free hours: charging at 48 A")


# --------------------------------------------------------------------------- night charging start


def test_the_car_waits_in_free_hours_for_the_night_charging_start(settings):
    late = replace(settings, night_start="23:00")
    now = datetime(2026, 6, 15, 20, 5)
    car = rivian(soc=60.0, since=now - timedelta(minutes=5))
    d = _plan(late).decide(snapshot(now, soc=50.0, cars=[car]))
    assert d.mode == "night" and d.amps == 0
    assert d.reason == "Free hours: the R1S waits for the night charging start at 23:00."
    assert d.next_change is not None
    assert (d.next_change.at, d.next_change.amps) == (datetime(2026, 6, 15, 23, 0), 48)
    assert d.next_change.why == "night charging starts"
    # the battery path has the car from 23:00, not before
    before = [p.car_kw for p in d.trajectory if p.t <= datetime(2026, 6, 15, 23, 0)]
    after = [p.car_kw for p in d.trajectory if p.t > datetime(2026, 6, 15, 23, 0)]
    assert not any(before) and max(after) > 10  # (steps run from now, 20:05)
    # from 23:00, and after midnight, full current
    for t in (datetime(2026, 6, 15, 23, 0), datetime(2026, 6, 16, 2, 0)):
        car = rivian(soc=60.0, since=t - timedelta(minutes=5))
        d = _plan(late).decide(snapshot(t, soc=50.0, cars=[car]))
        assert (d.mode, d.amps) == ("night", 48), t
        assert d.free_until == MONDAY_NIGHT_END


def test_a_night_start_outside_the_free_hours_means_their_start(settings):
    early = replace(settings, night_start="18:00")
    now = datetime(2026, 6, 15, 20, 5)
    car = rivian(soc=60.0, since=now - timedelta(minutes=5))
    d = _plan(early).decide(snapshot(now, soc=50.0, cars=[car]))
    assert (d.mode, d.amps) == ("night", 48)


def test_the_day_plans_the_night_from_the_night_charging_start(settings):
    now = datetime(2026, 6, 15, 17, 0)
    car = rivian(soc=40.0, since=now - timedelta(hours=3), commanded_a=40)
    tariff = Tariff.free_nights("18:00", "06:00")
    late = replace(settings, night_start="22:00")
    d = _plan(late).decide(snapshot(now, soc=90.0, cars=[car], tariff=tariff))
    assert d.next_change is not None
    assert (d.next_change.at, d.next_change.amps) == (datetime(2026, 6, 15, 22, 0), 48)
    assert d.next_change.why == "night charging starts"
    # four fewer free hours to fill the car in
    full = _plan(settings).decide(snapshot(now, soc=90.0, cars=[car], tariff=tariff))
    assert d.outlooks[0].free_kwh <= full.outlooks[0].free_kwh


# --------------------------------------------------------------------------- the free hours' end
# Rivian schedule control writes one window to it for the whole night, so a night decision
# carries when the free hours end, the same on every cycle; no other decision carries one.

MONDAY_NIGHT_END = datetime(2026, 6, 16, 6, 0)


def test_night_decisions_carry_the_end_of_the_free_hours(settings):
    for now in (
        datetime(2026, 6, 15, 20, 0),  # their first minute
        datetime(2026, 6, 15, 23, 37, 20),
        datetime(2026, 6, 16, 0, 0),  # past midnight: the same night
        datetime(2026, 6, 16, 5, 59, 30),  # their last minute
    ):
        car = rivian(soc=60.0, since=now - timedelta(minutes=5))
        d = _plan(settings).decide(snapshot(now, soc=50.0, cars=[car]))
        assert d.mode == "night" and d.amps == 48, now
        assert d.free_until == MONDAY_NIGHT_END, now
    # the car was charging when they began: the same end
    p = _plan(settings)
    t = datetime(2026, 6, 15, 19, 55)
    car = rivian(soc=40.0, since=t - timedelta(hours=3), commanded_a=16)
    assert p.decide(snapshot(t, soc=80.0, cars=[car])).free_until is None
    d = p.decide(snapshot(t + timedelta(minutes=5), soc=80.0, cars=[car]))
    assert (d.mode, d.amps, d.free_until) == ("night", 48, MONDAY_NIGHT_END)
    # other free hours: their own end
    now = datetime(2026, 6, 15, 23, 0)
    car = rivian(soc=60.0, since=now - timedelta(minutes=5))
    snap = snapshot(now, soc=50.0, cars=[car], tariff=Tariff.free_nights("22:00", "07:30"))
    assert _plan(settings).decide(snap).free_until == datetime(2026, 6, 16, 7, 30)


def test_a_held_night_keeps_the_same_end(settings):
    """Nothing to send while the car holds 48 A, and the end stays where it was."""
    p = _plan(settings)
    now = datetime(2026, 6, 15, 21, 0)
    car = rivian(soc=60.0, since=now - timedelta(minutes=5))
    assert p.decide(snapshot(now, soc=50.0, cars=[car])).free_until == MONDAY_NIGHT_END
    car = replace(car, commanded_a=48)
    for minutes in (1, 7, 95, 8 * 60 + 55):  # 21:01 ... 05:55
        d = p.decide(snapshot(now + timedelta(minutes=minutes), soc=50.0, cars=[car]))
        assert d.mode == "night" and d.amps is None, minutes
        assert d.free_until == MONDAY_NIGHT_END, minutes


def test_shadow_night_decisions_carry_the_end_too():
    """The core always says it; the integration publishes it only while it commands."""
    now = datetime(2026, 6, 15, 21, 0)
    car = rivian(soc=60.0, since=now - timedelta(minutes=5))
    d = Planner(Settings(shadow=True)).decide(snapshot(now, soc=50.0, cars=[car]))
    assert d.shadow and d.mode == "night" and d.free_until == MONDAY_NIGHT_END


def test_the_free_end_is_set_in_free_hours_with_a_car_in_any_mode(settings):
    assert _plan(settings).decide(snapshot(NOON, soc=90.0)).free_until is None
    for now in (datetime(2026, 6, 15, 19, 55), datetime(2026, 6, 16, 6, 0)):
        car = rivian(soc=60.0, since=now - timedelta(hours=1))
        d = _plan(settings).decide(snapshot(now, soc=50.0, cars=[car]))
        assert d.mode == "day" and d.free_until is None, now
    # a pause by day (the battery low on a poor day): no end either
    snap = snapshot(NOON, soc=25.0, actual_ratio=0.3, fc_ratio=0.3, house_scale=1.5)
    d = _plan(settings).decide(snap)
    assert (d.mode, d.amps, d.free_until) == ("day", 0, None)
    # in the free hours: a full or stale car keeps its standing request, so the end goes
    # with it; without a car (or with the planner off) there is none
    night = datetime(2026, 6, 15, 22, 0)
    end = datetime(2026, 6, 16, 6, 0)
    since = night - timedelta(hours=1)
    decisions = [
        _plan(settings).decide(snapshot(night, cars=[rivian(plugged=False), tesla()])),
        _plan(settings).decide(snapshot(night, cars=[replace(rivian(since=since), soc_pct=85.0)])),
        _plan(settings).decide(snapshot(night, cars=[rivian(since=since, stale=True)])),
        _plan(replace(settings, enabled=False)).decide(snapshot(night)),
    ]
    assert [(d.mode, d.free_until) for d in decisions] == [
        ("empty", None),
        ("full", end),
        ("stale", end),
        ("off", None),
    ]
    # no free hours on the plan: no night, no end
    flat = _plan(settings).decide(snapshot(night, soc=50.0, tariff=Tariff(0.32, 0.0)))
    assert flat.mode == "day" and flat.free_until is None


def test_the_free_end_when_the_clocks_change(settings):
    """The free hours end at 06:00 local time on the nights the clocks change, which is an
    hour more or less after the evening's offset."""
    tz = ZoneInfo("America/Los_Angeles")
    for evening, end_utc in (
        (datetime(2026, 3, 7, 23, 0), datetime(2026, 3, 8, 13, 0, tzinfo=UTC)),  # PST to PDT
        (datetime(2026, 10, 31, 23, 0), datetime(2026, 11, 1, 14, 0, tzinfo=UTC)),  # PDT to PST
    ):
        now = evening.replace(tzinfo=tz)
        snap = snapshot(evening, soc=50.0)
        snap = replace(
            snap,
            now=now,
            forecast=[replace(s, start=s.start.replace(tzinfo=tz)) for s in snap.forecast],
            house_recent=[replace(s, start=s.start.replace(tzinfo=tz)) for s in snap.house_recent],
            cars=(rivian(soc=60.0, since=now - timedelta(minutes=5)),),
            forecast_issued_at=now - timedelta(hours=1),
        )
        d = _plan(settings).decide(snap)
        assert d.mode == "night" and d.free_until is not None, evening
        assert d.free_until.astimezone(UTC) == end_utc, evening
        assert (d.free_until.hour, d.free_until.minute) == (6, 0)


def test_the_free_end_to_the_minute(settings):
    now = datetime(2026, 6, 16, 1, 0)
    car = rivian(soc=60.0, since=now - timedelta(minutes=5))
    snap = snapshot(now, soc=50.0, cars=[car], tariff=Tariff.free_nights("20:00", "05:58"))
    d = _plan(settings).decide(snap)
    assert d.mode == "night" and d.free_until == datetime(2026, 6, 16, 5, 58)


def test_free_hours_past_the_horizon_give_one_end(settings):
    weekend = Tariff(0.32, 0.0, (PriceWindow(WeeklySchedule.days("00:00", "24:00", (5, 6)), 0.0),))
    p = _plan(settings)
    saturday = datetime(2026, 6, 20, 1, 0)  # free until Monday 00:00, 47 h ahead
    car = rivian(soc=60.0, since=saturday - timedelta(minutes=5))
    a = p.decide(snapshot(saturday, soc=50.0, cars=[car], tariff=weekend))
    car = replace(car, commanded_a=48)
    b = p.decide(snapshot(saturday + timedelta(minutes=1), soc=50.0, cars=[car], tariff=weekend))
    assert a.mode == b.mode == "night"
    assert a.free_until is not None and a.free_until == b.free_until
