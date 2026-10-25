"""replay.py, timeline.py and chart.py on a snapshot pulled from the fake Home Assistant."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import ModuleType

from fake_hass import FakeHass
import pytest
from synthetic import TZ, E, build_site

from tools import replay, snapshot
from tools.chart import render_html
from tools.config import load_config
from tools.hass_client import HassClient
from tools.timeline import Timeline

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=TZ)
START = datetime(2026, 10, 20, 0, 0, tzinfo=TZ)
PLUG = datetime(2026, 10, 21, 10, 5)  # naive local: when the replay sees the Rivian plugged


async def make_snapshot(client: HassClient, fake: FakeHass, log: Path | None = None) -> dict:
    build_site(fake, NOW)
    cfg = load_config()
    if log is not None:
        await snapshot.record_once(client, cfg, log, TZ)
    snap = await snapshot.pull(client, cfg, START, NOW, now=NOW, forecast_log=log)
    return json.loads(json.dumps(snap))  # exactly what a file holds


async def test_shadow_replay(client: HassClient, fake: FakeHass, core: ModuleType) -> None:
    snap = await make_snapshot(client, fake)
    res = replay.replay(snap, core=core)
    assert len(res.rows) == 39 * 12
    assert res.rows[0].t == datetime(2026, 10, 20, 0, 0)
    assert {r.mode for r in res.rows if r.t < PLUG} == {"empty"}
    first = res.changes[0]
    assert first.t == PLUG and first.car == "rivian" and first.from_a == 0
    assert first.mode == "day" and 8 <= first.to_a <= 48 and first.to_a % 2 == 0
    assert all(r.car == "rivian" for r in res.rows if r.t >= PLUG)
    # day rows carry the forecast in force, its source and the planner's numbers
    noon = next(r for r in res.rows if r.t == datetime(2026, 10, 21, 12, 0))
    assert noon.pv_p10_kw < noon.pv_p50_kw < noon.pv_p90_kw
    assert noon.p_cap_kw is not None and noon.reason
    assert noon.price == pytest.approx(0.32)
    night = next(r for r in res.rows if r.t == datetime(2026, 10, 20, 23, 0))
    assert night.price == 0.0
    assert set(res.sources) <= {"totals", "current"} and res.sources["totals"] > 400
    # predictions made 3 h earlier line up with the actual level
    pred = [r for r in res.rows if r.soc_pred is not None]
    assert len(pred) > 300
    assert max(abs(r.soc_pred - r.soc) for r in pred) < 25
    assert [d["day"] for d in res.days] == ["2026-10-20", "2026-10-21"]
    assert res.days[1]["changes"] == len(res.changes)
    assert res.days[1]["planned_car_kwh"] > 0 and res.days[1]["actual_car_kwh"] > 0
    assert res.days[0]["whatif_paid_import_kwh"] is None


async def test_whatif_replay(client: HassClient, fake: FakeHass, core: ModuleType) -> None:
    snap = await make_snapshot(client, fake)
    res = replay.replay(snap, core=core, whatif=True, step_min=15)
    assert len(res.rows) == 39 * 4
    assert all(r.soc_whatif is not None for r in res.rows)
    charging = [r for r in res.rows if r.car_whatif_kw]
    assert charging and charging[0].t >= PLUG
    assert charging[0].car_whatif_kw == pytest.approx(res.changes[0].to_a * 0.24)
    day = res.days[1]
    assert day["whatif_paid_import_kwh"] is not None and day["whatif_min_soc"] is not None
    assert res.whatif


def test_battery_model() -> None:
    from tools.replay import battery_step

    class Bat:  # the planner core's BatterySpec interface
        capacity_kwh, max_charge_kw, max_discharge_kw = 30.0, 9.0, 9.0
        efficiency, reserve_pct = 1.0, 10.0

        def kwh(self, pct):
            return self.capacity_kwh * pct / 100

        def pct(self, kwh):
            return 100 * kwh / self.capacity_kwh

    bat = Bat()
    soc, b, grid = battery_step(bat, 50, pv=6, house=1, car=2, hold=False, dt_h=1)
    assert (round(soc, 1), b, grid) == (60.0, 3, 0)  # surplus 3 kW charges the battery
    soc, b, grid = battery_step(bat, 50, pv=0, house=1, car=11, hold=False, dt_h=1)
    assert (b, grid) == (-9, 3)  # 9 kW from the battery, the rest imported
    soc, b, grid = battery_step(bat, 50, pv=0, house=1, car=11, hold=True, dt_h=1)
    assert (soc, b, grid) == (50, 0, 12)  # free hours: the battery holds
    soc, b, grid = battery_step(bat, 11, pv=0, house=2, car=0, hold=False, dt_h=1)
    assert round(soc, 6) == 10 and round(grid, 6) == 1.7  # only down to the reserve
    soc, b, grid = battery_step(bat, 99, pv=10, house=0, car=0, hold=False, dt_h=1)
    assert soc == 100 and round(grid, 6) == -9.7  # full: the rest is exported


async def test_recorded_issue_preferred(
    client: HassClient, fake: FakeHass, core: ModuleType, tmp_path: Path
) -> None:
    snap = await make_snapshot(client, fake, tmp_path / "log.jsonl")
    assert len(snap["forecast"]["issues"]) == 1
    res = replay.replay(snap, core=core, step_min=30)
    issued = datetime(2026, 10, 21, 14, 3)
    assert {r.forecast_source for r in res.rows if r.t >= issued} == {"recorded"}
    assert {r.forecast_source for r in res.rows if r.t < issued} == {"totals"}


async def test_hindsight_fallback_is_flagged(
    client: HassClient, fake: FakeHass, core: ModuleType
) -> None:
    snap = await make_snapshot(client, fake)
    for key in ("fc_today", "fc_tomorrow"):
        snap["history"][E[key]] = []
    snap["forecast"]["current"]["slots"] = []
    res = replay.replay(snap, core=core, step_min=60)
    assert set(res.sources) == {"actual"}
    assert any("hindsight" in n for n in res.notes)
    tl = Timeline(snap, core)
    day = tl.forecasts.day(datetime(2026, 10, 20).date(), datetime(2026, 10, 20, 9))
    actual = tl.pv_day_kwh(datetime(2026, 10, 20).date())
    assert sum(s.p50 for s in day.slots) / 2 == pytest.approx(actual)


async def test_timeline_sources_of_power(
    client: HassClient, fake: FakeHass, core: ModuleType
) -> None:
    snap = await make_snapshot(client, fake)
    full = Timeline(snap, core)
    snap["statistics"]["5minute"] = {}  # older than the recorder keeps: history, then hours
    tl = Timeline(snap, core)
    t = datetime(2026, 10, 21, 12, 0)
    # the fake reports each 5-min mean as the state at the end of its 5 minutes
    lag = timedelta(minutes=5)
    assert tl.power("pv_power", t) == pytest.approx(full.power("pv_power", t - lag), rel=0.01)
    before = datetime(2026, 10, 15, 12, 0)  # outside the history: hourly statistics
    assert tl.power("pv_power", before) is not None and tl.minutes[before] == 60.0
    assert tl.house(t) is not None
    assert tl.car_plugged("rivian", PLUG) and not tl.car_plugged(
        "rivian", PLUG - timedelta(hours=1)
    )
    assert not tl.car_plugged("tesla", PLUG)
    assert tl.battery(t).capacity_kwh == pytest.approx(31.2)
    tariff = tl.tariff()
    assert tariff.is_free(datetime(2026, 10, 21, 21, 0)) and not tariff.is_free(t)
    snap["free_hours"] = None  # no Schedule helper: the config's fallback window
    assert Timeline(snap, core).tariff().is_free(datetime(2026, 10, 21, 5, 0))
    # the totals in force at noon, issued at the 10:03 poll (polls every 2 h from 06:03)
    slots, issued, source = tl.forecasts.at(t)
    assert source == "totals" and issued == datetime(2026, 10, 21, 10, 3)
    assert len(slots) == 96 and slots[0].start == datetime(2026, 10, 21, 0, 0)
    pv_days = tl.forecasts.pv_days(datetime(2026, 10, 21).date())
    assert pv_days and pv_days[-1].day == datetime(2026, 10, 20).date()


async def test_settings_and_plans(client: HassClient, fake: FakeHass, core: ModuleType) -> None:
    snap = await make_snapshot(client, fake)
    snap["mapping"]["plans"] = {"rivian": [{"target_pct": 70, "at": "18:00", "weekdays": [2]}]}
    res = replay.replay(snap, core=core, step_min=60, settings={"caution": "balanced"})
    assert res.settings["caution"] == "balanced" and res.settings["shadow"] is True
    with pytest.raises(ValueError, match="unknown planner settings"):
        replay.replay(snap, core=core, settings={"nope": 1})
    assert replay.parse_settings(["caution=balanced", "floor_pct=25", "shadow=off", "x=none"]) == {
        "caution": "balanced",
        "floor_pct": 25,
        "shadow": False,
        "x": None,
    }
    with pytest.raises(ValueError):
        replay.parse_settings(["caution"])


async def test_cli_writes_csv_and_html(
    client: HassClient, fake: FakeHass, core: ModuleType, tmp_path: Path, capsys
) -> None:
    snap = await make_snapshot(client, fake)
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(snap))
    stem = tmp_path / "r"
    assert replay.main([str(path), "--out", str(stem), "--step", "15", "--whatif"]) == 0
    out = capsys.readouterr().out
    assert "2026-10-21:" in out and "what-if paid import" in out
    rows = list(csv.DictReader((tmp_path / "r.csv").open()))
    assert len(rows) == 39 * 4 and rows[0]["t"] == "2026-10-20T00:00"
    assert {"mode", "planned_a", "reason", "soc_pred", "soc_whatif", "forecast_source"} <= set(
        rows[0]
    )
    page = (tmp_path / "r.html").read_text()
    assert page.startswith("<!doctype html>") and page.count("<svg") == 2
    for external in ("http://", "https://", "<link", " src=", "@import"):
        assert external not in page, external
    assert "prefers-color-scheme: dark" in page
    # default output folder (tools/out, redirected to tmp in tests), and a time window
    assert replay.main([str(path), "--from", "2026-10-21T09:00", "--to", "2026-10-21T12:00"]) == 0
    assert list((tmp_path / "out").glob("replay_2026-10-21_2026-10-21.*"))


def test_cli_refusals(tmp_path: Path, monkeypatch, capsys) -> None:
    assert replay.main([]) == 2
    assert "snapshot file or --live" in capsys.readouterr().err
    assert replay.main([str(tmp_path / "missing.json")]) in (2,)
    (tmp_path / "bad.json").write_text("{")
    assert replay.main([str(tmp_path / "bad.json")]) == 2

    def no_core():
        from tools.core import CoreNotFound

        raise CoreNotFound("no core here")

    monkeypatch.setattr("tools.replay.load_core", no_core)
    assert replay.main([str(tmp_path / "bad.json")]) == 2
    assert "no core here" in capsys.readouterr().err


def test_cli_live_needs_a_connection(core: ModuleType, capsys) -> None:
    assert replay.main(["--live", "--days", "1"]) == 2
    assert "HASS_TOKEN" in capsys.readouterr().err


async def test_html_escapes_reasons(client: HassClient, fake: FakeHass, core: ModuleType) -> None:
    snap = await make_snapshot(client, fake)
    res = replay.replay(snap, core=core, step_min=60)
    evil = '</script><img onerror="x">'
    res.rows[-1].reason = evil
    res.changes[0].reason = evil
    page = render_html(res)
    assert evil not in page
    assert "&lt;/script&gt;&lt;img" in page  # the changes table
    assert '<\\/script><img onerror=\\"x\\">' in page  # the embedded JSON
