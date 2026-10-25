"""The Bayesian correction of the rest-of-day solar forecast."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from custom_components.solar_ev_planner.core import PvDay
from custom_components.solar_ev_planner.core.pv import PvModel, multi_day_bias

from .helpers import MONDAY, forecast, pv_so_far


def _model(hour: float, actual_ratio: float, fc_ratio: float = 1.0, **kw) -> PvModel:
    now = datetime.combine(MONDAY, datetime.min.time()) + timedelta(hours=hour)
    return PvModel(forecast(MONDAY, fc_ratio), now, pv_so_far(now, actual_ratio), **kw)


def test_no_evidence_before_sunrise():
    m = _model(5.0, 1.0)
    assert m.ratio == 1.0 and m.k == 0.0


def test_forecast_dominates_morning_actuals_dominate_afternoon():
    # the day runs at 70 % of the forecast
    morning, afternoon = _model(9.0, 0.7), _model(15.0, 0.7)
    assert morning.k < afternoon.k
    assert 0.7 < afternoon.ratio < morning.ratio < 1.0  # less sure in the morning: nearer 1
    assert afternoon.ratio < 0.9


def test_spread_shrinks_as_the_day_is_learned():
    t = datetime.combine(MONDAY, datetime.min.time()) + timedelta(hours=17)
    assert _model(15.0, 1.0).mean_sigma(t)[1] < _model(7.5, 1.0).mean_sigma(t)[1]


def test_correction_never_exceeds_p90_and_tomorrow_is_uncorrected():
    m = _model(15.0, 1.5)
    t = datetime.combine(MONDAY, datetime.min.time()) + timedelta(hours=16)
    s = m.slot_at(t)
    mean, _ = m.mean_sigma(t)
    assert mean <= s.p90 + 1e-9
    tm = t + timedelta(days=1)
    assert m.mean_sigma(tm)[0] == m.slot_at(tm).p50


def test_multi_day_bias_learns_a_persistent_ratio():
    days = [PvDay(date(2026, 6, 1) + timedelta(days=i), 40.0, 50.0) for i in range(14)]
    b, v, r = multi_day_bias(days, _model(5, 1).p)
    assert -0.3 < b < -0.15  # log(0.8) = -0.22
    assert r is not None and v < 0.08**2
    m = _model(5.0, 1.0, days=days)
    assert m.ratio < 0.85  # before sunrise the prior is the learned bias


def test_stale_forecast_widens_future_slots():
    now = datetime.combine(MONDAY, datetime.min.time()) + timedelta(hours=12)
    fresh = _model(12.0, 1.0, issued_at=now - timedelta(hours=1))
    stale = _model(12.0, 1.0, issued_at=now - timedelta(hours=15))
    t = now + timedelta(hours=2)
    assert stale.mean_sigma(t)[1] > fresh.mean_sigma(t)[1] + 0.05
    assert stale.mean_sigma(t)[0] == fresh.mean_sigma(t)[0]
