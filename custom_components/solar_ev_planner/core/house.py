"""Today's house load on top of the 14-day profile: a 24-h level and a 2-h offset.

  expected kW at t = level * profile(t) + offset * fade(t)

level   the last 24 h of house energy against the profile for those hours, shrunk toward 1
        by ``level_prior_kwh`` pseudo-kWh (a hot day with the air conditioning on, or a week
        away, moves it; one oven does not);
offset  the lower-quartile residual of the 5-min loads over the last 2 h, fading with
        ``offset_tau_h``: a sustained jump (air conditioning) is kept, an oven or a pump
        that runs under ~90 min is ignored.
spread  the profile's day-to-day spread of the energy still to come (load_profile), for
        the caution split between solar and house.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from .load_profile import LoadProfile, LoadSample


@dataclass(frozen=True)
class HouseParams:
    level_prior_kwh: float = 4.0  # pseudo-kWh shrinking the 24-h ratio toward 1
    level_min: float = 0.7
    level_max: float = 1.6
    offset_tau_h: float = 3.0  # how long a recent offset is assumed to persist
    offset_window_h: float = 2.0
    offset_quantile: float = 0.25  # of the recent residuals (low: appliance spikes ignored)
    offset_min_kw: float = -1.0
    offset_max_kw: float = 3.0


class HouseModel:
    def __init__(
        self,
        profile: LoadProfile,
        recent: Sequence[LoadSample],
        now: datetime,
        params: HouseParams | None = None,
    ):
        p = self.p = params or HouseParams()
        self.profile = profile
        self.now = now
        act = pred = 0.0
        since = now - timedelta(hours=24)
        for s in recent:
            if since <= s.start < now:
                h = s.minutes / 60.0
                act += s.kw * h
                pred += profile.kw_at(s.start) * h
        c = p.level_prior_kwh
        self.level = max(p.level_min, min(p.level_max, (act + c) / (pred + c))) if pred > 0 else 1.0
        cut = now - timedelta(hours=p.offset_window_h)
        res = sorted(
            s.kw - self.level * profile.kw_at(s.start) for s in recent if cut <= s.start < now
        )
        self.offset = 0.0
        if len(res) >= 6:
            q = res[min(len(res) - 1, int(len(res) * p.offset_quantile))]
            self.offset = max(p.offset_min_kw, min(p.offset_max_kw, q))

    def kw_profile(self, t: datetime) -> float:
        return self.level * self.profile.kw_at(t)

    def kw(self, t: datetime, offset_until: datetime) -> float:
        """Expected house kW at t; the recent offset fades and ends at ``offset_until``."""
        v = self.level * self.profile.kw_at(t)
        if t >= offset_until or self.offset == 0.0:
            return v
        fade = math.exp(-(t - self.now).total_seconds() / 3600.0 / self.p.offset_tau_h)
        return max(0.1, v + self.offset * fade)

    def spread(self, n_bins: int) -> float:
        return self.profile.spread_kwh(self.now, n_bins)
