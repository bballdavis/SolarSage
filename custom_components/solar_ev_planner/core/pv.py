"""Solar: Solcast's half hours as log-normals, corrected by today's actual production.

Each slot's actual power is modelled as ``p50 * exp(b + e)``: ``b`` is the day's forecast
bias (shared by every slot of the day: a day that runs 20 % under the forecast at 10:00 tends
to stay under it), ``e`` the slot's own cloud noise. Solcast states the spread itself:
``sigma = ln(p90 / p10) / (2 * 1.2816)``.

  prior     b ~ N(b0, v0). b0 is the slow multi-day bias (a Kalman random walk over past days'
            log(actual / forecast in force), from ``PvDay``s; 0 without them). v0 is
            Solcast's stated spread for today (energy-weighted), blended with the past days'
            day-to-day variance once there are a few of them, plus the drift of b0.
  evidence  today so far: y = ln(actual kWh / forecast kWh in force for the hours gone), with
            noise ``obs_noise^2 / (fraction of today's forecast energy already seen)``.
  posterior the conjugate normal update; the rest of today is p50 * exp(b_post), capped at
            p90 (the correction never forecasts more than Solcast's optimistic case), and each
            slot's spread shrinks as the day bias becomes known. In the morning the forecast
            dominates, by afternoon today's real behaviour does.
  stale     a forecast polled more than ``stale_after_h`` ago widens every future slot's
            spread (Solcast polls failed; its lead time is longer than it says).

Tomorrow keeps Solcast's own spread around p50 * exp(b0).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from .types import SLOT, ForecastSlot, PvDay

Z10 = 1.2815515655446004  # z of the 10th / 90th percentile


@dataclass(frozen=True)
class PvParams:
    obs_noise: float = 0.08  # log-sd of a whole day's actual/forecast ratio around its bias
    slot_share: float = 0.35  # share of a slot's spread that today's actuals cannot explain
    default_sigma: float = 0.30  # when a slot states no spread
    bias_drift: float = 0.05  # day-to-day drift of the multi-day bias (log sd)
    bias_prior: float = 0.08  # prior sd of the multi-day bias
    stale_after_h: float = 3.0  # forecast older than this widens future slots
    stale_sigma_per_12h: float = 0.15  # extra log-sd per 12 h of staleness


def slot_sigma(s: ForecastSlot, default: float = 0.30) -> float:
    """Solcast's stated log spread of one slot."""
    if s.p10 > 0.02 and s.p90 > s.p10:
        return min(1.2, math.log(s.p90 / s.p10) / (2 * Z10))
    return default


def multi_day_bias(days: Sequence[PvDay], p: PvParams) -> tuple[float, float, float | None]:
    """Kalman random walk over past days: (bias, its variance, day-to-day variance or None)."""
    ys = [
        math.log((d.actual_kwh + 0.2) / (d.forecast_kwh + 0.2))
        for d in sorted(days, key=lambda d: d.day)
        if d.forecast_kwh >= 3.0
    ][-14:]
    r = None
    if len(ys) >= 3:
        mu = sum(ys) / len(ys)
        r = min(0.4**2, max(0.04**2, sum((y - mu) ** 2 for y in ys) / (len(ys) - 1)))
    b, v = 0.0, p.bias_prior**2
    rr = r if r is not None else 0.15**2
    for y in ys:
        v += p.bias_drift**2
        k = v / (v + rr)
        b += k * (y - b)
        v *= 1 - k
    return b, v, r


class PvModel:
    """Corrected expected solar and its log spread for any time in the forecast."""

    def __init__(
        self,
        slots: Sequence[ForecastSlot],
        now: datetime,
        pv_today_kwh: float,
        days: Sequence[PvDay] = (),
        issued_at: datetime | None = None,
        params: PvParams | None = None,
    ):
        p = self.p = params or PvParams()
        self.slots = sorted(slots, key=lambda s: s.start)
        self.now = now
        self.t0 = self.slots[0].start if self.slots else now
        self.today = now.date()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.b0, v_b, r_hist = multi_day_bias(days, p)
        e_day = f_el = s_w = 0.0
        for s in self.slots:
            if s.start.date() != self.today:
                continue
            e_day += s.p50 * 0.5
            s_w += s.p50 * 0.5 * slot_sigma(s, p.default_sigma)
            el = (min(now, s.end) - max(midnight, s.start)).total_seconds() / 3600.0
            if el > 0:
                f_el += s.p50 * el  # forecast energy in force for the hours already gone
        sigma_day = s_w / e_day if e_day > 0.5 else p.default_sigma
        v0 = sigma_day**2 if r_hist is None else 0.5 * sigma_day**2 + 0.5 * r_hist
        v0 += v_b + p.bias_drift**2
        k, bias = 0.0, self.b0
        self.seen = f_el / e_day if e_day > 0 else 0.0
        if f_el > 0.3 and e_day > 1.0:
            y = math.log((pv_today_kwh + 0.3) / (f_el + 0.3))
            var_o = p.obs_noise**2 / min(1.0, f_el / e_day)
            k = v0 / (v0 + var_o)  # weight of today's evidence
            bias = self.b0 + k * (y - self.b0)
        self.bias = max(-1.2, min(0.6, bias))
        self.ratio = math.exp(self.bias)
        self.k = k
        # what is left of a slot's spread once the day bias is (partly) known
        self.shrink = math.sqrt(p.slot_share + (1.0 - p.slot_share) * (1.0 - k))
        age_h = (now - issued_at).total_seconds() / 3600.0 if issued_at is not None else 0.0
        self.age_h = max(0.0, age_h)
        extra = max(0.0, self.age_h - p.stale_after_h) / 12.0 * p.stale_sigma_per_12h
        self.stale_var = extra * extra

    def slot_at(self, t: datetime) -> ForecastSlot | None:
        i = int((t - self.t0) / SLOT)
        return self.slots[i] if 0 <= i < len(self.slots) else None

    def mean_sigma(self, t: datetime) -> tuple[float, float]:
        """Expected kW at t (today corrected by today's production) and its log spread."""
        s = self.slot_at(t)
        if s is None or s.p50 <= 0.0:
            return 0.0, 0.0
        sig = slot_sigma(s, self.p.default_sigma)
        if s.start.date() == self.today:
            mean = min(s.p50 * self.ratio, max(s.p50, s.p90))
            sig *= self.shrink
        else:
            mean = min(s.p50 * math.exp(self.b0), max(s.p50, s.p90))
        if self.stale_var > 0.0 and t >= self.now:
            sig = math.sqrt(sig * sig + self.stale_var)
        return mean, sig

    def sun_times(self, t0: datetime, t1: datetime, min_kw: float = 0.05) -> list[datetime]:
        """Starts of the half hours in [t0, t1) with expected solar above min_kw."""
        out, t = [], t0
        while t < t1:
            if self.mean_sigma(t + SLOT / 2)[0] > min_kw:
                out.append(t)
            t += timedelta(minutes=30)
        return out
