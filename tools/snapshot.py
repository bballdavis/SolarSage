"""Pull real Home Assistant data into a sanitized JSON snapshot, usable offline and in tests.

    python -m tools.snapshot pull --days 3                     # the last 3 days up to now
    python -m tools.snapshot pull --start 2026-10-18 --end 2026-10-24 [--out FILE]
    python -m tools.snapshot record-forecasts [--every 10]    # keep running; Ctrl-C stops

``pull`` writes (default ``tools/out/snapshot_<start>_<end>.json``, git-ignored):

- the current state of every mapped entity, and its state history over the range;
- recorder statistics of the four powers and the battery level: 5-min rows where the
  recorder still has them (about 10 days) and hourly rows, from ``--load-days`` (14) before
  the range, so a replay can build the house-load profile for its first day;
- the current Solcast forecast (detailedForecast for today and tomorrow);
- the free-hours Schedule helper's weekly blocks;
- the forecast issues recorded by ``record-forecasts`` that fall in the range.

**Solcast's half hours are not in HA's history.** The Solcast integration (BJReplay
ha-solcast-solar) lists ``detailedForecast``, ``detailedHourly`` and ``analysis`` of its
forecast-day sensors in ``_unrecorded_attributes``, so the recorder keeps only each day's
totals (the state plus the ``estimate`` / ``estimate10`` / ``estimate90`` attributes) and the
remaining-today values. ``record-forecasts`` fills the gap from now on: it watches
``api_last_polled`` and appends every new issue (today's and tomorrow's half hours) to
``tools/out/forecast_issues.jsonl``. For times no recorded issue covers, replay.py builds a
documented approximation from the recorded daily totals (see replay.py).

Sanitized: no coordinates, zone names, VINs, tokens, emails, user ids or HA location; the
connection URL is not stored. The tool only reads; it never calls a service.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import json
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import config as tool_config  # noqa: E402
from tools.config import Config, ConfigError, load_config  # noqa: E402
from tools.hass_client import HassClient, HassError  # noqa: E402
from tools.sanitize import scrub, scrub_state, tracker_state  # noqa: E402

FORMAT = "hass-solar-rivian-snapshot"
VERSION = 1
FORECAST_LOG = "forecast_issues.jsonl"
STAT_ROLES = ("pv_power", "grid_power", "battery_power", "wall_power", "battery_soc")
FORECAST_ROLES = ("forecast_today", "forecast_tomorrow", "forecast_remaining_today")
FORECAST_ATTRS = (
    "unit_of_measurement",
    "estimate",
    "estimate10",
    "estimate90",
    "detailedForecast",
    "dataCorrect",
    "dayname",
)
SLOT_KEYS = ("period_start", "pv_estimate10", "pv_estimate", "pv_estimate90")
HA_CONFIG_KEEP = ("version", "time_zone", "unit_system", "currency")


def zone(name: Any) -> tzinfo:
    try:
        return ZoneInfo(str(name))
    except ZoneInfoNotFoundError, ValueError:
        return UTC


def local_iso(v: Any, tz: tzinfo) -> str | None:
    """An HA timestamp (ISO text or epoch seconds / ms) as local ISO text with its offset."""
    if v is None:
        return None
    if isinstance(v, int | float):
        x = float(v)
        t = datetime.fromtimestamp(x / 1000.0 if x > 1e11 else x, UTC)
    else:
        t = datetime.fromisoformat(str(v))
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
    return t.astimezone(tz).isoformat()


def attribute_entities(cfg: Config) -> set[str]:
    """Entities whose history needs attributes: Solcast totals, the adapter's state."""
    ids = {cfg.site[r] for r in FORECAST_ROLES if cfg.site.get(r)}
    ids |= {c["control_state"] for c in cfg.cars.values() if c.get("control_state")}
    return ids


def keep_attributes(eid: str, attrs: Mapping[str, Any], cfg: Config) -> dict[str, Any]:
    """Solcast sensors keep their forecast fields (not per-site breakdowns, which name the
    Solcast site ids); everything else keeps its scrubbed attributes."""
    if eid in {cfg.site.get(r) for r in FORECAST_ROLES}:
        out = {k: attrs[k] for k in FORECAST_ATTRS if k in attrs}
        if isinstance(out.get("detailedForecast"), list):
            out["detailedForecast"] = [
                {k: r.get(k) for k in SLOT_KEYS if k in r}
                for r in out["detailedForecast"]
                if isinstance(r, Mapping)
            ]
        return out
    return scrub(dict(attrs))


def clean_state(st: Mapping[str, Any], cfg: Config, tz: tzinfo) -> dict[str, Any]:
    s = scrub_state(st)
    return {
        "state": s.get("state"),
        "attributes": keep_attributes(str(st.get("entity_id")), s.get("attributes") or {}, cfg),
        "last_changed": local_iso(st.get("last_changed"), tz),
        "last_updated": local_iso(st.get("last_updated"), tz),
    }


def clean_history(
    rows: Sequence[Mapping[str, Any]], eid: str, cfg: Config, tz: tzinfo, with_attrs: bool
) -> list[list[Any]]:
    """[[local time, state], ...] or [[time, state, attributes], ...] in time order."""
    tracker = eid.startswith("device_tracker.")
    out = []
    for r in rows:
        # last_updated also moves for a change of attributes alone
        t = local_iso(r.get("last_updated") or r.get("last_changed"), tz)
        state = tracker_state(r.get("state")) if tracker else r.get("state")
        if with_attrs:
            out.append([t, state, keep_attributes(eid, scrub(r.get("attributes") or {}), cfg)])
        else:
            out.append([t, state])
    out.sort(key=lambda x: x[0] or "")
    return out


def _extend(series: list[list[Any]], rows: list[list[Any]]) -> None:
    """Append one request's rows. Each request starts with the state at its start, which
    repeats the previous request's last state: that row is dropped."""
    if series and rows and rows[0][1:] == series[-1][1:]:
        rows = rows[1:]
    series.extend(rows)


def clean_statistics(
    data: Mapping[str, Sequence[Mapping[str, Any]]], tz: tzinfo
) -> dict[str, list[dict[str, Any]]]:
    return {
        sid: [
            {"start": local_iso(r["start"], tz), "mean": r.get("mean")}
            for r in rows
            if r.get("start") is not None and r.get("mean") is not None
        ]
        for sid, rows in data.items()
    }


def slots_from_attributes(*attrs: Mapping[str, Any] | None) -> list[list[Any]]:
    """[[period_start, p10, p50, p90], ...] from Solcast day attributes (missing p10/p90
    fall back to p50, as the planner core does)."""
    out: dict[str, list[Any]] = {}
    for a in attrs:
        for r in (a or {}).get("detailedForecast") or ():
            if not isinstance(r, Mapping) or "period_start" not in r:
                continue
            p50 = r.get("pv_estimate") or 0.0
            out[str(r["period_start"])] = [
                str(r["period_start"]),
                r.get("pv_estimate10", p50),
                p50,
                r.get("pv_estimate90", p50),
            ]
    return [out[k] for k in sorted(out)]


def read_forecast_log(path: Path, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Recorded issues with issued_at in [start - 1 day, end] (a bad line is skipped)."""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            issue = json.loads(line)
            t = datetime.fromisoformat(issue["issued_at"])
        except ValueError, KeyError, TypeError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=start.tzinfo)
        if start - timedelta(days=1) <= t <= end:
            out.append(issue)
    out.sort(key=lambda i: i["issued_at"])
    return out


async def pull(
    client: HassClient,
    cfg: Config,
    start: datetime,
    end: datetime,
    *,
    load_days: int = 14,
    forecast_log: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The snapshot dict for [start, end) (aware datetimes)."""
    now = now or datetime.now(UTC)
    ha = await client.config()
    tz = zone(ha.get("time_zone"))
    notes: list[str] = []
    ids = cfg.entity_ids()
    current = await client.states_of(ids)
    states = {eid: clean_state(st, cfg, tz) for eid, st in current.items() if st}
    legacy = {e for c in cfg.cars.values() for k, e in c.items() if k.startswith("legacy_")}
    missing = sorted(eid for eid, st in current.items() if st is None and eid not in legacy)
    if missing:
        notes.append(f"not in Home Assistant: {', '.join(missing)}")
    with_attrs = attribute_entities(cfg)
    plain = [e for e in ids if e not in with_attrs and current.get(e)]
    attr_ids = [e for e in with_attrs if current.get(e)]
    history: dict[str, list[list[Any]]] = {}
    t0 = start
    while t0 < end:  # a day per request keeps each answer small
        t1 = min(end, t0 + timedelta(days=1))
        plain_rows = await client.history(plain, t0, t1, minimal=True, no_attributes=True)
        attr_rows = await client.history(attr_ids, t0, t1, minimal=False, attribute_changes=True)
        for eid, rows in plain_rows.items():
            _extend(history.setdefault(eid, []), clean_history(rows, eid, cfg, tz, False))
        for eid, rows in attr_rows.items():
            _extend(history.setdefault(eid, []), clean_history(rows, eid, cfg, tz, True))
        t0 = t1
    stat_ids = [cfg.site[r] for r in STAT_ROLES]
    stat_from = start - timedelta(days=load_days)
    statistics: dict[str, Any] = {}
    for period in ("5minute", "hour"):
        try:
            raw = await client.statistics_during_period(
                stat_ids, stat_from, end, period=period, types=("mean",)
            )
        except HassError as err:
            notes.append(f"{period} statistics unavailable: {err}")
            raw = {}
        statistics[period] = clean_statistics(raw, tz)
    first5 = min(
        (rows[0]["start"] for rows in statistics["5minute"].values() if rows), default=None
    )
    if first5 is None or first5 > stat_from.astimezone(tz).isoformat():
        notes.append(
            f"5-min statistics start {first5 or 'nowhere'} (the recorder keeps them about "
            "10 days); hourly statistics cover the rest"
        )
    try:
        meta = await client.statistics_metadata(stat_ids)
    except HassError:
        meta = []
    stat_units = {
        m["statistic_id"]: m.get("statistics_unit_of_measurement")
        for m in meta
        if m.get("statistic_id")
    }
    weekly = None
    if eid := cfg.site.get("free_hours"):
        try:
            weekly = await client.schedule_config(eid)
        except HassError as err:
            notes.append(f"free-hours schedule unreadable: {err}")
        if weekly is not None:
            names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            weekly = {
                d: [{"from": b["from"], "to": b["to"]} for b in weekly.get(d) or ()] for d in names
            }
    log = forecast_log or tool_config.OUT_DIR / FORECAST_LOG
    issues = read_forecast_log(log, start, end)
    if not issues:
        notes.append(
            "no recorded forecast issues in the range: replays approximate the half hours "
            "from the recorded daily totals (run 'snapshot record-forecasts' to keep them)"
        )
    fc_today = states.get(cfg.site["forecast_today"], {})
    fc_tomorrow = states.get(cfg.site["forecast_tomorrow"], {})
    polled = states.get(cfg.site["forecast_last_polled"], {}).get("state")
    return {
        "format": FORMAT,
        "version": VERSION,
        "created_at": now.astimezone(tz).isoformat(),
        "home_assistant": {k: ha[k] for k in HA_CONFIG_KEEP if k in ha},
        "time_zone": str(ha.get("time_zone") or "UTC"),
        "range": {"start": start.astimezone(tz).isoformat(), "end": end.astimezone(tz).isoformat()},
        "mapping": {
            "site": dict(cfg.site),
            "cars": {cid: dict(c) for cid, c in cfg.cars.items()},
            "units": cfg.section("units"),
            "tariff": cfg.section("tariff"),
            "battery": cfg.section("battery"),
            "settings": cfg.section("settings"),
            "plans": cfg.section("plans"),
        },
        "states": states,
        "history": history,
        "statistics": statistics,
        "statistics_units": stat_units,
        "free_hours": weekly,
        "forecast": {
            "current": {
                "issued_at": local_iso(polled, tz) if polled not in (None, "unknown") else None,
                "slots": slots_from_attributes(
                    fc_today.get("attributes"), fc_tomorrow.get("attributes")
                ),
            },
            "issues": issues,
        },
        "notes": notes,
    }


# --------------------------------------------------------------------------- recording


def last_logged_issue(path: Path) -> str | None:
    if not path.is_file():
        return None
    last = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            last = json.loads(line)["issued_at"]
        except ValueError, KeyError, TypeError:
            continue
    return last


async def record_once(client: HassClient, cfg: Config, log: Path, tz: tzinfo) -> bool:
    """Append the current forecast issue if it is new; True when one was written."""
    ids = [cfg.site[r] for r in ("forecast_last_polled", "forecast_today", "forecast_tomorrow")]
    st = await client.states_of(ids)
    polled = (st[ids[0]] or {}).get("state")
    if polled in (None, "unknown", "unavailable"):
        return False
    issued = local_iso(polled, tz)
    if issued == last_logged_issue(log):
        return False
    slots = slots_from_attributes(
        (st[ids[1]] or {}).get("attributes"), (st[ids[2]] or {}).get("attributes")
    )
    if not slots:
        return False
    log.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "issued_at": issued,
        "recorded_at": datetime.now(UTC).astimezone(tz).isoformat(),
        "slots": slots,
    }
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, separators=(",", ":")) + "\n")
    return True


async def record_forecasts(
    client: HassClient,
    cfg: Config,
    log: Path,
    *,
    every_s: float = 600.0,
    cycles: int | None = None,
    say=print,
) -> int:
    """Poll every ``every_s`` seconds; ``cycles`` limits the loop (None = until Ctrl-C)."""
    tz = zone((await client.config()).get("time_zone"))
    written, n = 0, 0
    while cycles is None or n < cycles:
        n += 1
        try:
            if await record_once(client, cfg, log, tz):
                written += 1
                say(f"{datetime.now(tz):%Y-%m-%d %H:%M} recorded a new forecast issue -> {log}")
        except HassError as err:
            say(f"{datetime.now(tz):%Y-%m-%d %H:%M} {err}; retrying")
        if cycles is None or n < cycles:
            await asyncio.sleep(every_s)
    return written


# --------------------------------------------------------------------------- CLI


def day_range(
    start: str | None, end: str | None, days: int | None, tz: tzinfo, now: datetime
) -> tuple[datetime, datetime]:
    """[start 00:00, end + 1 day 00:00) in local time, never past now."""
    now_l = now.astimezone(tz)
    if start:
        d0 = date.fromisoformat(start)
        d1 = date.fromisoformat(end) if end else now_l.date()
        t0 = datetime.combine(d0, time(0), tzinfo=tz)
        t1 = min(datetime.combine(d1 + timedelta(days=1), time(0), tzinfo=tz), now_l)
    else:
        t1 = now_l
        t0 = datetime.combine(now_l.date() - timedelta(days=(days or 3) - 1), time(0), tzinfo=tz)
    if t1 <= t0:
        raise ValueError("the range is empty")
    return t0, t1


def write_json(data: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), default=str), encoding="utf-8")
    tmp.replace(path)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", help="entity mapping (default: tools/config.json or the example)")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pull", help="write a snapshot of a date range")
    p.add_argument("--start", help="first local date, YYYY-MM-DD")
    p.add_argument("--end", help="last local date (inclusive); default today")
    p.add_argument("--days", type=int, help="the last N days up to now (default 3)")
    p.add_argument("--load-days", type=int, default=14, help="statistics before the range")
    p.add_argument(
        "--forecast-log", type=Path, help=f"recorded issues (default out/{FORECAST_LOG})"
    )
    p.add_argument("--out", type=Path, help="output file (default tools/out/snapshot_*.json)")
    r = sub.add_parser("record-forecasts", help="record every new Solcast issue")
    r.add_argument("--log", type=Path, help=f"default tools/out/{FORECAST_LOG}")
    r.add_argument("--every", type=float, default=10.0, help="minutes between checks")
    r.add_argument("--once", action="store_true", help="check once and exit")
    return ap


async def amain(argv: Sequence[str] | None = None, *, client: HassClient | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        own = client is None
        client = client or HassClient.from_env()
    except (ConfigError, HassError) as err:
        print(f"snapshot: {err}", file=sys.stderr)
        return 2
    try:
        if args.command == "record-forecasts":
            log = args.log or tool_config.OUT_DIR / FORECAST_LOG
            n = await record_forecasts(
                client, cfg, log, every_s=args.every * 60, cycles=1 if args.once else None
            )
            print(f"{n} new issue(s) recorded in {log}")
            return 0
        tz = zone((await client.config()).get("time_zone"))
        try:
            start, end = day_range(args.start, args.end, args.days, tz, datetime.now(UTC))
        except ValueError as err:
            print(f"snapshot: {err}", file=sys.stderr)
            return 2
        data = await pull(
            client, cfg, start, end, load_days=args.load_days, forecast_log=args.forecast_log
        )
        out = args.out or tool_config.OUT_DIR / (
            f"snapshot_{start.date().isoformat()}_{(end - timedelta(seconds=1)).date()}.json"
        )
        write_json(data, out)
        print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
        for note in data["notes"]:
            print(f"  note: {note}")
        return 0
    except HassError as err:
        print(f"snapshot: {err}", file=sys.stderr)
        return 2
    finally:
        if own:
            await client.close()


def main() -> None:
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
