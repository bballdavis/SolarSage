"""attended_test.py against the fake Home Assistant with emulated cars: the opt-in gates,
the refusals, the Tesla current steps with restore, and the Rivian trial through Rivian
schedule control."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import json
from pathlib import Path

from fake_cars import FakeCars, live_schedule_control, tesla_on_charger
from fake_hass import TOKEN, FakeHass
import pytest
from synthetic import TZ, E, build_site, schedule_control_attrs

from tools import attended_test as at
from tools.hass_client import HassClient

NOW = datetime(2026, 10, 21, 13, 0, tzinfo=TZ)


class Clock:
    """Fake time: sleeping advances it at once."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s
        await asyncio.sleep(0)


class Owner:
    """Scripted answers: (prompt fragment, answer or a callable that returns one)."""

    def __init__(self, script: list[tuple[str, str | Callable[[], str]]] | None = None) -> None:
        self.script = list(script or [])
        self.prompts: list[str] = []
        self.said: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        for i, (frag, ans) in enumerate(self.script):
            if frag.lstrip("*") in prompt:
                if not frag.startswith("*"):  # "*...": answers every time, not once
                    self.script.pop(i)
                return ans() if callable(ans) else ans
        if "run this step" in prompt or "send " in prompt:
            return "y"
        if "restore now" in prompt or "do it now" in prompt:
            return ""  # Enter: the default yes
        return ""

    def console(self) -> at.Console:
        return at.Console(self.ask, self.said.append)

    @property
    def text(self) -> str:
        return "\n".join(self.said)


@pytest.fixture
async def live(url: str):
    async with HassClient(url, TOKEN, allow_service_calls=True) as c:
        yield c


async def run(client, owner: Owner, clock: Clock, *argv: str, tmp: Path) -> int:
    return await at.amain(
        [*argv, "--log", str(tmp / "log.jsonl"), "--poll", "5"],
        client=client,
        console=owner.console(),
        interactive=True,
        sleep=clock.sleep,
        clock=clock,
    )


def log_lines(tmp: Path) -> list[dict]:
    return [json.loads(x) for x in (tmp / "log.jsonl").read_text().splitlines()]


# --------------------------------------------------------------------------- gates


async def test_needs_the_flag(fake: FakeHass, server, capsys) -> None:
    owner = Owner()
    code = await at.amain(["tesla"], console=owner.console(), interactive=True)
    assert code == 2 and "--i-am-at-the-car" in owner.text
    assert fake.requests == []  # never even connected


async def test_needs_a_terminal(fake: FakeHass, server) -> None:
    owner = Owner()
    code = await at.amain(
        ["tesla", "--i-am-at-the-car"], console=owner.console(), interactive=False
    )
    assert code == 2 and "interactive terminal" in owner.text
    assert fake.requests == []


async def test_unknown_car_and_steps(live, fake, tmp_path) -> None:
    build_site(fake, NOW)
    owner = Owner()
    assert await run(live, owner, Clock(), "bmw", "--i-am-at-the-car", tmp=tmp_path) == 2
    assert (
        await run(live, owner, Clock(), "rivian", "--i-am-at-the-car", "--only", "t9", tmp=tmp_path)
        == 2
    )
    assert fake.service_calls == []


async def test_connection_from_the_environment(url, fake, tmp_path, monkeypatch) -> None:
    """Without an injected client the tool makes its own, allowed to call services."""
    build_site(fake, NOW)
    tesla_on_charger(fake)
    clock = Clock()
    FakeCars(fake, clock)
    monkeypatch.setenv("HASS_URL", url)
    monkeypatch.setenv("HASS_TOKEN", TOKEN)
    owner = Owner([("run this step", "q")])
    code = await at.amain(
        ["tesla", "--i-am-at-the-car", "--log", str(tmp_path / "l.jsonl")],
        console=owner.console(),
        interactive=True,
        sleep=clock.sleep,
        clock=clock,
    )
    assert code == 1 and "Stopped by the owner" in owner.text
    assert fake.service_calls == []


async def test_refuses_while_the_planner_commands(live, fake, tmp_path) -> None:
    build_site(fake, NOW)
    tesla_on_charger(fake)
    fake.set_state("sensor.solar_ev_planner_heartbeat", "active", {})
    owner = Owner()
    code = await run(live, owner, Clock(), "tesla", "--i-am-at-the-car", tmp=tmp_path)
    assert code == 3 and "shadow mode is off" in owner.text
    fake.set_state("sensor.solar_ev_planner_heartbeat", "shadow", {})
    owner = Owner([("run this step", "q")])
    assert await run(live, owner, Clock(), "tesla", "--i-am-at-the-car", tmp=tmp_path) == 1
    assert fake.service_calls == []


async def test_rivian_refuses_while_the_schedule_control_sees_its_controller_down(
    live, fake, tmp_path
) -> None:
    build_site(fake, NOW)
    live_schedule_control(fake)
    observation = {"plugged": True, "home": True, "at_charger": True, "controller_alive": False}
    fake.set_state(
        E["r_state"], "idle", schedule_control_attrs(dry_run=False, observation=observation)
    )
    owner = Owner()
    code = await run(live, owner, Clock(), "rivian", "--i-am-at-the-car", tmp=tmp_path)
    assert code == 3 and "controller as down" in owner.text
    assert fake.service_calls == []


# --------------------------------------------------------------------------- Tesla


async def test_tesla_steps_and_restore(live, fake, tmp_path) -> None:
    build_site(fake, NOW)
    tesla_on_charger(fake)
    clock = Clock()
    FakeCars(fake, clock)
    owner = Owner()
    code = await run(live, owner, clock, "tesla", "--i-am-at-the-car", tmp=tmp_path)
    assert code == 0, owner.text
    calls = [(c["service"], c["data"].get("value")) for c in fake.service_calls]
    assert calls == [
        ("turn_on", None),
        ("set_value", 16),
        ("set_value", 10),
        ("set_value", 6),
        ("set_value", 16),
        ("turn_off", None),  # restored: it was not charging
    ]
    assert all(
        c["target"]["entity_id"].startswith(("switch.roxxo", "number.roxxo"))
        for c in fake.service_calls
    )
    assert "[PASS] step 3: 6 A" in owner.text and "6 command(s) sent" in owner.text
    assert fake.states[E["t_switch"]]["state"] == "off"
    assert fake.states[E["t_current"]]["state"] == "16"
    lines = log_lines(tmp_path)
    assert {x["event"] for x in lines} >= {"begin", "sent", "sample", "result", "summary"}
    text = (tmp_path / "log.jsonl").read_text()
    assert "latitude" not in text and "Office" not in text and TOKEN not in text


async def test_tesla_not_following_fails(live, fake, tmp_path) -> None:
    build_site(fake, NOW)
    tesla_on_charger(fake)
    clock = Clock()
    cars = FakeCars(fake, clock)
    cars.wall = lambda amps: None  # the Wall Connector never moves
    owner = Owner()
    code = await run(live, owner, clock, "tesla", "--i-am-at-the-car", "--amps", "10", tmp=tmp_path)
    assert code == 1
    assert "[FAIL] step 1: 10 A" in owner.text


async def test_tesla_declined_and_quit_still_restore(live, fake, tmp_path) -> None:
    build_site(fake, NOW)
    tesla_on_charger(fake)
    fake.set_state(E["t_switch"], "on")
    clock = Clock()
    FakeCars(fake, clock)
    owner = Owner(
        [
            ("send number.set_value", "y"),
            ("send number.set_value", "n"),
            ("run this step", "y"),
            ("run this step", "y"),
            ("run this step", "q"),
        ]
    )
    code = await run(live, owner, clock, "tesla", "--i-am-at-the-car", tmp=tmp_path)
    assert code == 1
    # 16 A sent, 10 A declined, then quit: the current is already as it was
    assert [c["data"].get("value") for c in fake.service_calls] == [16]
    assert "already as it was" in owner.text


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        (lambda f: f.set_state(E["t_plugged"], "off"), "plugged in at home"),
        (lambda f: f.set_state(E["t_location"], "Work"), "plugged in at home"),
        (lambda f: f.set_state(E["wall_connected"], "off"), "sees no vehicle"),
        (lambda f: f.set_state(E["r_plugged"], "on"), "only one car"),
        (lambda f: f.remove(E["t_switch"]), "Tesla Fleet"),
        (lambda f: f.set_state(E["t_soc"], 90), "charge limit"),
    ],
)
async def test_tesla_refusals(live, fake, tmp_path, setup, why) -> None:
    build_site(fake, NOW)
    tesla_on_charger(fake)
    setup(fake)
    owner = Owner([("continue anyway", "n")])
    code = await run(live, owner, Clock(), "tesla", "--i-am-at-the-car", tmp=tmp_path)
    assert code == 3 and "REFUSED" in owner.text and why in owner.text
    assert fake.service_calls == []


# --------------------------------------------------------------------------- Rivian


@pytest.mark.parametrize(
    ("state", "attrs", "why"),
    [
        ("dry_run", {"dry_run": True}, "dry run"),
        ("fault", {"dry_run": False, "fault_code": "external_edit"}, "faulted"),
        ("handed_back", {"dry_run": False, "handed_back": True}, "handed back"),
    ],
)
async def test_rivian_refusals(live, fake, tmp_path, state, attrs, why) -> None:
    build_site(fake, NOW)
    fake.set_state(E["r_state"], state, schedule_control_attrs(**attrs))
    owner = Owner()
    code = await run(live, owner, Clock(), "rivian", "--i-am-at-the-car", tmp=tmp_path)
    assert code == 3 and why in owner.text
    assert fake.service_calls == []


async def test_rivian_refuses_without_schedule_control(live, fake, tmp_path) -> None:
    build_site(fake, NOW)
    fake.remove(E["r_state"])
    owner = Owner()
    assert await run(live, owner, Clock(), "rivian", "--i-am-at-the-car", tmp=tmp_path) == 3
    assert "is Rivian schedule control set up" in owner.text
    live_schedule_control(fake)
    fake.set_state(E["r_status"], "a")
    owner = Owner()
    assert await run(live, owner, Clock(), "rivian", "--i-am-at-the-car", tmp=tmp_path) == 3
    assert fake.service_calls == []


async def test_rivian_full_trial(live, fake, tmp_path) -> None:
    build_site(fake, NOW)
    live_schedule_control(fake)
    clock = Clock()
    FakeCars(fake, clock)

    def replugged() -> str:
        fake.set_state(E["r_state"], "idle", schedule_control_attrs(dry_run=False))
        return ""

    owner = Owner(
        [
            ("plugged in again", replugged),
            ("Did the car wait", "y"),
            ("What happened", "it stayed waiting"),
            ("own schedules again", "y"),
        ]
    )
    code = await run(live, owner, clock, "rivian", "--i-am-at-the-car", tmp=tmp_path)
    assert code == 0, owner.text
    for step in (
        "inventory",
        "T1 write and readback",
        "replug",
        "T2 16 A",
        "T2 10 A",
        "T2 20 A",
        "T2b continuity",
        "T3 window off now",
        "T4 lease expiry",
        "T4 park",
        "T5 window back on",
        "T6 plug in while parked",
        "T7 exact restore",
    ):
        assert f"[PASS] {step}" in owner.text, step
    # only the schedule control's entities were commanded, never a Rivian service
    targets = {c["target"]["entity_id"] for c in fake.service_calls}
    assert targets <= {E[k] for k in ("r_switch", "r_current", "r_renewal", "r_restore", "r_read")}
    assert fake.states[E["r_renewal"]]["state"] == "on"
    notes = [x for x in log_lines(tmp_path) if x["event"] == "owner_note"]
    assert any(n["note"] == "it stayed waiting" for n in notes)


async def test_rivian_selected_steps_and_cleanup(live, fake, tmp_path) -> None:
    """Only T2; stopping there leaves charging on, so the clean-up offers to undo it."""
    build_site(fake, NOW)
    live_schedule_control(fake)
    clock = Clock()
    FakeCars(fake, clock)
    owner = Owner()
    code = await run(
        live, owner, clock, "rivian", "--i-am-at-the-car", "--only", "t2", tmp=tmp_path
    )
    assert code == 0, owner.text
    assert "== clean up" in owner.text
    last = [(c["service"], c["target"]["entity_id"]) for c in fake.service_calls][-2:]
    assert last == [("turn_off", E["r_switch"]), ("press", E["r_restore"])]
    assert fake.states[E["r_state"]]["state"] == "handed_back"


async def test_rivian_skip_everything(live, fake, tmp_path) -> None:
    build_site(fake, NOW)
    live_schedule_control(fake)
    owner = Owner([("*run this step", "s")])
    code = await run(live, owner, Clock(), "rivian", "--i-am-at-the-car", tmp=tmp_path)
    assert code == 0, owner.text
    assert owner.text.count("[SKIP]") == len(at.RIVIAN_STEPS)
    assert fake.service_calls == []
