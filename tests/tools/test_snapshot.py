"""snapshot.py against the fake Home Assistant: what a snapshot holds, what it must never
hold, forecast recording, and the command line."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path

from fake_hass import FakeHass
import pytest
from synthetic import TZ, E, build_site

from tools import snapshot
from tools.config import load_config
from tools.hass_client import HassClient

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=TZ)
START = datetime(2026, 10, 20, 0, 0, tzinfo=TZ)
VIN = "TESTVEHCLE0000001"  # made up, shaped like a VIN


async def pulled(client: HassClient, fake: FakeHass, **kw) -> dict:
    build_site(fake, NOW)
    # sensitive extras a real instance may carry
    fake.set_state(E["t_soc"], 63, {"unit_of_measurement": "%", "vin": VIN, "car": f"car {VIN}"})
    fake.set_state(
        E["r_state"],
        "idle",
        dict(fake.states[E["r_state"]]["attributes"], owner="owner@example.invalid"),
        when=NOW - timedelta(minutes=20),
    )
    fake.set_state(
        E["r_state"],
        "idle",
        dict(fake.states[E["r_state"]]["attributes"], requested_amps=24),
        when=NOW - timedelta(minutes=10),
    )
    return await snapshot.pull(client, load_config(), START, NOW, now=NOW, **kw)


async def test_snapshot_contents(client: HassClient, fake: FakeHass) -> None:
    snap = await pulled(client, fake)
    assert snap["format"] == snapshot.FORMAT and snap["version"] == snapshot.VERSION
    assert snap["time_zone"] == "America/Los_Angeles"
    assert snap["range"] == {"start": START.isoformat(), "end": NOW.isoformat()}
    assert snap["mapping"]["site"]["pv_power"] == E["pv"]
    # history: the state at the start plus every change, in local time
    pv = snap["history"][E["pv"]]
    assert pv[0][0] == START.isoformat() and len(pv) > 200
    assert all(len(r) == 2 for r in pv)
    plugged = snap["history"][E["r_plugged"]]
    assert plugged == [["2026-10-20T00:00:00-07:00", "off"], ["2026-10-21T10:05:00-07:00", "on"]]
    # pulled a day at a time: the second day's starting state is not repeated
    assert all(a[1] != b[1] for a, b in zip(pv, pv[1:], strict=False))
    # forecast totals keep their estimates; the adapter keeps its attributes
    fc = snap["history"][E["fc_today"]]
    assert fc[-1][2]["estimate10"] < fc[-1][2]["estimate"] < fc[-1][2]["estimate90"]
    assert "detailedForecast" not in fc[-1][2]  # not recorded by HA
    adapter = snap["history"][E["r_state"]]
    # a change of attributes alone (16 -> 24 A while idle) is kept
    assert [r[2]["requested_amps"] for r in adapter if r[1] == "idle"] == [16, 24]
    # statistics: 5-min rows and hourly rows from 14 days before the range
    five = snap["statistics"]["5minute"][E["pv"]]
    hour = snap["statistics"]["hour"][E["pv"]]
    assert five[0]["start"] > (START - timedelta(days=14)).isoformat()
    assert hour[0]["start"] <= (START - timedelta(days=13)).isoformat()
    assert snap["statistics_units"][E["wall"]] == "kW"
    # the current forecast: today and tomorrow, 48 half hours each
    cur = snap["forecast"]["current"]
    assert len(cur["slots"]) == 96 and cur["issued_at"].startswith("2026-10-21T14:03")
    assert snap["free_hours"]["monday"] == [
        {"from": "00:00:00", "to": "06:00:00"},
        {"from": "20:00:00", "to": "23:59:59"},
    ]
    assert any("no recorded forecast issues" in n for n in snap["notes"])
    assert not any("roxxo_charger" in n for n in snap["notes"])  # legacy names are optional
    assert fake.service_calls == []


async def test_snapshot_is_sanitized(client: HassClient, fake: FakeHass) -> None:
    text = json.dumps(await pulled(client, fake))
    for secret in (
        "latitude",
        "longitude",
        "32.87",
        "-117.22",
        "gps_accuracy",
        "Office",  # the Tesla's zone name
        "Test Home",
        VIN,
        "owner@example.invalid",
        "test-user-id",
        "context",
        "abcd_1234",  # the Solcast site id in a per-site attribute
        "test-long-lived-token",
        "127.0.0.1",
    ):
        assert secret not in text, secret
    assert '"not_home"' in text


async def test_forecast_recording_and_merge(
    client: HassClient, fake: FakeHass, tmp_path: Path
) -> None:
    build_site(fake, NOW)
    cfg = load_config()
    log = tmp_path / "issues.jsonl"
    tz = TZ
    assert await snapshot.record_once(client, cfg, log, tz)
    assert not await snapshot.record_once(client, cfg, log, tz)  # same issue: nothing new
    fake.set_state(E["fc_polled"], (NOW - timedelta(minutes=15)).isoformat())
    assert await snapshot.record_once(client, cfg, log, tz)
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(lines) == 2 and len(lines[0]["slots"]) == 96
    assert lines[0]["slots"][0][0].startswith("2026-10-21T00:00:00")
    snap = await snapshot.pull(client, cfg, START, NOW, now=NOW, forecast_log=log)
    assert [i["issued_at"] for i in snap["forecast"]["issues"]] == [
        lines[0]["issued_at"],
        lines[1]["issued_at"],
    ]
    # an issue far before the range is left out; a bad line is skipped
    with log.open("a") as f:
        f.write("not json\n")
        f.write(json.dumps({"issued_at": "2026-09-01T10:00:00-07:00", "slots": []}) + "\n")
    assert len(snapshot.read_forecast_log(log, START, NOW)) == 2


async def test_record_loop_survives_errors(client: HassClient, fake: FakeHass, tmp_path) -> None:
    build_site(fake, NOW)
    fake.remove(E["fc_polled"])
    said: list[str] = []
    n = await snapshot.record_forecasts(
        client, load_config(), tmp_path / "x.jsonl", every_s=0, cycles=2, say=said.append
    )
    assert n == 0 and not (tmp_path / "x.jsonl").exists()


def test_day_range() -> None:
    now = datetime(2026, 10, 21, 15, 0, tzinfo=TZ)
    t0, t1 = snapshot.day_range("2026-10-18", "2026-10-19", None, TZ, now)
    assert (t0.isoformat(), t1.isoformat()) == (
        "2026-10-18T00:00:00-07:00",
        "2026-10-20T00:00:00-07:00",
    )
    t0, t1 = snapshot.day_range(None, None, 2, TZ, now)
    assert t0 == datetime(2026, 10, 20, tzinfo=TZ) and t1 == now
    t0, t1 = snapshot.day_range("2026-10-21", None, None, TZ, now)
    assert t1 == now
    with pytest.raises(ValueError):
        snapshot.day_range("2026-10-25", None, None, TZ, now)


async def test_cli_pull_and_record(client: HassClient, fake: FakeHass, tmp_path, capsys) -> None:
    build_site(fake, datetime.now(TZ).replace(second=0, microsecond=0))
    out = tmp_path / "snap.json"
    code = await snapshot.amain(["pull", "--days", "1", "--out", str(out)], client=client)
    assert code == 0, capsys.readouterr().err
    assert json.loads(out.read_text())["format"] == snapshot.FORMAT
    log = tmp_path / "log.jsonl"
    assert (
        await snapshot.amain(["record-forecasts", "--once", "--log", str(log)], client=client) == 0
    )
    assert "1 new issue" in capsys.readouterr().out
    # the default output goes to tools/out (redirected to tmp in tests)
    assert await snapshot.amain(["pull", "--days", "1"], client=client) == 0
    assert list((tmp_path / "out").glob("snapshot_*.json"))
    assert fake.service_calls == []


async def test_cli_errors(capsys) -> None:
    assert await snapshot.amain(["pull"]) == 2
    assert "HASS_URL" in capsys.readouterr().err
    assert await snapshot.amain(["--config", "/nonexistent.json", "pull"]) == 2


async def test_cli_empty_range(client: HassClient, fake: FakeHass, capsys) -> None:
    build_site(fake, NOW)
    assert await snapshot.amain(["pull", "--start", "2099-01-01"], client=client) == 2
    assert "empty" in capsys.readouterr().err
