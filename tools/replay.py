"""What would the planner have done on these real days? Replay the planner core over a
snapshot (or live data) and write a CSV plus a self-contained HTML chart.

    python -m tools.replay tools/out/snapshot_2026-10-18_2026-10-24.json
    python -m tools.replay --live --days 2              # pull the last 2 days, then replay
    python -m tools.replay SNAP --whatif --set caution=balanced --step 15

Every ``--step`` minutes (5) it builds the planner's ``Snapshot`` from the recorded data, as
the integration would from live states, calls ``Planner.decide`` in shadow mode and records
the decision: mode, command, the planner's current, its reason, the safety rule that acted,
and the battery level it predicted ``--horizon`` hours (3) ahead.

Two ways to run it:

- **shadow** (default, open loop): the planner sees what really happened, including whatever
  current the car really drew. This is what shadow mode will show in Home Assistant.
- **what-if** (``--whatif``, closed loop): the car draws what the planner commands, and a
  simple Sol-Ark model (the sun serves the house, the battery takes the surplus and covers the
  deficit within 9 kW and its reserve, it holds in free hours, the grid takes the rest) turns
  the real solar and house load into the battery level, grid power and costs the planner
  would have caused. The planner then sees that world, not the real one.

The forecast in force at each step comes from the recorded issues when there are any, else
from an approximation (see timeline.py); the CSV and the chart name the source per step.
Outputs go to ``tools/out/`` (git-ignored) unless ``--out`` says otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
from bisect import bisect_left
from collections.abc import Mapping, Sequence
import csv
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime, time, timedelta
import json
from pathlib import Path
import sys
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import config as tool_config  # noqa: E402
from tools.chart import render_html  # noqa: E402
from tools.config import load_config  # noqa: E402
from tools.core import CoreNotFound, load_core  # noqa: E402
from tools.hass_client import HassClient, HassError  # noqa: E402
from tools.snapshot import day_range, pull, zone  # noqa: E402
from tools.timeline import FIVE, Timeline, floor5  # noqa: E402

CAR_VOLTS = 240.0


@dataclass
class Row:
    """One replay step (one CSV row). Powers kW, levels %, current A."""

    t: datetime
    mode: str
    car: str
    command_a: int | None  # what the planner would send now (None = nothing)
    planned_a: int  # the planner's current after this step (its virtual car)
    reason: str
    safety: str
    forecast_source: str
    pv_kw: float | None
    pv_p10_kw: float | None
    pv_p50_kw: float | None
    pv_p90_kw: float | None
    pv_expected_kw: float | None  # the planner's corrected expectation for the next step
    house_kw: float | None
    battery_kw: float | None
    grid_kw: float | None
    car_kw: float | None
    wall_a: float | None
    soc: float | None
    price: float | None = None  # import price, $/kWh (0 in free hours)
    soc_pred: float | None = None  # expected path, predicted ``horizon`` hours earlier
    soc_pred_caution: float | None = None  # caution path, same
    soc_whatif: float | None = None
    grid_whatif_kw: float | None = None
    car_whatif_kw: float | None = None
    p_plan_kw: float | None = None
    p_cap_kw: float | None = None
    min_soc: float | None = None
    next_change: str = ""


@dataclass
class Change:
    t: datetime
    car: str
    from_a: int
    to_a: int
    mode: str
    reason: str


@dataclass
class Result:
    rows: list[Row]
    changes: list[Change]
    days: list[dict[str, Any]]
    notes: list[str]
    settings: dict[str, Any]
    whatif: bool
    horizon_h: float
    floor_pct: float
    sources: dict[str, int] = field(default_factory=dict)


@dataclass
class WhatIf:
    """The closed-loop world: the home battery and each car's level under the planner, and
    the last step's powers (what the planner's safety rules would have measured)."""

    soc: float
    car_soc: dict[str, float] = field(default_factory=dict)
    car_session: dict[str, datetime | None] = field(default_factory=dict)
    battery_kw: float | None = None
    grid_kw: float | None = None
    car_kw: float = 0.0


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(x, nd)


def parse_settings(pairs: Sequence[str]) -> dict[str, Any]:
    """``key=value`` pairs for planner Settings (numbers, true/false, none, text)."""
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise ValueError(f"--set needs key=value, not {pair!r}")
        low = raw.strip().lower()
        value: Any
        if low in ("true", "on", "yes"):
            value = True
        elif low in ("false", "off", "no"):
            value = False
        elif low in ("none", "null", ""):
            value = None
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
        out[key.strip()] = value
    return out


def battery_step(
    bat: Any, soc: float, pv: float, house: float, car: float, free: bool, dt_h: float
) -> tuple[float, float, float]:
    """One step of the Sol-Ark model: (new soc %, battery kW + charging, grid kW + import).
    By day, self-consumption; in free hours the Sol-Ark's own settings charge the battery
    from the grid at its full rate until it is full, and it never discharges."""
    e, eff = bat.kwh(soc), bat.efficiency
    e_min, e_max = bat.kwh(bat.reserve_pct), bat.capacity_kwh
    net = pv - house - car
    room = max(0.0, (e_max - e) / eff / dt_h)
    if free:
        b = min(bat.max_charge_kw, room)
        e += b * eff * dt_h
    elif net >= 0:
        b = min(net, bat.max_charge_kw, room)
        e += b * eff * dt_h
    else:
        b = -min(-net, bat.max_discharge_kw, max(0.0, (e - e_min) * eff / dt_h))
        e += b / eff * dt_h
    return bat.pct(e), b, b - net


def replay(
    snap: Mapping[str, Any],
    *,
    step_min: int = 5,
    start: datetime | None = None,
    end: datetime | None = None,
    settings: Mapping[str, Any] | None = None,
    horizon_h: float = 3.0,
    whatif: bool = False,
    core: Any = None,
) -> Result:
    """Run the planner over the snapshot's range (or [start, end), naive local times)."""
    core = core or load_core()
    tl = Timeline(snap, core)
    notes = list(snap.get("notes") or ())
    over = dict(snap["mapping"].get("settings") or {}) | dict(settings or {})
    valid = {f.name for f in fields(core.Settings)}
    unknown = sorted(set(over) - valid)
    if unknown:
        raise ValueError(f"unknown planner settings: {', '.join(unknown)}")
    over["shadow"] = True
    planner = core.Planner(core.Settings(**over))
    tariff = tl.tariff()
    t = floor5(start or tl.start)
    t_end = end or tl.end
    step = timedelta(minutes=step_min)
    samples = tl.samples()
    sample_times = [s.start for s in samples]
    profile_day: date | None = None
    profile = None
    rows: list[Row] = []
    changes: list[Change] = []
    planned: dict[str, int] = {cid: 0 for cid in tl.cars}
    since: dict[str, datetime | None] = {cid: None for cid in tl.cars}
    preds: dict[datetime, tuple[float, float]] = {}
    sources: dict[str, int] = {}
    world = None
    if whatif:
        soc0 = tl.number_at(tl.site["battery_soc"], t) or tl.power("battery_soc", t - FIVE)
        world = WhatIf(soc0 if soc0 is not None else 50.0)
    pv_today, u, pv_days = 0.0, t, []
    while t < t_end:
        if t.date() != profile_day:
            profile_day = t.date()
            profile = core.build_load_profile(samples, profile_day)
            pv_days = tl.forecasts.pv_days(profile_day)
            pv_today, u = 0.0, datetime.combine(profile_day, time(0))
        while u < t:  # actual solar since midnight
            pv_today += (tl.power("pv_power", u) or 0.0) * 5 / 60
            u += FIVE
        b = t - FIVE  # the last complete 5-min bin
        pv, grid = tl.power("pv_power", b), tl.power("grid_power", b)
        bat_kw, wall = tl.power("battery_power", b), tl.power("wall_power", b)
        house = tl.house(b)
        soc_meas = tl.power("battery_soc", b)
        soc = tl.number_at(tl.site["battery_soc"], t)
        soc = soc if soc is not None else soc_meas
        recent = [tl.power("pv_power", t - k * FIVE) for k in (1, 2, 3)]
        recent = [x for x in recent if x is not None]
        forecast, issued, source = tl.forecasts.at(t)
        sources[source] = sources.get(source, 0) + 1
        lo = bisect_left(sample_times, t - timedelta(hours=24))
        hi = bisect_left(sample_times, t)
        cars = []
        for cid in tl.cars:
            plugged = tl.car_plugged(cid, t)
            if plugged and since[cid] is None:
                since[cid] = t
            if not plugged:
                since[cid] = None
            car_soc = tl.number_at(tl.cars[cid].get("soc"), t)
            limit = tl.number_at(tl.cars[cid].get("limit"), t)
            if world is not None and car_soc is not None:
                if plugged and world.car_session.get(cid) != since[cid]:
                    world.car_session[cid] = since[cid]
                    world.car_soc[cid] = car_soc
                if plugged:
                    car_soc = world.car_soc.get(cid, car_soc)
            cars.append(
                core.CarState(
                    cid,
                    tl.car_spec(cid),
                    car_soc if car_soc is not None else 0.0,
                    limit if limit is not None else 80.0,
                    plugged,
                    since[cid],
                    planned[cid],
                    False,
                    car_soc is None or limit is None,
                    tl.plans(cid, t),
                )
            )
        measured = None
        if world is not None:
            soc = world.soc
            if None not in (pv, house, world.grid_kw, world.battery_kw):
                measured = core.Measured(
                    pv, world.grid_kw, world.battery_kw, world.car_kw, soc, house
                )
        elif None not in (pv, grid, bat_kw, wall) and soc is not None:
            measured = core.Measured(pv, grid, bat_kw, wall, soc)
        snapshot = core.Snapshot(
            now=t,
            soc_pct=soc if soc is not None else 50.0,
            battery=tl.battery(t),
            tariff=tariff,
            forecast=forecast,
            load=profile,
            house_recent=samples[lo:hi],
            pv_today_kwh=pv_today,
            pv_recent_kw=sum(recent) / len(recent) if recent else 0.0,
            measured=measured,
            cars=tuple(cars),
            forecast_issued_at=issued,
            pv_days=pv_days,
        )
        d = planner.decide(snapshot)
        cid = d.car_id or ""
        for c in cars:
            # the planner's current while its car is on the charger; a car that leaves the
            # charger starts its next session from nothing
            new = (d.amps if d.amps is not None else planned[cid]) if c.car_id == cid else 0
            if new != planned[c.car_id]:
                if c.car_id == cid or planned[c.car_id]:
                    changes.append(Change(t, c.car_id, planned[c.car_id], new, d.mode, d.reason))
                planned[c.car_id] = new
        slot = next((s for s in forecast if s.start <= t < s.end), None)
        target = t + timedelta(hours=horizon_h)
        point = min(d.trajectory, key=lambda p: abs(p.t - target), default=None)
        if point is not None and abs(point.t - target) <= timedelta(minutes=8):
            preds[floor5(target)] = (point.soc_expected_pct, point.soc_pct)
        first = d.trajectory[0] if d.trajectory else None
        wall_a = tl.number_at(tl.site.get("wall_current"), t)
        dbg = d.debug or {}
        row = Row(
            t=t,
            mode=d.mode,
            car=cid,
            command_a=d.amps,
            planned_a=planned.get(cid, 0) if cid else 0,
            reason=d.reason,
            safety=d.safety or "",
            forecast_source=source,
            pv_kw=_r(pv),
            pv_p10_kw=_r(slot.p10) if slot else None,
            pv_p50_kw=_r(slot.p50) if slot else None,
            pv_p90_kw=_r(slot.p90) if slot else None,
            pv_expected_kw=_r(first.pv_expected_kw) if first else None,
            house_kw=_r(house),
            battery_kw=_r(bat_kw),
            grid_kw=_r(grid),
            car_kw=_r(wall),
            wall_a=wall_a,
            soc=_r(soc if world is None else tl.number_at(tl.site["battery_soc"], t), 1),
            price=tariff.import_price(t),
            p_plan_kw=_r(dbg.get("p_plan_kw")),
            p_cap_kw=_r(dbg.get("p_cap_kw")),
            min_soc=_r(dbg.get("min_soc"), 1),
            next_change=(
                f"{d.next_change.at:%H:%M} {d.next_change.amps} A" if d.next_change else ""
            ),
        )
        if (p := preds.get(t)) is not None:
            row.soc_pred, row.soc_pred_caution = _r(p[0], 1), _r(p[1], 1)
        if world is not None:
            _advance(world, row, cars, planned, snapshot, step, pv, house)
        rows.append(row)
        t += step
    days = summarize(rows, changes, tariff, step_min, whatif)
    if sources.get("actual"):
        notes.append(
            f"{sources['actual']} steps used a hindsight forecast (the day's own actual "
            "total): no forecast history was recorded for them, so the planner looks better "
            "than it would have"
        )
    return Result(
        rows,
        changes,
        days,
        notes,
        {f.name: getattr(planner.settings, f.name) for f in fields(planner.settings)},
        whatif,
        horizon_h,
        float(planner.settings.floor_pct),
        sources,
    )


def _advance(
    world: WhatIf,
    row: Row,
    cars: Sequence[Any],
    planned: Mapping[str, int],
    snap: Any,
    step: timedelta,
    pv: float | None,
    house: float | None,
) -> None:
    """Move the what-if world one step: the planned car current, the Sol-Ark model."""
    dt_h = step.total_seconds() / 3600
    car_kw = 0.0
    for c in cars:
        amps = planned.get(c.car_id, 0)
        if c.plugged and amps > 0 and world.car_soc.get(c.car_id, c.soc_pct) < c.limit_pct:
            kw = c.spec.kw(amps, CAR_VOLTS)
            car_kw += kw
            gain = kw * dt_h * c.spec.charge_eff / c.spec.capacity_kwh * 100
            world.car_soc[c.car_id] = world.car_soc.get(c.car_id, c.soc_pct) + gain
    bat = snap.battery
    free = snap.tariff.is_free(row.t)
    soc, b, grid = battery_step(bat, world.soc, pv or 0.0, house or 0.0, car_kw, free, dt_h)
    world.soc, world.battery_kw, world.grid_kw, world.car_kw = soc, b, grid, car_kw
    row.soc_whatif = _r(soc, 1)
    row.grid_whatif_kw = _r(grid)
    row.car_whatif_kw = _r(car_kw)


def summarize(
    rows: Sequence[Row], changes: Sequence[Change], tariff: Any, step_min: int, whatif: bool
) -> list[dict[str, Any]]:
    """Per day: changes, the planner's car energy, real and what-if grid costs."""
    dt_h = step_min / 60
    out: dict[date, dict[str, Any]] = {}
    for r in rows:
        d = out.setdefault(
            r.t.date(),
            {
                "day": r.t.date().isoformat(),
                "changes": 0,
                "planned_car_kwh": 0.0,
                "actual_car_kwh": 0.0,
                "paid_import_kwh": 0.0,
                "paid_import_cost": 0.0,
                "export_kwh": 0.0,
                "min_soc": None,
                "safety_steps": 0,
                "whatif_paid_import_kwh": 0.0 if whatif else None,
                "whatif_paid_import_cost": 0.0 if whatif else None,
                "whatif_export_kwh": 0.0 if whatif else None,
                "whatif_min_soc": None,
            },
        )
        price = tariff.import_price(r.t)
        if r.car and r.planned_a and r.mode in ("day", "night"):
            d["planned_car_kwh"] += r.planned_a * CAR_VOLTS / 1000 * dt_h
        d["actual_car_kwh"] += (r.car_kw or 0.0) * dt_h
        g = r.grid_kw or 0.0
        if g > 0 and price > 0:
            d["paid_import_kwh"] += g * dt_h
            d["paid_import_cost"] += g * dt_h * price
        d["export_kwh"] += max(0.0, -g) * dt_h
        if r.soc is not None:
            d["min_soc"] = r.soc if d["min_soc"] is None else min(d["min_soc"], r.soc)
        d["safety_steps"] += bool(r.safety)
        if whatif and r.grid_whatif_kw is not None:
            gw = r.grid_whatif_kw
            if gw > 0 and price > 0:
                d["whatif_paid_import_kwh"] += gw * dt_h
                d["whatif_paid_import_cost"] += gw * dt_h * price
            d["whatif_export_kwh"] += max(0.0, -gw) * dt_h
            if r.soc_whatif is not None:
                m = d["whatif_min_soc"]
                d["whatif_min_soc"] = r.soc_whatif if m is None else min(m, r.soc_whatif)
    for c in changes:
        if c.t.date() in out:
            out[c.t.date()]["changes"] += 1
    for d in out.values():
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 2)
    return list(out.values())


# --------------------------------------------------------------------------- outputs


def write_csv(result: Result, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = [f.name for f in fields(Row)]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(names)
        for r in result.rows:
            vals = []
            for n in names:
                v = getattr(r, n)
                vals.append(v.isoformat(timespec="minutes") if isinstance(v, datetime) else v)
            w.writerow(["" if v is None else v for v in vals])


async def live_snapshot(days: int, config_path: str | None) -> dict[str, Any]:
    """Pull the last ``days`` days straight from Home Assistant (read-only)."""
    cfg = load_config(config_path)
    async with HassClient.from_env() as client:
        tz = zone((await client.config()).get("time_zone"))
        start, end = day_range(None, None, days, tz, datetime.now(UTC))
        return await pull(client, cfg, start, end)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("snapshot", nargs="?", type=Path, help="a snapshot from snapshot.py")
    ap.add_argument("--live", action="store_true", help="pull the data from HA first")
    ap.add_argument("--days", type=int, default=2, help="with --live: days up to now")
    ap.add_argument("--config", help="with --live: entity mapping")
    ap.add_argument("--from", dest="start", help="first local time, e.g. 2026-10-20T06:00")
    ap.add_argument("--to", dest="end", help="end local time")
    ap.add_argument("--step", type=int, default=5, help="minutes between decisions (5)")
    ap.add_argument("--horizon", type=float, default=3.0, help="prediction lead, hours (3)")
    ap.add_argument("--whatif", action="store_true", help="closed loop: the car obeys")
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a planner setting, e.g. caution=balanced",
    )
    ap.add_argument("--out", type=Path, help="output stem (default tools/out/replay_<range>)")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.live and args.snapshot is None:
        print("replay: give a snapshot file or --live", file=sys.stderr)
        return 2
    try:
        load_core()
    except CoreNotFound as err:
        print(f"replay: {err}", file=sys.stderr)
        return 2
    try:
        if args.live:
            snap = asyncio.run(live_snapshot(args.days, args.config))
        else:
            snap = json.loads(args.snapshot.read_text(encoding="utf-8"))
        settings = parse_settings(args.set)
        tz_start = datetime.fromisoformat(args.start) if args.start else None
        tz_end = datetime.fromisoformat(args.end) if args.end else None
        result = replay(
            snap,
            step_min=args.step,
            start=tz_start,
            end=tz_end,
            settings=settings,
            horizon_h=args.horizon,
            whatif=args.whatif,
        )
    except (OSError, ValueError, KeyError, HassError) as err:
        print(f"replay: {err}", file=sys.stderr)
        return 2

    if not result.rows:
        print("replay: nothing to replay in that range", file=sys.stderr)
        return 2
    first, last = result.rows[0].t, result.rows[-1].t
    stem = args.out or tool_config.OUT_DIR / f"replay_{first:%Y-%m-%d}_{last:%Y-%m-%d}"
    csv_path, html_path = stem.with_suffix(".csv"), stem.with_suffix(".html")
    write_csv(result, csv_path)
    html_path.write_text(render_html(result), encoding="utf-8")
    print(f"wrote {csv_path} and {html_path}")
    for d in result.days:
        line = (
            f"  {d['day']}: {d['changes']} changes, planner car {d['planned_car_kwh']:.1f} kWh "
            f"(actual {d['actual_car_kwh']:.1f}), paid import {d['paid_import_kwh']:.1f} kWh"
        )
        if result.whatif:
            line += f", what-if paid import {d['whatif_paid_import_kwh']:.1f} kWh"
        print(line)
    for note in result.notes:
        print(f"  note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
