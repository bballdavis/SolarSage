"""The planner: one decision per call for the car on the Wall Connector.

DAY (paid hours). Every 15 minutes (and on plug-in, a new forecast or a mode change) it
predicts the home battery in 15-min steps from now until tomorrow's free hours (or tomorrow's
sunset when there are none), on a pessimistic day at the owner's caution level, and solves
for one steady car power:

    P_cap      the highest steady power such that the predicted battery never goes under the
               floor (nor below the no-car path where that is already under it), still holds
               the wanted level when free hours start (at sunset without free hours), and the
               car causes no paid import. The car holds P for ``commit_h``, then takes only
               surplus in the model, because the planner re-plans long before that.
    P_noexport the lowest steady power at which the EXPECTED day exports nothing while the car
               still wants energy: the solar the battery cannot store.
    P_fill     with a plan: the lowest steady power that reaches its target by the end of the
               solar window on the pessimistic day.
    P_spread   with free hours ahead: the Sol-Ark refills the battery from the grid in them, so
               what it would still hold when they start is the car's. The highest steady power
               held all window (not just ``commit_h``) that keeps the floor and the wanted
               level on the pessimistic day.

    P = min(P_cap, max(P_noexport, P_fill, P_spread, the car's minimum current))

So the floor is a limit on the predicted trajectory, not a trigger: when the predicted lowest
point stays above it, the battery drains now and the current stays put. The current moves
toward P slowly (a deadband, twice as wide for decreases the battery rules do not require; at
most one change per interval and ``max_step_a`` per change).

NIGHT (free hours). The car charges at its full current to its limit, from the moment the free
hours begin; the Sol-Ark's own settings refill the home battery meanwhile, so the model starts
the next paid hours with it full.

PLANS. Sun and battery (P_fill), then free hours, then full current in paid hours: now, when
the deadline falls inside the open solar window (the sun can refill the battery), otherwise in
the cheapest paid hours as late as possible.

SAFETY (every call). Paid import while the car draws for longer than the tolerance, or the
battery at the floor and discharging into the car, cuts the car by what it costs and caps it
for 30 min; a full battery that keeps exporting allows an increase before the interval is up;
an unplugged, full or stale car is never commanded.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import math
from statistics import NormalDist
from typing import Any

from .battery import Path, Step, lowest, simulate, simulate_profile
from .house import HouseModel, HouseParams
from .pv import PvModel, PvParams
from .tariff import next_flip
from .types import (
    SLOT,
    CarCommand,
    CarOutlook,
    CarPlan,
    CarState,
    Decision,
    PlannedChange,
    Settings,
    Snapshot,
    TrajectoryPoint,
)

STEP = timedelta(minutes=15)  # forward-model step and the planning cadence
FIVE = timedelta(minutes=5)


@dataclass(frozen=True)
class Params:
    """Internal constants (not owner settings). Each has one job; the simulation showed the
    result is insensitive to each within a wide range (PLANNER_DESIGN.md)."""

    deadband_a: float = 5.0  # ignore planned changes smaller than this (x2 for decreases)
    commit_h: float = 2.0  # the model holds the car's power this long, then solar only
    horizon_h: float = 44.0  # longest forward model (now to tomorrow's free start)
    import_trip_kw: float = 0.25  # paid import above this counts for the safety rule
    safety_cap_min: float = 30.0  # after a safety cut, no increase for this long
    settle_min: float = 10.0  # no safety action this soon after a change
    export_kick_min: float = 10.0  # full battery exporting this long allows an early increase
    full_soc_pct: float = 97.0
    near_limit_frac: float = 0.75  # no cut when the car needs less than this x 1 h at its rate
    min_current_spill: float = 0.0  # run at the minimum current only with this share of it spare
    pv: PvParams = field(default_factory=PvParams)
    house: HouseParams = field(default_factory=HouseParams)


def fmt_t(t: datetime | None, now: datetime) -> str:
    if t is None:
        return "-"
    return t.strftime("%H:%M") if t.date() == now.date() else t.strftime("%H:%M tomorrow")


@dataclass
class Budget:
    """One daytime solve."""

    p_kw: float  # steady car power
    p_cap: float  # highest steady power the battery rules allow
    first_kw: float  # the power now (p_kw while the car's window is open)
    path: Path  # pessimistic path at p_kw
    base: Path  # pessimistic path without the car
    steps: list[Step]  # pessimistic steps
    steps0: list[Step]  # expected-day steps
    binding: str
    min_soc: float
    min_at: datetime | None
    soc_free: float | None
    t_free: datetime | None
    t_car_end: datetime | None
    t_end: datetime
    e0: float
    debug: dict[str, Any]


@dataclass
class _Models:
    pv: PvModel
    house: HouseModel
    steps0: list[Step]
    steps: list[Step]
    t_free: datetime | None
    t_car_end: datetime | None
    t_check: datetime | None
    t_end: datetime
    z_pv: float
    z_load: float


class Planner:
    """One instance per Wall Connector. Call ``decide`` every 1-5 minutes with a fresh
    ``Snapshot``; persist ``state_dict()`` across restarts and restore it with
    ``load_state``. ``settings`` may be replaced between calls."""

    def __init__(self, settings: Settings | None = None, params: Params | None = None):
        self.settings = settings or Settings()
        self.params = params or Params()
        self._reset_session(None)
        self.last_change: datetime | None = None
        self._cache: dict[str, tuple[Any, Any]] = {}  # display models, per quarter hour

    # ------------------------------------------------------------------ state

    def _reset_session(self, key: tuple[str, datetime | None] | None) -> None:
        self.session = key
        self.mode = ""
        self.budget: Budget | None = None
        self.solved_key: tuple | None = None
        self.import_since: datetime | None = None
        self.export_since: datetime | None = None
        self.cap: tuple[int, datetime] | None = None  # safety cap (amps, until)
        self.plan_on: datetime | None = None  # deadline of the plan pushed through paid hours
        self.shadow_a: int | None = None  # the virtual car's current in shadow mode

    def state_dict(self) -> dict[str, Any]:
        """JSON-serialisable memory (session, rate limiter, plan latch)."""

        def iso(t: datetime | None) -> str | None:
            return t.isoformat() if t else None

        return {
            "session": [self.session[0], iso(self.session[1])] if self.session else None,
            "mode": self.mode,
            "last_change": iso(self.last_change),
            "plan_on": iso(self.plan_on),
            "cap": [self.cap[0], iso(self.cap[1])] if self.cap else None,
            "shadow_a": self.shadow_a,
        }

    def load_state(self, d: dict[str, Any]) -> None:
        def dt(s: str | None) -> datetime | None:
            return datetime.fromisoformat(s) if s else None

        s = d.get("session")
        self._reset_session((s[0], dt(s[1])) if s else None)
        self.mode = d.get("mode", "")
        self.last_change = dt(d.get("last_change"))
        self.plan_on = dt(d.get("plan_on"))
        c = d.get("cap")
        self.cap = (int(c[0]), dt(c[1])) if c else None
        self.shadow_a = d.get("shadow_a")

    # ------------------------------------------------------------------ entry point

    def decide(self, snap: Snapshot) -> Decision:
        s, now = self.settings, snap.now
        plugged = [c for c in snap.cars if c.plugged]
        car = max(
            plugged, key=lambda c: c.plugged_since or now - timedelta(days=3650), default=None
        )
        if not s.enabled:
            self._reset_session(None)
            return self._finish(
                snap, None, "off", None, "The planner is off; both cars are left alone."
            )
        if car is None:
            self._reset_session(None)
            return self._finish(snap, None, "empty", None, "The charger is empty.")
        key = (car.car_id, car.plugged_since)
        plug_in = key != self.session
        if plug_in:
            self._reset_session(key)
            self.shadow_a = car.commanded_a
        if s.shadow:
            car = replace(car, commanded_a=self.shadow_a or 0, pending=False)
        if car.stale:
            return self._finish(
                snap, car, "stale", None, f"The {car.spec.name}'s data is stale; not commanding it."
            )
        if car.need_kwh < 0.05:
            return self._finish(
                snap,
                car,
                "full",
                None,
                f"The {car.spec.name} is at its {car.limit_pct:.0f}% limit; nothing to command.",
            )
        safety = None
        if snap.tariff.is_free(now):
            amps, reason, dbg = self._night(snap, car)
            dbg["exempt"] = True  # free hours: full current at once
            mode = "night"
            self.import_since = self.export_since = None  # import is free: nothing to time
        else:
            amps, reason, dbg = self._day(snap, car)
            mode = "day"
            safety = dbg.pop("safety", None)
        fresh = plug_in or mode != self.mode  # a new session or mode: tell the car once
        exempt = dbg.pop("exempt", False) or fresh
        self.mode = mode
        amps_out, reason, want = self._limit(snap, car, amps, reason, exempt, fresh, dbg)
        if amps_out is not None and amps_out != car.commanded_a:
            self.last_change = now
        if s.shadow and amps_out is not None:
            self.shadow_a = amps_out
        return self._finish(snap, car, mode, amps_out, reason, dbg, safety, want)

    # ------------------------------------------------------------------ night (free hours)

    def _max_a(self, car: CarState, snap: Snapshot) -> int:
        """The car's full current on this charger: what it gets in free hours."""
        return min(car.spec.max_a, snap.charger_max_a)

    def _free_end(self, snap: Snapshot) -> datetime:
        lim = timedelta(hours=self.params.horizon_h)
        end = next_flip(snap.now, snap.tariff.is_free, lim)
        return end or snap.now + lim

    def _night(self, snap: Snapshot, car: CarState) -> tuple[int, str, dict]:
        """Free hours: the car charges at its full current to its limit; the Sol-Ark's own
        settings refill the home battery from the grid meanwhile."""
        a = self._max_a(car, snap)
        return a, f"Free hours: charging at {a} A to the {car.limit_pct:.0f}% limit.", {}

    # ------------------------------------------------------------------ day (paid hours)

    def _day(self, snap: Snapshot, car: CarState) -> tuple[int, str, dict]:
        s, now = self.settings, snap.now
        q = (now.date(), (now.hour * 60 + now.minute) // 15)
        key = (q, snap.forecast_issued_at)
        if self.budget is None or self.mode != "day" or key != self.solved_key:
            self.budget = self.solve(snap, car)
            self.solved_key = key
        b = self.budget
        dbg = dict(b.debug)
        p_sun = b.first_kw if s.daytime_solar else 0.0
        extra, plan_why = self._plan_extra(snap, car, b)
        a = self._to_amps(min(p_sun + extra, car.spec.kw(car.spec.max_a, snap.volts)), car, snap)
        dbg.update(p_sun_kw=round(p_sun, 2), plan_extra_kw=round(extra, 2))
        if plan_why:  # a plan outranks paid import: the safety cuts and slow limits do not apply
            dbg["exempt"] = True
            return a, plan_why, dbg
        safe = self._safety(snap, car)
        if safe is not None:
            cut, why, rule = safe
            self.cap = (cut, now + timedelta(minutes=self.params.safety_cap_min))
            dbg["exempt"] = True  # either way the lower current goes at once
            dbg["safety"] = rule
            if cut < a:  # otherwise the plan already wants less than the cut
                return cut, why, dbg
        if self.cap is not None and now < self.cap[1] and a > self.cap[0]:
            dbg["capped_from"] = a
            a = self.cap[0]
        if not s.daytime_solar:
            return 0, "Daytime solar charging is off; waiting for the free hours.", dbg
        conf = f"{s.caution_p():.0%}-confidence"
        if b.t_car_end is None or b.t_car_end <= now or not b.steps or not b.steps[0].car_on:
            return (
                a,
                f"No solar window now; the {car.spec.name} waits for "
                f"{'free hours' if b.t_free else 'the sun'}.",
                dbg,
            )
        if a == 0:
            return a, f"No room for the {car.spec.name}: on a {conf} forecast {b.binding}.", dbg
        return a, f"Solar charging at {a} A: on a {conf} forecast {b.binding}.", dbg

    # ------------------------------------------------------------------ models and horizon

    def _models(self, snap: Snapshot) -> _Models:
        p, s, now = self.params, self.settings, snap.now
        pv = PvModel(
            snap.forecast, now, snap.pv_today_kwh, snap.pv_days, snap.forecast_issued_at, p.pv
        )
        house = HouseModel(snap.load, snap.house_recent, now, p.house)
        z = NormalDist().inv_cdf(s.caution_p())
        # the expected day; then split z between PV and house over the stretch today's decision
        # is about (now until free hours, or the end of today's sun)
        steps0, t_free, t_car_end, t_check, t_end = self._horizon(snap, pv, house, 0.0, 1.0)
        t_split = t_check if t_check is not None and t_check > now else t_end
        sd_pv = e_load = 0.0
        for st in steps0:
            if st.t >= t_split:
                break
            m, sig = pv.mean_sigma(st.t + STEP / 2)
            sd_pv += m * sig * st.dt_h  # slots fully correlated within the day
            e_load += st.house * st.dt_h
        sd_load = house.spread(max(1, math.ceil((t_split - now) / SLOT))) if steps0 else 0.0
        sd_net = math.hypot(sd_pv, sd_load)
        z_pv = z * sd_pv / sd_net if sd_net > 0 else 0.0
        z_load = z * sd_load / sd_net if sd_net > 0 else 0.0
        qload = 1.0 + z_load * sd_load / e_load if e_load > 0 else 1.0
        steps, t_free, t_car_end, t_check, t_end = self._horizon(snap, pv, house, z_pv, qload)
        return _Models(pv, house, steps0, steps, t_free, t_car_end, t_check, t_end, z_pv, z_load)

    def _horizon(self, snap: Snapshot, pv: PvModel, house: HouseModel, z_pv: float, qload: float):
        """Forward-model steps from now to tomorrow's free start (or tomorrow's sunset), the
        car's window (sunrise to the end of today's surplus, before free hours) and the time
        the wanted battery level is checked (the next free start, else today's sunset)."""
        now, tar = snap.now, snap.tariff
        lim = timedelta(hours=self.params.horizon_h)
        t_free = None if tar.is_free(now) else next_flip(now, tar.is_free, lim)
        if t_free is not None and t_free - now > timedelta(hours=24):
            t_free = None
        t_free_end = next_flip(t_free, tar.is_free, lim) if t_free else None
        # the sun from the forecast: today's sunrise, sunset and end of surplus; tomorrow's sunset
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        sun_start = sun_end = surplus_end = sun_end_tmrw = None
        t = midnight
        while t < midnight + timedelta(days=2):
            m, _ = pv.mean_sigma(t + SLOT / 2)
            if t >= midnight + timedelta(days=1):
                sun_end_tmrw = t + SLOT if m > 0.05 else sun_end_tmrw
            elif m > 0.05:
                sun_start = sun_start or t
                sun_end = t + SLOT
                if m > house.kw_profile(t + SLOT / 2):
                    surplus_end = t + SLOT
            t += SLOT
        win_start = sun_start or midnight
        t_car_end = min([x for x in (t_free, surplus_end) if x is not None], default=None)
        t_check = t_free if t_free is not None else sun_end
        off_until = t_free or (sun_end if sun_end and sun_end > now else now)
        r_now = None  # measured clearness, persisted for the first 30 min
        s_now = pv.slot_at(now)
        if s_now is not None and s_now.p50 > 0.3:
            r_now = max(0.0, min(1.6, snap.pv_recent_kw / s_now.p50))
        steps: list[Step] = []
        t, t_commit = now, now + timedelta(hours=self.params.commit_h)
        while t < now + lim:
            if t_free is None and sun_end_tmrw is not None and t >= sun_end_tmrw:
                break  # no free hours: the check ends at tomorrow's sunset
            if t_free_end is not None and t > t_free_end and tar.is_free(t):
                break  # tomorrow's free hours: the drain ends there
            mid = t + STEP / 2
            m, sig = pv.mean_sigma(mid)
            if r_now is not None and t < now + 2 * STEP:
                sl = pv.slot_at(mid)
                pv_kw = sl.p50 * r_now if sl else 0.0
            else:
                pv_kw = m * math.exp(-z_pv * sig)
            price = tar.import_price(mid)
            steps.append(
                Step(
                    t,
                    STEP.total_seconds() / 3600.0,
                    pv_kw,
                    house.kw(mid, off_until) * qload,
                    price > 0,
                    t_car_end is not None and win_start <= t < t_car_end,
                    t < t_commit,
                    price,
                    price <= 0.0,
                )
            )
            t += STEP
        return steps, t_free, t_car_end, t_check, t

    # ------------------------------------------------------------------ the budget solve

    def solve(self, snap: Snapshot, car: CarState) -> Budget:
        now, bat, s = snap.now, snap.battery, self.settings
        md = self._models(snap)
        steps, steps0, t_check = md.steps, md.steps0, md.t_check
        e0 = bat.kwh(snap.soc_pct)
        p_max = car.spec.kw(min(car.spec.max_a, snap.charger_max_a), snap.volts)
        need = car.need_kwh
        base = simulate(steps, e0, 0.0, need, bat)
        floor_e = bat.kwh(s.floor_pct)
        want_e = bat.kwh(s.wanted_at_free_start_pct)
        k_check = None  # the step whose end is nearest the check time
        if t_check is not None and t_check > now and steps:
            k_check = max(
                [k for k, st in enumerate(steps) if st.t + STEP / 2 <= t_check], default=0
            )
        slack = 0.002 * bat.capacity_kwh
        thr = []  # the floor rule, step by step
        for k, x in enumerate(base.e):
            start = base.e[k - 1] if k else e0
            thr.append((x if start < floor_e else min(floor_e, x)) - slack)
        thr_check = min(want_e, base.e[k_check]) - slack if k_check is not None else None

        def broken(path: Path) -> str:
            """Which battery rule the path breaks ('' = none)."""
            for k, x in enumerate(path.e):
                if x < thr[k]:
                    return f"floor@{k}"
            if thr_check is not None and path.e[k_check] < thr_check:
                return "check"
            return "paid" if path.paid_kwh - base.paid_kwh > 0.05 else ""

        def run(p: float, which: list[Step] = steps, held: bool = False) -> Path:
            return simulate(which, e0, p, need, bat, held)

        p_cap = p_spread = 0.0
        refill = any(st.refill for st in steps)
        spreading = False  # the battery's spare energy sets the power
        if not any(st.car_on for st in steps) or need <= 0:
            p_kw, fail = 0.0, "nowindow"
        else:
            # P_cap: the highest steady power the battery rules allow (pessimistic day)
            if not broken(run(p_max)):
                p_cap, fail = p_max, "max"
            else:
                p_cap = lowest(lambda p: bool(broken(run(p))), 0.0, p_max, 13)
                p_cap = max(0.0, p_cap - p_max / 8192)
                fail = broken(run(min(p_max, p_cap + 0.25)))
            # P_fill (plans only): the plan's target by the end of the window, pessimistic day
            need_fill, p_fill = self._plan_need(car, now), 0.0
            if need_fill > 0.1:
                if run(p_cap).car_kwh >= need_fill - 0.1:
                    p_fill = lowest(lambda p: run(p).car_kwh >= need_fill - 0.1, 0.0, p_cap)
                else:
                    p_fill = p_cap
            # P_noexport: the solar the battery cannot store, on the expected day
            p_noexp = 0.0
            if run(0.0, steps0).export_unmet_kwh > 0.5:
                p_noexp = p_cap
                if run(p_cap, steps0).export_unmet_kwh <= 0.5:
                    p_noexp = lowest(lambda p: run(p, steps0).export_unmet_kwh <= 0.5, 0.0, p_cap)
            # P_spread (refill only): tonight's free hours fill the battery from the grid, so
            # what it holds above the wanted level when they start is worth nothing. The car
            # gets it spread over the window: the highest power held all window (not just the
            # commitment period) that keeps the battery rules on the cautious day.
            if refill and p_cap > 0.0:
                if not broken(run(p_cap, held=True)):
                    p_spread = p_cap
                else:
                    p_spread = lowest(lambda p: bool(broken(run(p, held=True))), 0.0, p_cap, 13)
                    p_spread = max(0.0, p_spread - p_max / 8192)
            p_min = car.spec.kw(car.spec.min_a, snap.volts)
            if max(p_noexp, p_spread) < self.params.min_current_spill * p_min and p_fill <= 0.0:
                p_min = 0.0  # not enough spill to justify the car's minimum from the battery
            p_kw = min(p_cap, max(p_fill, p_noexp, p_spread, p_min))
            spreading = p_spread > 0.0 and p_spread >= max(p_fill, p_noexp, p_min)
            if p_kw < p_cap - 0.05:
                if p_kw <= 0.0:
                    fail = "nospill"
                elif p_fill >= max(p_noexp, p_spread, p_min):
                    fail = "fill"
                elif p_spread >= max(p_noexp, p_min):
                    fail = "spread"
                else:
                    fail = "noexport" if p_noexp >= p_min else "min"
        best = run(p_kw, held=spreading)  # spreading: the power is held all afternoon
        first = p_kw if steps and steps[0].car_on else 0.0
        k_min = min(range(len(best.e)), key=lambda k: best.e[k]) if best.e else None
        min_soc = bat.pct(best.e[k_min]) if k_min is not None else snap.soc_pct
        min_at = steps[k_min].t + STEP if k_min is not None else None
        soc_free = bat.pct(best.e[k_check]) if k_check is not None else None
        dbg = dict(
            p_plan_kw=round(p_kw, 2),
            p_cap_kw=round(p_cap, 2),
            p_spread_kw=round(p_spread, 2),
            min_soc=round(min_soc, 1),
            soc_free=None if soc_free is None else round(soc_free, 1),
            pv_ratio=round(md.pv.ratio, 3),
            pv_seen=round(md.pv.seen, 2),
            fc_age_h=round(md.pv.age_h, 1),
            load_level=round(md.house.level, 3),
            load_off=round(md.house.offset, 2),
            z_pv=round(md.z_pv, 2),
            z_load=round(md.z_load, 2),
            horizon_end=md.t_end.strftime("%d %H:%M"),
        )
        b = Budget(
            p_kw,
            p_cap,
            first,
            best,
            base,
            steps,
            steps0,
            "",
            min_soc,
            min_at,
            soc_free,
            md.t_free,
            md.t_car_end,
            md.t_end,
            e0,
            dbg,
        )
        b.binding = self._binding(fail, b, t_check, now)
        return b

    def _binding(self, fail: str, b: Budget, t_check: datetime | None, now: datetime) -> str:
        s = self.settings
        low = f"the battery bottoms at {b.min_soc:.0f}% at {fmt_t(b.min_at, now)}"
        allow = f"(the battery rules would allow {b.p_cap:.1f} kW)"
        if fail == "fill":
            return f"a steady rate meets the plan by {fmt_t(b.t_car_end, now)} and {low} {allow}"
        if fail == "noexport":
            return f"a steady rate takes the solar the battery cannot store and {low} {allow}"
        if fail == "spread":
            return (
                f"a steady rate through the afternoon uses the battery down to "
                f"{b.soc_free:.0f}% at {fmt_t(t_check, now)}, before the free hours refill it "
                f"{allow}"
            )
        if fail == "min":
            return f"the car's minimum current is enough and {low} {allow}"
        if fail == "nospill":
            return "there is no solar the battery cannot store, so the battery keeps its energy"
        if fail == "max":
            return f"{low} even with the car at its maximum"
        if fail == "nowindow":
            return "there is no solar window left"
        if fail.startswith("floor@"):
            t = b.steps[int(fail[6:])].t + STEP
            if b.min_soc < s.floor_pct - 0.5:
                return (
                    f"the battery is below the {s.floor_pct:.0f}% floor until "
                    f"{fmt_t(t, now)}, so the car only gets what it cannot store"
                )
            return (
                f"more would take the battery under the {s.floor_pct:.0f}% floor at {fmt_t(t, now)}"
            )
        if fail == "check":
            return (
                f"the battery holds {b.soc_free:.0f}% at {fmt_t(t_check, now)} (wanted "
                f"{s.wanted_at_free_start_pct:.0f}%) and {low}"
            )
        if fail == "paid":
            return "more would draw paid grid power"
        return low

    # ------------------------------------------------------------------ plans

    @staticmethod
    def _next_plan(car: CarState, now: datetime) -> CarPlan | None:
        """The earliest plan that is not met yet."""
        for pl in sorted(car.plans, key=lambda x: x.deadline):
            if pl.deadline > now and car.need_for(pl.target_pct) > 0.1:
                return pl
        return None

    def _plan_need(self, car: CarState, now: datetime) -> float:
        pl = self._next_plan(car, now)
        return car.need_for(pl.target_pct) if pl else 0.0

    def _plan_extra(self, snap: Snapshot, car: CarState, b: Budget) -> tuple[float, str]:
        """Full current from paid hours when sun, battery and free hours together cannot reach
        a plan. Any car draw empties the home battery first, so the push goes where the battery
        recovers best: for a deadline inside the solar window, now (the sun refills the battery
        before the evening); otherwise in the cheapest paid hours as late as possible."""
        now, tar = snap.now, snap.tariff
        for pl in sorted(car.plans, key=lambda x: x.deadline):
            if pl.deadline <= now:
                continue
            need = car.need_for(pl.target_pct)
            if need < 0.2:
                continue
            e_sun = 0.0
            if self.settings.daytime_solar:
                for st, kw in zip(b.steps, b.path.car_kw, strict=False):
                    if st.t + STEP <= pl.deadline:
                        e_sun += kw * st.dt_h
            free_h = 0.0
            paid: list[tuple[float, datetime, float]] = []  # price, start, hours
            t = now
            while t < pl.deadline:
                d = min(STEP, pl.deadline - t).total_seconds() / 3600.0
                if tar.is_free(t):
                    free_h += d
                else:
                    paid.append((round(tar.import_price(t), 4), t, d))
                t += STEP
            short = need - e_sun - free_h * car.spec.kw(self._max_a(car, snap), snap.volts)
            if short <= (-1.0 if self.plan_on == pl.deadline else 0.2):
                continue
            p_room = max(0.5, car.spec.kw(car.spec.max_a, snap.volts) - b.p_kw)
            why = (
                f"Plan {pl.target_pct:.0f}% by {fmt_t(pl.deadline, now)}: sun and free hours "
                f"cover {max(0.0, need - short):.0f} of {need:.0f} kWh"
            )
            sun_now = bool(b.steps) and b.steps[0].car_on  # the solar window is open now
            early = sun_now and b.t_car_end is not None and pl.deadline <= b.t_car_end
            if early or self.plan_on == pl.deadline:  # once started, keep going (hysteresis)
                self.plan_on = pl.deadline
                return p_room, (
                    f"{why}; charging the rest at full current now, while the sun "
                    "can still refill the battery."
                    if early
                    else f"{why}; charging the rest at full current until the deadline."
                )
            left = short * 1.1  # 10 % slack: the house and the battery share the power
            for price, start, hrs in sorted(paid, key=lambda x: (x[0], -x[1].timestamp())):
                if left <= 0:
                    break
                if start == now:
                    self.plan_on = pl.deadline
                    return p_room, (
                        f"{why}; charging the rest at full current in the last "
                        f"${price:.2f} hours before the deadline."
                    )
                left -= hrs * p_room
            return 0.0, ""
        return 0.0, ""

    # ------------------------------------------------------------------ safety rules

    def _safety(self, snap: Snapshot, car: CarState) -> tuple[int, str, str] | None:
        m, now, s, p = snap.measured, snap.now, self.settings, self.params
        if m is None:
            return None
        price = snap.tariff.import_price(now - FIVE)
        imp = max(0.0, m.grid_kw)
        if price > 0 and imp > p.import_trip_kw and m.car_kw > 0.5:
            self.import_since = self.import_since or now - FIVE
        else:
            self.import_since = None
        full = m.soc_pct >= p.full_soc_pct or m.battery_kw >= snap.battery.max_charge_kw - 0.3
        if -m.grid_kw > 0.4 and full and car.need_kwh > 0.5:
            self.export_since = self.export_since or now - FIVE
        else:
            self.export_since = None
        cur = car.commanded_a
        settling = self.last_change is not None and now - self.last_change < timedelta(
            minutes=p.settle_min
        )
        if cur <= 0 or car.pending or settling:
            return None
        v = snap.volts
        tol = s.paid_tolerance_min * 60.0
        if self.import_since and (now - self.import_since).total_seconds() >= tol:
            new = self._down(car, cur - math.ceil(imp * 1000.0 / v) - 1)
            mins = (now - self.import_since).total_seconds() / 60.0
            return (
                new,
                f"Paid grid import of {imp:.1f} kW for {mins:.0f} min with the car "
                f"drawing: reducing to {new} A.",
                "paid_import",
            )
        if price > 0 and m.soc_pct <= s.floor_pct and m.battery_kw < -0.3 and m.car_kw > 0.5:
            new = self._down(car, cur - math.ceil(-m.battery_kw * 1000.0 / v))
            return (
                new,
                f"Battery at the {s.floor_pct:.0f}% floor and discharging "
                f"{-m.battery_kw:.1f} kW: reducing to {new} A.",
                "battery_floor",
            )
        return None

    def _early_increase_ok(self, now: datetime) -> bool:
        return self.export_since is not None and now - self.export_since >= timedelta(
            minutes=self.params.export_kick_min
        )

    # ------------------------------------------------------------------ amps and rate limits

    @staticmethod
    def _down(car: CarState, a: float) -> int:
        """The legal current at or below a (0 below the car's minimum)."""
        sp = car.spec
        if a < sp.min_a:
            return 0
        return sp.min_a + int((a - sp.min_a) // sp.step_a) * sp.step_a

    def _to_amps(self, p_kw: float, car: CarState, snap: Snapshot) -> int:
        sp = car.spec
        a = min(p_kw * 1000.0 / snap.volts, sp.max_a, snap.charger_max_a)
        if a >= sp.min_a:
            return self._down(car, a)
        # hysteresis: a running car holds its minimum down to 60 % of it
        return sp.min_a if car.commanded_a > 0 and a >= 0.6 * sp.min_a else 0

    def _limit(
        self,
        snap: Snapshot,
        car: CarState,
        want: int,
        reason: str,
        exempt: bool,
        fresh: bool,
        dbg: dict,
    ) -> tuple[int | None, str, int]:
        """(amps to send or None, reason, the current the plan wants). An unchanged current
        is sent again only on a new session or mode (the executor re-sends a request that
        Rivian schedule control dropped, and never repeats a Tesla command the owner
        overrode)."""
        now, s, cur = snap.now, self.settings, car.commanded_a
        dbg["want_a"] = want
        if want == cur and not fresh:
            return None, reason, want
        if exempt or want == cur:
            return want, reason, want
        since = (now - self.last_change).total_seconds() if self.last_change else 1e9
        if since < s.min_change_min * 60.0 and not (want > cur and self._early_increase_ok(now)):
            nxt = self.last_change + timedelta(minutes=s.min_change_min)
            return (
                None,
                f"Holding {cur} A until {nxt:%H:%M} (minimum time between changes); "
                f"planned {want} A.",
                want,
            )
        db = self.params.deadband_a
        if cur > 0 and want < cur:
            cur_kw = car.spec.kw(cur, snap.volts)
            if car.need_kwh <= cur_kw * self.params.near_limit_frac:
                return (
                    None,
                    f"Holding {cur} A: the {car.spec.name} reaches its {car.limit_pct:.0f}% "
                    "limit within 45 min and stops by itself.",
                    want,
                )
            # a decrease the battery rules do not require needs twice the deadband: a little
            # more current only uses battery buffer that P_cap already protects
            b = self.budget
            if want > 0 and b is not None and cur_kw <= b.p_cap + 0.05 and cur - want < 2 * db:
                return (
                    None,
                    f"Holding {cur} A: the planned {want} A is within the {2 * db:.0f} A "
                    "deadband for decreases the battery rules do not require.",
                    want,
                )
        if cur > 0 and want > 0:
            if abs(want - cur) < db:
                return (
                    None,
                    f"Holding {cur} A: the planned {want} A is within the {db:.0f} A deadband.",
                    want,
                )
            step = max(car.spec.step_a, s.max_step_a - s.max_step_a % car.spec.step_a)
            if abs(want - cur) > step:
                new = cur + step if want > cur else cur - step
                head = f"at {want} A:"
                if head in reason:
                    reason = reason.replace(head, f"at {new} A (on the way to {want} A):", 1)
                else:
                    reason = reason.rstrip(".") + f"; {new} A now, on the way to {want} A."
                return new, reason, want
        return want, reason, want

    # ------------------------------------------------------------------ outputs

    def _finish(
        self,
        snap: Snapshot,
        car: CarState | None,
        mode: str,
        amps: int | None,
        reason: str,
        dbg: dict | None = None,
        safety: str | None = None,
        want: int | None = None,
    ) -> Decision:
        cid = car.car_id if car else None
        cmds = []
        for c in snap.cars:
            if c.car_id == cid:
                cmds.append(_cmd(cid, amps, reason))
            elif c.plugged:
                cmds.append(_cmd(c.car_id, None, "Another car is on the charger."))
            else:
                cmds.append(_cmd(c.car_id, None, f"The {c.spec.name} is not on the charger."))
        budget = self.budget if (mode == "day" and car is not None) else None
        traj = self._trajectory(snap, car, mode, amps, budget)
        outlooks = tuple(self._outlook(snap, c, c.car_id == cid, budget) for c in snap.cars)
        nxt = self._next_change(snap, car, mode, amps, want, budget)
        tonight = _tonight_car(outlooks)
        d = dict(dbg or {})
        if tonight:
            d["tonight_car"] = tonight
        return Decision(
            snap.now,
            mode,
            cid,
            tuple(cmds),
            reason,
            self.settings.shadow,
            traj,
            outlooks,
            nxt,
            safety,
            d,
            self._free_end(snap) if mode == "night" else None,
        )

    def _trajectory(
        self,
        snap: Snapshot,
        car: CarState | None,
        mode: str,
        amps: int | None,
        b: Budget | None,
    ) -> tuple[TrajectoryPoint, ...]:
        """The predicted battery with the planned car power (caution and expected day) and
        without the car, through the horizon."""
        bat = snap.battery
        if b is not None and b.steps:
            steps, steps0 = b.steps, b.steps0
            car_p = list(b.path.car_kw)
            e_q, e_none = b.path.e, b.base.e
            e0 = b.e0
        else:
            md = self._cached("models", snap, lambda: self._models(snap))
            steps, steps0 = md.steps, md.steps0
            e0 = bat.kwh(snap.soc_pct)
            car_p = [0.0] * len(steps)
            if car is not None and mode == "night":  # full current in the free hours
                kw = car.spec.kw(self._max_a(car, snap), snap.volts)
                left = car.need_kwh
                for k, st in enumerate(steps):
                    if st.refill and left > 0:
                        car_p[k] = min(kw, left / st.dt_h)
                        left -= car_p[k] * st.dt_h
            e_q = simulate_profile(steps, e0, car_p, bat)
            e_none = simulate_profile(steps, e0, [0.0] * len(steps), bat)
        e_mean = simulate_profile(steps0, e0, car_p, bat)
        out = []
        for k, st in enumerate(steps):
            out.append(
                TrajectoryPoint(
                    st.t + STEP,
                    bat.pct(e_q[k]),
                    bat.pct(e_mean[k]),
                    bat.pct(e_none[k]),
                    st.pv,
                    steps0[k].pv if k < len(steps0) else st.pv,
                    st.house,
                    car_p[k],
                    st.price,
                )
            )
        return tuple(out)

    def _free_capacity(self, snap: Snapshot, car: CarState, until: datetime | None) -> float:
        """kWh the next free period can put into the car before ``until`` (full current all
        through it, as _night does)."""
        t0, t1 = self._cached("free", snap, lambda: self._next_free(snap))
        if t0 is None:
            return 0.0
        if until is not None:
            t1 = min(t1, until)
        if t1 <= t0:
            return 0.0
        return (t1 - t0).total_seconds() / 3600.0 * car.spec.kw(self._max_a(car, snap), snap.volts)

    def _next_free(self, snap: Snapshot) -> tuple[datetime | None, datetime | None]:
        """The next free period (now if free now): (start, end)."""
        now, tar = snap.now, snap.tariff
        lim = timedelta(hours=self.params.horizon_h)
        t0 = now if tar.is_free(now) else next_flip(now, tar.is_free, lim)
        if t0 is None:
            return None, None
        return t0, next_flip(t0, tar.is_free, lim) or t0 + lim

    def _cached(self, name: str, snap: Snapshot, make):
        """Display-only values, recomputed once per quarter hour or on a new forecast."""
        now = snap.now
        key = (now.date(), (now.hour * 60 + now.minute) // 15, snap.forecast_issued_at)
        hit = self._cache.get(name)
        if hit is None or hit[0] != key:
            hit = (key, make())
            self._cache[name] = hit
        return hit[1]

    def _outlook(
        self, snap: Snapshot, car: CarState, plugged: bool, b: Budget | None
    ) -> CarOutlook:
        now = snap.now
        pl = self._next_plan(car, now)
        target = pl.target_pct if pl else car.limit_pct
        by = pl.deadline if pl else None
        need = car.need_for(target)
        solar = 0.0
        if plugged and b is not None and self.settings.daytime_solar:
            for st, kw in zip(b.steps, b.path.car_kw, strict=False):
                if by is None or st.t + STEP <= by:
                    solar += kw * st.dt_h
            solar = min(need, solar)
        free = min(need - solar, self._free_capacity(snap, car, by))
        paid = short = 0.0
        rest = need - solar - free
        if rest > 0.1 and pl is not None:
            hrs = 0.0
            t = now
            while t < pl.deadline:
                if not snap.tariff.is_free(t):
                    hrs += min(STEP, pl.deadline - t).total_seconds() / 3600.0
                t += STEP
            paid = min(rest, hrs * car.spec.kw(car.spec.max_a, snap.volts) * 0.9)
            short = rest - paid
        elif rest > 0.1:
            short = rest  # no plan: whatever the free hours leave waits for the next day
        if need < 0.1:
            summary = "at its plan target" if pl else "at its charge limit"
        elif solar >= need - 0.1:
            summary = "covered by sun"
        else:
            parts = []
            if solar >= 0.5:
                parts.append(f"about {solar:.0f} kWh from the sun")
            if free >= 0.5:
                parts.append(f"about {free:.0f} kWh in free hours")
            if paid >= 0.5:
                parts.append(f"about {paid:.0f} kWh paid")
            if short >= 0.5:
                parts.append(f"{short:.0f} kWh {'short' if pl else 'left for later'}")
            summary = ", ".join(parts) or "nothing planned"
        return CarOutlook(
            car.car_id,
            plugged,
            round(need, 2),
            target,
            by,
            round(solar, 2),
            round(free, 2),
            round(paid, 2),
            round(max(0.0, short), 2),
            summary,
        )

    def _next_change(
        self,
        snap: Snapshot,
        car: CarState | None,
        mode: str,
        amps: int | None,
        want: int | None,
        b: Budget | None,
    ) -> PlannedChange | None:
        if car is None or mode in ("off", "empty", "stale", "full"):
            return None
        s, now = self.settings, snap.now
        cur = amps if amps is not None else car.commanded_a
        if want is not None and amps is None and want != cur and self.last_change is not None:
            at = max(now, self.last_change + timedelta(minutes=s.min_change_min))
            return PlannedChange(
                at, want, "the planned current, after the minimum time between changes"
            )
        if mode == "day" and b is not None:
            for st, kw in zip(b.steps, b.path.car_kw, strict=False):
                if st.refill:
                    break  # the free hours: full current, below
                a = self._to_amps(kw, replace(car, commanded_a=cur), snap) if st.car_on else 0
                if (a == 0) != (cur == 0) or abs(a - cur) >= self.params.deadband_a:
                    if a > 0:
                        why = "solar surplus rises" if a > cur else "solar surplus falls"
                    elif st.car_on:
                        why = f"the {car.spec.name} has what the sun can give today"
                    else:
                        why = "the solar window ends"
                    return PlannedChange(max(now, st.t), a, why)
            max_a = self._max_a(car, snap)
            if b.t_free is not None and car.need_kwh > 0.5 and max_a != cur:
                return PlannedChange(b.t_free, max_a, "the free hours start")
            return None
        if mode == "night" and cur > 0:
            t_done = now + timedelta(hours=car.need_kwh / car.spec.kw(cur, snap.volts))
            end = self._free_end(snap)
            if t_done < end:
                return PlannedChange(t_done, 0, f"the {car.spec.name} reaches its limit")
            return PlannedChange(end, 0, "the free hours end")
        return None


def _cmd(car_id: str, amps: int | None, reason: str) -> CarCommand:
    return CarCommand(car_id, amps, reason)


def _tonight_car(outlooks: tuple[CarOutlook, ...]) -> str | None:
    """The car that should be on the charger in the next free hours: the one with a plan that
    needs them, else the one that needs the most energy."""
    cand = [o for o in outlooks if o.need_kwh - o.solar_kwh > 1.0]
    if not cand:
        return None
    return max(cand, key=lambda o: (o.by is not None, o.need_kwh - o.solar_kwh)).car_id
