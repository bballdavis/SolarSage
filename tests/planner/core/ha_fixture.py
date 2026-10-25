"""Run planner_core on Home Assistant-shaped data (a synthetic fixture, never a real HA).

    python -m tests.planner.core.ha_fixture               # build the fixture, decide, print
    python -m tests.planner.core.ha_fixture --dump f.json # also write the fixture

``snapshot_from_ha`` is the mapping the integration's coordinator performs: entity states
(state string + attributes, as ``hass.states.get`` gives them), recorder statistics rows
(``statistics_during_period`` of the four power sensors), the free-hours Schedule helper's
weekly config, the Solcast attributes and the planner's own memory of what it commanded. The
fixture uses the owner's entity ids (core.entities) with made-up values.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
from typing import Any

from custom_components.solar_ev_planner.core import (
    RIVIAN_R1S,
    TESLA,
    BatterySpec,
    CarState,
    Measured,
    Planner,
    PriceWindow,
    RepeatingPlan,
    Settings,
    Snapshot,
    Tariff,
    WeeklySchedule,
    build_load_profile,
    parse_detailed_forecast,
    samples_from_statistics,
)
from custom_components.solar_ev_planner.core.entities import CAR_ENTITIES, SITE_ENTITIES

E = SITE_ENTITIES
SPECS = {"rivian": RIVIAN_R1S, "tesla": TESLA}
UNAVAILABLE = ("unavailable", "unknown", "", None)


def num(states: Mapping[str, Any], eid: str) -> float | None:
    st = states.get(eid)
    v = st.get("state") if st else None
    if v in UNAVAILABLE:
        return None
    try:
        return float(v)
    except TypeError, ValueError:
        return None


def on(states: Mapping[str, Any], eid: str) -> bool:
    st = states.get(eid)
    return bool(st) and st.get("state") == "on"


def snapshot_from_ha(
    now: datetime,
    states: Mapping[str, Mapping[str, Any]],
    statistics: Mapping[str, list[Mapping[str, Any]]],
    free_schedule: Mapping[str, Any],
    memory: Mapping[str, Any],
    plans: Mapping[str, list[RepeatingPlan]],
    price: float = 0.32,
    buyback: float = 0.0,
) -> Snapshot:
    """``statistics``: 5-min rows per site entity id (the last 14 days + today);
    ``memory``: {car_id: {"commanded_a": int, "plugged_since": iso | None}} kept by the
    integration; ``plans``: the dashboard's per-car plans."""
    st = statistics
    samples = samples_from_statistics(
        st[E["pv_power"]], st[E["grid_power"]], st[E["battery_power"]], st[E["wall_power"]]
    )
    today = now.date()
    profile = build_load_profile(samples, today)
    recent = [s for s in samples if now - timedelta(hours=24) <= s.start < now]
    midnight = datetime.combine(today, datetime.min.time())
    pv_rows = [r for r in st[E["pv_power"]] if _t(r["start"]) >= midnight and _t(r["start"]) < now]
    pv_today = sum(float(r["mean"]) / 1000.0 / 12.0 for r in pv_rows)
    pv_recent = sum(float(r["mean"]) for r in pv_rows[-3:]) / 1000.0 / max(1, len(pv_rows[-3:]))
    soc = num(states, E["battery_soc"]) or 0.0
    w = lambda k: num(states, E[k])  # noqa: E731
    measured = None
    if None not in (w("pv_power"), w("grid_power"), w("battery_power"), w("wall_power")):
        measured = Measured(
            w("pv_power") / 1000.0,
            w("grid_power") / 1000.0,
            w("battery_power") / 1000.0,
            w("wall_power"),
            soc,
        )
    fc = parse_detailed_forecast(
        states.get(E["forecast_today"], {}).get("attributes"),
        states.get(E["forecast_tomorrow"], {}).get("attributes"),
    )
    polled = states.get(E["forecast_last_polled"], {}).get("state")
    polled_t = _t(polled).replace(tzinfo=None) if polled not in UNAVAILABLE else None
    cars = []
    connected = on(states, E["wall_connected"])
    for cid, ent in CAR_ENTITIES.items():
        soc_c, lim = num(states, ent["soc"]), num(states, ent["limit"])
        home = (states.get(ent["location"]) or {}).get("state") == "home"
        plugged = connected and on(states, ent["plugged"]) and home
        mem = memory.get(cid, {})
        since = mem.get("plugged_since")
        cars.append(
            CarState(
                cid,
                SPECS[cid],
                soc_c or 0.0,
                lim or 80.0,
                plugged,
                datetime.fromisoformat(since) if since else None,
                int(mem.get("commanded_a", 0)),
                False,
                soc_c is None or lim is None,  # stale only when unavailable
                tuple(p for rp in plans.get(cid, []) for p in rp.deadlines(now)),
            )
        )
    free = WeeklySchedule.from_ha(free_schedule)
    return Snapshot(
        now=now,
        soc_pct=soc,
        battery=BatterySpec(capacity_kwh=num(states, E["battery_capacity"]) or 31.0),
        tariff=Tariff(price, buyback, (PriceWindow(free, 0.0, "free"),)),
        forecast=fc,
        load=profile,
        house_recent=recent,
        pv_today_kwh=pv_today,
        pv_recent_kw=pv_recent,
        measured=measured,
        cars=tuple(cars),
        forecast_issued_at=polled_t,
    )


def _t(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v))
    return datetime.fromisoformat(str(v))


# --------------------------------------------------------------------------- the fixture


def _clear(h: float) -> float:
    return 0.0 if not 6.5 < h < 20.5 else 11.0 * math.sin(math.pi * (h - 6.5) / 14.0) ** 1.6


def build_fixture(now: datetime) -> dict[str, Any]:
    """A synthetic summer Monday: Rivian plugged in at 10:00, Tesla away."""
    stats: dict[str, list[dict[str, Any]]] = {E[k]: [] for k in ("pv_power", "grid_power")}
    stats[E["battery_power"]] = []
    stats[E["wall_power"]] = []
    t = datetime.combine(now.date() - timedelta(days=14), datetime.min.time())
    while t < now:
        h = t.hour + t.minute / 60.0
        pv = _clear(h + 1 / 24) * (0.8 if t.day % 3 == 0 else 1.0) * 1000.0
        house = 0.6 + 1.6 * math.exp(-((h - 18.5) ** 2) / 3) + 0.8 * math.exp(-((h - 7.5) ** 2))
        house *= 1000.0 * (1.15 if t.weekday() >= 5 else 1.0)
        batt = max(-9000.0, min(9000.0, pv - house))
        stats[E["pv_power"]].append({"start": t.isoformat(), "mean": pv})
        stats[E["grid_power"]].append({"start": t.isoformat(), "mean": house - pv + batt})
        stats[E["battery_power"]].append({"start": t.isoformat(), "mean": batt})
        stats[E["wall_power"]].append({"start": t.isoformat(), "mean": 0.0})
        t += timedelta(minutes=5)

    def detailed(d: date) -> dict[str, Any]:
        rows = []
        for j in range(48):
            p50 = _clear(j / 2 + 0.25)
            rows.append(
                {
                    "period_start": f"{d.isoformat()}T{j // 2:02d}:{30 * (j % 2):02d}:00",
                    "pv_estimate": round(p50, 4),
                    "pv_estimate10": round(p50 * 0.75, 4),
                    "pv_estimate90": round(p50 * 1.1, 4),
                }
            )
        return {"detailedForecast": rows}

    st = lambda v, **a: {"state": v, "attributes": a}  # noqa: E731
    states = {
        E["pv_power"]: st("7800"),
        E["grid_power"]: st("-150"),
        E["battery_power"]: st("2900"),
        E["battery_soc"]: st("72"),
        E["battery_capacity"]: st("31.2"),
        E["wall_power"]: st("3.8"),
        E["wall_current"]: st("16"),
        E["wall_connected"]: st("on"),
        E["wall_contactor"]: st("on"),
        E["forecast_today"]: {"state": "52.1", "attributes": detailed(now.date())},
        E["forecast_tomorrow"]: {
            "state": "50.3",
            "attributes": detailed(now.date() + timedelta(days=1)),
        },
        E["forecast_last_polled"]: st((now - timedelta(minutes=40)).isoformat()),
        CAR_ENTITIES["rivian"]["soc"]: st("46"),
        CAR_ENTITIES["rivian"]["limit"]: st("85"),
        CAR_ENTITIES["rivian"]["plugged"]: st("on"),
        CAR_ENTITIES["rivian"]["location"]: st("home"),
        CAR_ENTITIES["tesla"]["soc"]: st("63"),
        CAR_ENTITIES["tesla"]["limit"]: st("90"),
        CAR_ENTITIES["tesla"]["plugged"]: st("off"),
        CAR_ENTITIES["tesla"]["location"]: st("not_home"),
    }
    days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    sched = {
        d: [{"from": "00:00:00", "to": "06:00:00"}, {"from": "20:00:00", "to": "23:59:59"}]
        for d in days
    }
    memory = {
        "rivian": {"commanded_a": 16, "plugged_since": (now - timedelta(hours=2)).isoformat()}
    }
    return {
        "now": now.isoformat(),
        "states": states,
        "statistics": stats,
        "free_schedule": sched,
        "memory": memory,
    }


def run(fx: dict[str, Any]) -> Any:
    now = datetime.fromisoformat(fx["now"])
    plans = {"tesla": [RepeatingPlan(80.0, "07:30", (0, 1, 2, 3, 4))]}
    snap = snapshot_from_ha(
        now, fx["states"], fx["statistics"], fx["free_schedule"], fx["memory"], plans
    )
    planner = Planner(Settings(shadow=True))
    return snap, planner.decide(snap)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", default="2026-06-15T12:00:00")
    ap.add_argument("--dump", default="")
    a = ap.parse_args()
    fx = build_fixture(datetime.fromisoformat(a.now))
    if a.dump:
        Path(a.dump).write_text(json.dumps(fx, indent=1))
    snap, d = run(fx)
    print(f"{d.now:%a %H:%M} mode={d.mode} car={d.car_id} shadow={d.shadow}")
    for c in d.commands:
        print(f"  {c.car_id:7s} amps={c.amps}  {c.reason}")
    print(f"  load profile days={snap.load.n_days}  forecast slots={len(snap.forecast)}")
    for o in d.outlooks:
        print(f"  outlook {o.car_id:7s} need {o.need_kwh:5.1f} kWh: {o.summary}")
    if d.next_change:
        n = d.next_change
        print(f"  next change {n.at:%H:%M} -> {n.amps} A ({n.why})")
    lo = min(d.trajectory, key=lambda p: p.soc_pct)
    print(f"  battery outlook: lowest {lo.soc_pct:.0f}% at {lo.t:%a %H:%M} (caution path)")
    for k, v in d.debug.items():
        print(f"  {k} = {v}")


if __name__ == "__main__":
    main()
