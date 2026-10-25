"""Read-only readiness check of the live Home Assistant (plan phase 1: "read everything").

    python -m tools.check [--config tools/config.json] [--strict] [--json]

For every mapped input it checks that the entity exists, is available, has the expected unit
and a plausible value, and that its integration is alive. It also checks the Sol-Ark sign
conventions against the last 24 h of 5-min statistics, the Solcast detailedForecast and its
last poll, the free-hours Schedule helper, the Tesla Fleet entities, the Rivian adapter's
entities and control state, which car the Wall Connector sees, and, once the planner is
installed, its heartbeat and shadow mode and whether the adapter's controller options point
at the planner. It prints a table and exits 1 when anything FAILs (also on WARN with
--strict), 2 when it cannot connect.

Freshness: an unchanged value keeps an old ``last_updated`` / ``last_reported`` timestamp,
so no single value is called stale for its age. A value is stale only when it is unavailable
or unknown, or when its whole integration has gone quiet (the Sol-Ark's powers never sit
still for long). Ages are shown for information.

It never calls a service: its client is created read-only, and refuses service calls.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
import math
from pathlib import Path
import sys
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.config import Config, ConfigError, load_config  # noqa: E402
from tools.hass_client import HassClient, HassError  # noqa: E402
from tools.sanitize import tracker_state  # noqa: E402

OK, INFO, WARN, FAIL = "OK", "INFO", "WARN", "FAIL"
UNAVAILABLE = ("unavailable", "unknown", "", None)
SOL_ARK = ("pv_power", "grid_power", "battery_power", "battery_soc")
PROBE_FIELDS = ("period_start", "pv_estimate", "pv_estimate10", "pv_estimate90")


@dataclass(frozen=True)
class Row:
    status: str
    check: str
    entity: str = ""
    value: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Expect:
    """What a mapped entity should look like. ``lo``/``hi`` bound the value in ``unit``."""

    domain: str
    unit: str | None = None
    lo: float | None = None
    hi: float | None = None
    required: bool = True


def expectations(cfg: Config) -> dict[str, Expect]:
    """Per role: domain, unit and a plausible range (powers bounded in kW, then scaled)."""
    out: dict[str, Expect] = {}
    for role, lo_kw, hi_kw in (
        ("pv_power", -0.1, 40.0),
        ("grid_power", -40.0, 40.0),
        ("battery_power", -20.0, 20.0),
        ("wall_power", -0.1, 15.0),
    ):
        k = cfg.kw_scale(role)
        out[role] = Expect("sensor", cfg.unit(role) or "W", lo_kw / k, hi_kw / k)
    out |= {
        "battery_soc": Expect("sensor", "%", 0, 100),
        "battery_capacity": Expect("sensor", "kWh", 5, 200),
        "wall_current": Expect("sensor", "A", 0, 80),
        "wall_connected": Expect("binary_sensor"),
        "wall_contactor": Expect("binary_sensor"),
        "forecast_today": Expect("sensor", "kWh", 0, 500),
        "forecast_tomorrow": Expect("sensor", "kWh", 0, 500),
        "forecast_remaining_today": Expect("sensor", "kWh", 0, 500, required=False),
        "forecast_last_polled": Expect("sensor"),
        "free_hours": Expect("schedule", required=False),
    }
    return out


CAR_EXPECT = {
    "soc": Expect("sensor", "%", 0, 100),
    "limit": Expect("", "%", 40, 100),  # number (Tesla) or sensor (Rivian telemetry)
    "plugged": Expect("binary_sensor"),
    "location": Expect("device_tracker"),
    "switch": Expect("switch"),
    "current": Expect("number", "A", 0, 80),
    "control_state": Expect("sensor"),
    "charger_status": Expect("sensor"),
    "charger_state": Expect("sensor", required=False),  # Rivian: for the adapter's option
    "lease_renewal": Expect("switch", required=False),
    "restore_button": Expect("button", required=False),
    "read_schedules_button": Expect("button", required=False),
}
PLATFORMS = {"tesla": "tesla_fleet", "rivian_adapter": "rivian_solar_adapter"}
ADAPTER_KEYS = ("switch", "current", "control_state", "charger_status")


def parse_time(v: Any) -> datetime | None:
    if not isinstance(v, str) or v in UNAVAILABLE:
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def number(state: Mapping[str, Any] | None) -> float | None:
    if not state or state.get("state") in UNAVAILABLE:
        return None
    try:
        x = float(state["state"])
    except TypeError, ValueError:
        return None
    return x if math.isfinite(x) else None


def activity(state: Mapping[str, Any] | None) -> datetime | None:
    """When the integration last wrote this entity (changed, updated or reported)."""
    if not state:
        return None
    times = [parse_time(state.get(k)) for k in ("last_changed", "last_updated", "last_reported")]
    return max((t for t in times if t), default=None)


def age_text(t: datetime | None, now: datetime) -> str:
    if t is None:
        return "?"
    s = max(0.0, (now - t).total_seconds())
    if s < 120:
        return f"{s:.0f} s"
    if s < 7200:
        return f"{s / 60:.0f} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} d"


def value_text(state: Mapping[str, Any] | None) -> str:
    """The state and unit; a device tracker's zone shows only as home / not_home."""
    if not state:
        return "missing"
    value = state.get("state")
    if str(state.get("entity_id", "")).startswith("device_tracker."):
        value = tracker_state(value)
    unit = (state.get("attributes") or {}).get("unit_of_measurement")
    return f"{value}{' ' + unit if unit else ''}"


def check_entity(
    label: str, eid: str | None, st: Mapping[str, Any] | None, exp: Expect, now: datetime
) -> Row:
    """Exists, available, domain, unit and range of one mapped entity."""
    if not eid:
        return Row(WARN if exp.required else INFO, label, "", "", "not mapped in the config")
    bad = FAIL if exp.required else WARN
    if st is None:
        return Row(bad, label, eid, "missing", "no such entity in Home Assistant")
    domain = eid.partition(".")[0]
    if exp.domain and domain != exp.domain:
        return Row(bad, label, eid, value_text(st), f"expected a {exp.domain} entity")
    unusable = ("unavailable",) if domain == "button" else UNAVAILABLE  # unknown: never pressed
    if st.get("state") in unusable:
        return Row(bad, label, eid, str(st.get("state")), "unavailable: its integration is down")
    attrs = st.get("attributes") or {}
    unit = attrs.get("unit_of_measurement")
    if exp.unit is not None and unit != exp.unit:
        return Row(
            FAIL,
            label,
            eid,
            value_text(st),
            f"unit {unit!r}, expected {exp.unit!r} (set units.{label} in the config if right)",
        )
    detail = f"changed {age_text(parse_time(st.get('last_changed')), now)} ago"
    if exp.lo is not None or exp.hi is not None:
        x = number(st)
        if x is None:
            return Row(bad, label, eid, value_text(st), "not a number")
        if (exp.lo is not None and x < exp.lo) or (exp.hi is not None and x > exp.hi):
            return Row(
                WARN, label, eid, value_text(st), f"outside the plausible {exp.lo}..{exp.hi}"
            )
    return Row(OK, label, eid, value_text(st), detail)


# --------------------------------------------------------------------------- the checks


def site_rows(cfg: Config, states: Mapping[str, Any], now: datetime) -> list[Row]:
    rows = []
    for role, exp in expectations(cfg).items():
        eid = cfg.site.get(role)
        rows.append(check_entity(role, eid, states.get(eid) if eid else None, exp, now))
    quiet = float(cfg.section("freshness").get("sol_ark_quiet_min", 15))
    last = [activity(states.get(cfg.site.get(r) or "")) for r in SOL_ARK]
    newest = max((t for t in last if t), default=None)
    if newest is None:
        rows.append(Row(FAIL, "Sol-Ark alive", "", "", "no Sol-Ark entity has a timestamp"))
    else:
        mins = (now - newest).total_seconds() / 60
        status = OK if mins <= quiet else WARN if mins <= 4 * quiet else FAIL
        rows.append(
            Row(
                status,
                "Sol-Ark alive",
                "",
                f"newest change {age_text(newest, now)} ago",
                "the powers move all the time; quiet for long means the integration stopped"
                if status != OK
                else "the integration is writing values",
            )
        )
    house = house_now(cfg, states)
    if house is not None:
        rows.append(
            Row(
                OK if house >= -0.3 else WARN,
                "house load now",
                "",
                f"{house:.2f} kW",
                "pv + grid - battery - wall connector"
                + ("" if house >= -0.3 else ": negative, check the sign conventions"),
            )
        )
    return rows


def house_now(cfg: Config, states: Mapping[str, Any]) -> float | None:
    vals = {}
    for role in ("pv_power", "grid_power", "battery_power", "wall_power"):
        x = number(states.get(cfg.site.get(role) or ""))
        if x is None:
            return None
        vals[role] = x * cfg.kw_scale(role)
    return vals["pv_power"] + vals["grid_power"] - vals["battery_power"] - vals["wall_power"]


def _means(rows: Sequence[Mapping[str, Any]], scale: float) -> dict[float, float]:
    out = {}
    for r in rows:
        m, start = r.get("mean"), r.get("start")
        if m is None or start is None:
            continue
        if isinstance(start, str):
            t = parse_time(start)
            if t is None:
                continue
            start = t.timestamp() * 1e3
        out[float(start)] = float(m) * scale
    return out


def sign_rows(cfg: Config, stats: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[Row]:
    """Which sign convention keeps the computed house load non-negative, and whether the
    battery's power agrees with its level changing."""
    site = cfg.site
    series = {
        role: _means(stats.get(site[role], ()), cfg.kw_scale(role))
        for role in ("pv_power", "grid_power", "battery_power", "wall_power")
    }
    missing = [site[r] for r, s in series.items() if not s]
    if missing:
        return [
            Row(
                WARN,
                "signs (24 h)",
                ", ".join(missing),
                "no statistics",
                "no 5-min statistics: the sensor needs state_class measurement "
                "(also needed for the load profile)",
            )
        ]
    keys = sorted(set.intersection(*(set(s) for s in series.values())))
    if len(keys) < 24:
        return [Row(WARN, "signs (24 h)", "", f"{len(keys)} periods", "too little data to tell")]
    pv, grid, bat, wall = (series[r] for r in series)

    def negative_share(g: int, b: int) -> float:
        neg = sum(1 for k in keys if pv[k] + g * grid[k] - b * bat[k] - wall[k] < -0.2)
        return neg / len(keys)

    shares = {(g, b): negative_share(g, b) for g in (1, -1) for b in (1, -1)}
    expected = shares[(1, 1)]
    best = min(shares, key=shares.get)
    rows = []
    if expected <= 0.05:
        rows.append(
            Row(
                OK,
                "signs (24 h)",
                "",
                f"{len(keys)} periods",
                f"grid + import, battery + charging: house >= 0 in {100 * (1 - expected):.0f}%",
            )
        )
    elif best != (1, 1) and shares[best] <= 0.05:
        flipped = [n for n, s in zip(("grid", "battery"), best, strict=True) if s < 0]
        rows.append(
            Row(
                FAIL,
                "signs (24 h)",
                "",
                f"{len(keys)} periods",
                f"the {' and '.join(flipped)} sign looks reversed: with it flipped the house "
                f"is never negative, as configured it is negative {100 * expected:.0f}% of "
                "the time",
            )
        )
    else:
        rows.append(
            Row(
                WARN,
                "signs (24 h)",
                "",
                f"{len(keys)} periods",
                f"house load computed negative {100 * expected:.0f}% of the time; "
                "check the units and signs",
            )
        )
    soc = _means(stats.get(site["battery_soc"], ()), 1.0)
    rows.append(battery_direction(bat, soc))
    return rows


def battery_direction(bat: Mapping[float, float], soc: Mapping[float, float]) -> Row:
    """Correlate hourly battery energy with the level's change: + charging means both rise."""
    hour = 3600e3
    buckets: dict[int, list[float]] = {}
    for k, v in bat.items():
        buckets.setdefault(int(k // hour), []).append(v)
    xs, ys = [], []
    for h, vals in buckets.items():
        a, b = soc.get(h * hour), soc.get((h + 1) * hour)
        if a is None or b is None:
            # statistics rows start on whole 5 minutes; take the first and last of the hour
            inside = sorted(k for k in soc if h * hour <= k < (h + 1) * hour)
            if len(inside) < 2:
                continue
            a, b = soc[inside[0]], soc[inside[-1]]
        xs.append(sum(vals) / len(vals))
        ys.append(b - a)
    if len(xs) < 4 or max(map(abs, ys), default=0.0) < 1.0:
        return Row(INFO, "battery direction", "", "", "the battery hardly moved; cannot tell")
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    r = sxy / (sx * sy) if sx > 0 and sy > 0 else 0.0
    if r >= 0.3:
        return Row(OK, "battery direction", "", f"r = {r:.2f}", "positive power raises the level")
    if r <= -0.3:
        return Row(
            FAIL,
            "battery direction",
            "",
            f"r = {r:.2f}",
            "positive battery power LOWERS the level: the sign is reversed",
        )
    return Row(WARN, "battery direction", "", f"r = {r:.2f}", "no clear relation; check")


def forecast_rows(cfg: Config, states: Mapping[str, Any], now: datetime, tz: Any) -> list[Row]:
    rows = []
    today = now.astimezone(tz).date()
    for role, day in (
        ("forecast_today", today),
        ("forecast_tomorrow", today + timedelta(days=1)),
    ):
        eid = cfg.site.get(role)
        st = states.get(eid or "")
        if not st:
            continue
        detailed = (st.get("attributes") or {}).get("detailedForecast")
        if not isinstance(detailed, list) or not detailed:
            rows.append(
                Row(
                    FAIL,
                    f"{role} detail",
                    eid or "",
                    "no detailedForecast",
                    "turn on the half-hourly detail attribute in the Solcast options",
                )
            )
            continue
        first = detailed[0] if isinstance(detailed[0], Mapping) else {}
        absent = [k for k in PROBE_FIELDS if k not in first]
        starts = [parse_time(str(r.get("period_start"))) for r in detailed if isinstance(r, dict)]
        days = {t.astimezone(tz).date() for t in starts if t}
        status, detail = OK, "period_start, pv_estimate, pv_estimate10, pv_estimate90"
        if "period_start" in absent or "pv_estimate" in absent:
            status, detail = FAIL, f"slots lack {', '.join(absent)}"
        elif absent:
            status = WARN
            detail = f"slots lack {', '.join(absent)}: turn on estimate10/90 in the Solcast options"
        elif days != {day}:
            status, detail = WARN, f"covers {sorted(days)}, expected {day}"
        rows.append(Row(status, f"{role} detail", eid or "", f"{len(detailed)} slots", detail))
    eid = cfg.site.get("forecast_last_polled")
    polled = parse_time((states.get(eid or "") or {}).get("state"))
    fc = cfg.section("freshness")
    warn_h, fail_h = (
        float(fc.get("forecast_poll_warn_h", 6)),
        float(fc.get("forecast_poll_fail_h", 24)),
    )
    if polled is None:
        rows.append(Row(FAIL, "Solcast last poll", eid or "", "unknown", "no poll time"))
    else:
        h = (now - polled).total_seconds() / 3600
        status = OK if h <= warn_h else WARN if h <= fail_h else FAIL
        rows.append(
            Row(
                status,
                "Solcast last poll",
                eid or "",
                f"{age_text(polled, now)} ago",
                "polls run in daylight; the planner widens its spread after 3 h"
                if status == OK
                else "polls are failing or the API quota ran out",
            )
        )
    rem = states.get(cfg.site.get("forecast_remaining_today") or "")
    if rem:
        attrs = rem.get("attributes") or {}
        have = [k for k in ("estimate10", "estimate", "estimate90") if k in attrs]
        rows.append(
            Row(
                OK if len(have) == 3 else INFO,
                "remaining today p10/p50/p90",
                rem["entity_id"],
                " / ".join(str(attrs.get(k)) for k in have) or str(rem.get("state")),
                "kWh",
            )
        )
    rows.append(
        Row(
            INFO,
            "forecast history",
            "",
            "",
            "HA does not record detailedForecast (Solcast marks it unrecorded); "
            "run 'snapshot record-forecasts' to keep issues for replays",
        )
    )
    return rows


def _blocks(weekly: Mapping[str, Any]) -> str:
    """'Mon-Fri 00:00-06:00 20:00-24:00; Sat-Sun ...': runs of days with the same blocks."""
    names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    per_day = [
        " ".join(
            f"{str(b.get('from'))[:5]}-{str(b.get('to'))[:5].replace('23:59', '24:00')}"
            for b in weekly.get(name) or ()
        )
        or "none"
        for name in names
    ]
    parts, i = [], 0
    while i < 7:
        j = i
        while j + 1 < 7 and per_day[j + 1] == per_day[i]:
            j += 1
        days = names[i][:3].title() + (f"-{names[j][:3].title()}" if j > i else "")
        parts.append(f"{days} {per_day[i]}")
        i = j + 1
    return "; ".join(parts)


async def schedule_rows(client: HassClient, cfg: Config) -> list[Row]:
    eid = cfg.site.get("free_hours")
    if not eid:
        return []
    try:
        weekly = await client.schedule_config(eid)
    except HassError as err:
        return [Row(WARN, "free hours weekly", eid, "", f"cannot read schedule/list: {err}")]
    if weekly is None:
        return [
            Row(
                WARN,
                "free hours weekly",
                eid,
                "not found",
                "no UI Schedule helper matches; replays use tariff.free_hours_fallback",
            )
        ]
    return [Row(OK, "free hours weekly", eid, "", _blocks(weekly))]


async def car_rows(
    client: HassClient, cfg: Config, states: Mapping[str, Any], now: datetime
) -> list[Row]:
    rows = []
    for car_id, car in cfg.cars.items():
        kind = car.get("kind")
        for key, exp in CAR_EXPECT.items():
            eid = car.get(key)
            if not eid:
                if kind == "rivian" and key in ADAPTER_KEYS:
                    rows.append(Row(FAIL, f"{car_id}.{key}", "", "", "not mapped"))
                continue
            if kind == "tesla" and key in ("control_state", "charger_status"):
                continue
            rows.append(check_entity(f"{car_id}.{key}", eid, states.get(eid), exp, now))
        if kind == "tesla":
            rows += await tesla_rows(client, car_id, car, states)
        elif kind == "rivian":
            rows += await adapter_rows(client, car_id, car, states)
            rows += controller_rows(cfg, car_id, car, states)
    return rows


async def _platform(client: HassClient, eid: str) -> str | None:
    try:
        entry = await client.registry_entry(eid)
    except HassError:
        return None
    return (entry or {}).get("platform")


async def tesla_rows(
    client: HassClient, car_id: str, car: Mapping[str, str], states: Mapping[str, Any]
) -> list[Row]:
    rows = []
    fleet = [car[k] for k in ("switch", "current", "soc", "plugged") if car.get(k)]
    legacy = [car[k] for k in ("legacy_switch", "legacy_current") if car.get(k)]
    found = [e for e in fleet if states.get(e)]
    if len(found) < len(fleet):
        old = [e for e in legacy if states.get(e)]
        rows.append(
            Row(
                FAIL,
                f"{car_id} on Tesla Fleet",
                ", ".join(e for e in fleet if e not in found),
                "missing",
                f"tesla_custom entities found ({', '.join(old)}): add HA core Tesla Fleet"
                if old
                else "add HA core Tesla Fleet (plan phase 1)",
            )
        )
        return rows
    platforms = {e: await _platform(client, e) for e in fleet}
    other = {e: p for e, p in platforms.items() if p and p != PLATFORMS["tesla"]}
    if other:
        rows.append(
            Row(
                WARN,
                f"{car_id} on Tesla Fleet",
                ", ".join(other),
                ", ".join(sorted(set(other.values()))),
                "these come from another integration, not tesla_fleet",
            )
        )
    else:
        known = any(platforms.values())
        rows.append(
            Row(
                OK if known else INFO,
                f"{car_id} on Tesla Fleet",
                "",
                "tesla_fleet" if known else "?",
                "every command is billed by Tesla's API"
                if known
                else "the entity registry could not say which integration",
            )
        )
    cur = states.get(car.get("current") or "")
    if cur:
        a = cur.get("attributes") or {}
        rows.append(
            Row(
                INFO,
                f"{car_id} current range",
                cur["entity_id"],
                f"{a.get('min')}..{a.get('max')} A",
                "the car's own range",
            )
        )
    return rows


async def adapter_rows(
    client: HassClient, car_id: str, car: Mapping[str, str], states: Mapping[str, Any]
) -> list[Row]:
    rows = []
    cs = states.get(car.get("control_state") or "")
    if not cs or cs.get("state") in UNAVAILABLE:
        return rows
    a = cs.get("attributes") or {}
    state = cs.get("state")
    platform = await _platform(client, cs["entity_id"])
    if platform and platform != PLATFORMS["rivian_adapter"]:
        rows.append(Row(WARN, f"{car_id} adapter", cs["entity_id"], platform, "not the adapter"))
    status, detail = OK, "the adapter is running"
    if state == "fault":
        status = FAIL
        detail = f"fault {a.get('fault_code')}: {a.get('fault_message')}; see the adapter guide"
    elif state == "handed_back":
        status, detail = WARN, "handed back to the owner until unplug; requests are ignored"
    elif a.get("dry_run"):
        status, detail = INFO, "dry run is on: nothing is written (expected until the trial)"
    rows.append(Row(status, f"{car_id} control state", cs["entity_id"], str(state), detail))
    req = (
        f"desired_charge={a.get('desired_charge')} requested_amps={a.get('requested_amps')} "
        f"lease_amps={a.get('lease_amps')} last_write_at={a.get('last_write_at')}"
    )
    rows.append(Row(INFO, f"{car_id} request", "", "", req))
    blocked = a.get("start_blocked_by")
    flags = [k for k in ("request_held", "request_unconfirmed") if a.get(k)]
    if blocked or flags:
        rows.append(
            Row(
                INFO,
                f"{car_id} start blocked",
                "",
                str(blocked or ""),
                ", ".join(flags) or "why charging cannot start now",
            )
        )
    if a.get("dry_run") and a.get("dry_run_target"):
        t = a["dry_run_target"]
        rows.append(Row(INFO, f"{car_id} dry-run target", "", "", json.dumps(t, default=str)))
    return rows


def _known(state: Mapping[str, Any] | None) -> str | None:
    """A state's value, or None when it is missing, unknown or unavailable."""
    value = (state or {}).get("state")
    return None if value in UNAVAILABLE else str(value)


def controller_rows(
    cfg: Config, car_id: str, car: Mapping[str, str], states: Mapping[str, Any]
) -> list[Row]:
    """Whether the adapter's controller options point at the planner, as the adapter itself
    reads them (its control state's ``observation``): the liveness option at the heartbeat,
    the charging option at this car's charging-wanted sensor."""
    cs = states.get(car.get("control_state") or "")
    if not cs or cs.get("state") in UNAVAILABLE:
        return []
    obs = (cs.get("attributes") or {}).get("observation") or {}
    hb_id = cfg.planner.get("heartbeat") or ""
    heartbeat = states.get(hb_id)
    if heartbeat is None:
        return [
            Row(
                INFO,
                f"{car_id} controller liveness",
                "",
                "planner not installed",
                "map the adapter's controller options once the planner is installed",
            )
        ]
    rows = []
    alive = obs.get("controller_alive")
    beat = _known(heartbeat)
    if alive is None:
        rows.append(
            Row(
                WARN,
                f"{car_id} controller liveness",
                hb_id,
                "not mapped",
                f"set the adapter's controller_liveness_entity to {hb_id}",
            )
        )
    elif alive and beat is None:
        rows.append(
            Row(
                WARN,
                f"{car_id} controller liveness",
                hb_id,
                "mapped elsewhere",
                f"the adapter sees its controller up while {hb_id} is down",
            )
        )
    elif alive:
        rows.append(Row(OK, f"{car_id} controller liveness", hb_id, beat or "", "planner up"))
    elif beat is not None:
        rows.append(
            Row(
                WARN,
                f"{car_id} controller liveness",
                hb_id,
                "mapped elsewhere",
                f"the adapter sees its controller down while {hb_id} is {beat}",
            )
        )
    else:
        rows.append(
            Row(
                WARN,
                f"{car_id} controller liveness",
                hb_id,
                "down",
                "the planner is off or cannot plan (see its reason sensor): the adapter "
                "starts and renews nothing, and parks the window after 3 minutes",
            )
        )
    wanted_id = car.get("charging_wanted") or ""
    wanted = _known(states.get(wanted_id))
    seen = obs.get("controller_charging")
    if wanted is None:
        rows.append(
            Row(
                INFO,
                f"{car_id} controller charging",
                wanted_id,
                "unknown",
                "no decision to compare (shadow mode, or no command since the plug-in)",
            )
        )
    elif seen is None:
        rows.append(
            Row(
                WARN,
                f"{car_id} controller charging",
                wanted_id,
                "not mapped",
                f"set the adapter's controller_charging_entity to {wanted_id}",
            )
        )
    elif seen != (wanted == "on"):
        rows.append(
            Row(
                WARN,
                f"{car_id} controller charging",
                wanted_id,
                "mapped elsewhere",
                f"the adapter reads {'on' if seen else 'off'} while {wanted_id} is {wanted}",
            )
        )
    else:
        rows.append(Row(OK, f"{car_id} controller charging", wanted_id, wanted, "in step"))
    return rows


def planner_rows(cfg: Config, states: Mapping[str, Any]) -> list[Row]:
    """The planner integration: running, in shadow mode, what it decided."""
    p = cfg.planner
    hb_id = p.get("heartbeat") or ""
    heartbeat = states.get(hb_id)
    if heartbeat is None:
        return [
            Row(
                INFO,
                "planner",
                hb_id,
                "not installed",
                "install it for plan phase 3 (docs/PLANNER.md)",
            )
        ]
    rows = []
    beat = _known(heartbeat)
    if beat == "shadow":
        rows.append(Row(OK, "planner heartbeat", hb_id, beat, "planning; nothing is sent"))
    elif beat == "active":
        rows.append(Row(INFO, "planner heartbeat", hb_id, beat, "commanding the cars"))
    else:
        rows.append(
            Row(
                WARN,
                "planner heartbeat",
                hb_id,
                str(heartbeat.get("state")),
                "Planning is off, or the planner cannot plan: see its reason",
            )
        )
    planning = _known(states.get(p.get("planning") or ""))
    if planning == "off":
        rows.append(
            Row(WARN, "planner planning", p.get("planning", ""), "off", "nothing is planned")
        )
    shadow = _known(states.get(p.get("shadow_mode") or ""))
    rows.append(
        Row(
            OK if shadow == "on" else INFO,
            "planner shadow mode",
            p.get("shadow_mode", ""),
            shadow or "?",
            "nothing is sent" if shadow == "on" else "the planner commands the car on the charger",
        )
    )
    for key in ("mode", "car_on_charger", "reason"):
        if eid := p.get(key):
            state = states.get(eid)
            full = ((state or {}).get("attributes") or {}).get("full_reason")
            value = str(full or (state or {}).get("state", "missing"))
            rows.append(Row(INFO, f"planner {key}", eid, "", value))
    return rows


def wall_rows(cfg: Config, states: Mapping[str, Any]) -> list[Row]:
    """Which car the Wall Connector sees: it sees a vehicle, and exactly one car's own cable
    or connection sensor is on while that car is home (as the planner decides it)."""
    wc = states.get(cfg.site.get("wall_connected") or "")
    if not wc or wc.get("state") in UNAVAILABLE:
        return [Row(FAIL, "car on the charger", "", "unknown", "Wall Connector state unavailable")]
    connected = wc.get("state") == "on"
    at_home = []
    plugged_away = []
    for car_id, car in cfg.cars.items():
        plugged = (states.get(car.get("plugged") or "") or {}).get("state")
        where = (states.get(car.get("location") or "") or {}).get("state")
        if plugged == "on" and where == "home":
            at_home.append(car_id)
        elif plugged == "on":
            plugged_away.append(car_id)
    amps = number(states.get(cfg.site.get("wall_current") or ""))
    contactor = (states.get(cfg.site.get("wall_contactor") or "") or {}).get("state")
    now_draw = f"{amps if amps is not None else '?'} A, contactor {contactor}"
    rows = []
    if not connected:
        status = INFO if not at_home else WARN
        detail = "no vehicle connected" + (
            f"; but {', '.join(at_home)} reports plugged in at home (another outlet?)"
            if at_home
            else ""
        )
        rows.append(Row(status, "car on the charger", "", "none", detail))
    elif len(at_home) == 1:
        rows.append(Row(OK, "car on the charger", "", at_home[0], now_draw))
    elif not at_home:
        rows.append(
            Row(
                WARN,
                "car on the charger",
                "",
                "unknown",
                "a vehicle is connected but no car reports being plugged in at home",
            )
        )
    else:
        rows.append(
            Row(
                WARN,
                "car on the charger",
                "",
                "ambiguous",
                f"{' and '.join(at_home)} both report plugged in at home; the planner "
                "commands neither until its charger car override names one",
            )
        )
    for car_id in plugged_away:
        rows.append(Row(INFO, f"{car_id} plugged away", "", "", "plugged in but not home"))
    for car_id, car in cfg.cars.items():
        cs = states.get(car.get("charger_status") or "")
        if cs and connected and car_id in at_home and cs.get("state") == "a":
            rows.append(
                Row(
                    WARN,
                    f"{car_id} charger status",
                    cs["entity_id"],
                    "a",
                    "the adapter does not see the car as plugged in at home",
                )
            )
    return rows


async def run_checks(client: HassClient, cfg: Config, now: datetime | None = None) -> list[Row]:
    """Every check, in table order. Raises HassError when HA cannot be reached at all."""
    now = now or datetime.now(UTC)
    ha = await client.config()
    rows = [
        Row(
            OK,
            "Home Assistant",
            client.url,
            str(ha.get("version", "?")),
            f"time zone {ha.get('time_zone', '?')}",
        )
    ]
    tz = _zone(ha.get("time_zone"))
    states = await client.states_of(cfg.entity_ids())
    rows += site_rows(cfg, states, now)
    site = cfg.site
    ids = [
        site[r] for r in ("pv_power", "grid_power", "battery_power", "wall_power", "battery_soc")
    ]
    try:
        stats = await client.statistics_during_period(
            ids, now - timedelta(hours=24), now, period="5minute", types=("mean",)
        )
    except HassError as err:
        rows.append(Row(WARN, "signs (24 h)", "", "", f"cannot read statistics: {err}"))
    else:
        rows += sign_rows(cfg, stats)
    rows += forecast_rows(cfg, states, now, tz)
    rows += await schedule_rows(client, cfg)
    rows += await car_rows(client, cfg, states, now)
    rows += wall_rows(cfg, states)
    rows += planner_rows(cfg, states)
    return rows


def _zone(name: Any) -> Any:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(str(name))
    except ZoneInfoNotFoundError, ValueError:
        return UTC


def render(rows: Sequence[Row]) -> str:
    head = ("", "check", "entity", "value", "detail")
    table = [head] + [(r.status, r.check, r.entity, r.value, r.detail) for r in rows]
    widths = [
        min(max(len(str(row[i])) for row in table), cap) for i, cap in enumerate((4, 28, 52, 28, 0))
    ]
    lines = []
    for row in table:
        cells = [str(c) for c in row]
        fixed = [
            (c if len(c) <= w else c[: w - 1] + "~").ljust(w)
            for c, w in zip(cells[:4], widths[:4], strict=True)
        ]
        lines.append("  ".join(fixed + [cells[4]]).rstrip())
    counts = {s: sum(r.status == s for r in rows) for s in (FAIL, WARN, INFO, OK)}
    lines.append("")
    lines.append(", ".join(f"{n} {s}" for s, n in counts.items()))
    return "\n".join(lines)


def exit_code(rows: Sequence[Row], strict: bool = False) -> int:
    bad = {FAIL, WARN} if strict else {FAIL}
    return 1 if any(r.status in bad for r in rows) else 0


async def amain(argv: Sequence[str] | None = None, *, client: HassClient | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", help="entity mapping (default: tools/config.json or the example)")
    ap.add_argument("--strict", action="store_true", help="exit 1 on WARN too")
    ap.add_argument("--json", action="store_true", help="print the rows as JSON")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        own = client is None
        client = client or HassClient.from_env()
    except (ConfigError, HassError) as err:
        print(f"check: {err}", file=sys.stderr)
        return 2
    try:
        rows = await run_checks(client, cfg)
    except HassError as err:
        print(f"check: {err}", file=sys.stderr)
        return 2
    finally:
        if own:
            await client.close()
    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=1))
    else:
        print(render(rows))
    return exit_code(rows, args.strict)


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
