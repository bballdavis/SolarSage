"""OPT-IN live command test, for the owner standing at the car. It sends real commands.

    python -m tools.attended_test tesla --i-am-at-the-car
    python -m tools.attended_test rivian --i-am-at-the-car [--only t1,t2,t3]

Nothing happens without ``--i-am-at-the-car`` and an interactive terminal, and every
command is shown and confirmed before it is sent. The planner must not command the car
meanwhile, so you are the only writer: the test refuses while the planner's heartbeat shows
``active`` (its shadow mode is off).

**Tesla** (through HA's Tesla Fleet entities; Tesla bills each command): with the Tesla
plugged in at home and charging, it steps the charge current 16 -> 10 -> 6 -> 16 A and watches
the Wall Connector's measured current follow each step, then restores the current and the
charge switch as they were.

**Rivian** (through this repo's Rivian schedule control; it never writes the Rivian itself):
it guides the schedule control's attended trial (docs/RIVIAN_SCHEDULE_CONTROL.md): inventory,
T1 write and readback, T2 current steps, T2b continuity, T3 stop, T4 lease expiry, T5 resume,
T6 plug-in while parked, T7 exact restore. It uses the schedule control's switch, current
number, lease-renewal switch and buttons, and refuses while the schedule control is in dry
run.

Every observation and answer goes to ``tools/out/attended_<car>_<time>.jsonl``.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import time
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import config as tool_config  # noqa: E402
from tools.config import Config, ConfigError, load_config  # noqa: E402
from tools.hass_client import HassClient, HassError  # noqa: E402
from tools.sanitize import scrub, tracker_state  # noqa: E402

TESLA_STEPS = (16, 10, 6, 16)
RIVIAN_STEPS = ("inventory", "t1", "t2", "t2b", "t3", "t4", "t5", "t6", "t7")
SCHEDULE_CONTROL_ATTRS = (
    "dry_run",
    "phase",
    "desired_charge",
    "requested_amps",
    "lease_active",
    "lease_amps",
    "lease_written_at",
    "lease_expires_at",
    "last_write_at",
    "start_blocked_by",
    "request_held",
    "request_unconfirmed",
    "fault_code",
    "handed_back",
    "renewal_enabled",
    "owner_schedule_count",
    "provider_schedule_count",
    "last_provider_read_at",
    "observation",
)
UNAVAILABLE = ("unavailable", "unknown", None)


class Refused(Exception):
    """A precondition failed; nothing (more) is sent."""


class Quit(Exception):
    """The owner asked to stop."""


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except TypeError, ValueError:
        return None


@dataclass
class Console:
    """Questions and messages; injectable so tests can answer."""

    ask: Callable[[str], str] = input
    say: Callable[[str], None] = print

    def choose(self, prompt: str, default: str = "n") -> str:
        """'y', 'n', 's' (skip) or 'q' (quit); Enter takes the default."""
        while True:
            try:
                raw = self.ask(f"{prompt} ").strip().lower()
            except EOFError:
                return "q"
            ans = (raw or default)[:1]
            if ans in "ynsq":
                return ans
            self.say("  please answer y, n, s (skip) or q (quit)")

    def text(self, prompt: str) -> str:
        try:
            return self.ask(f"{prompt} ").strip()
        except EOFError:
            return ""


@dataclass
class Session:
    """One attended run: the client, the log and the pacing."""

    client: HassClient
    cfg: Config
    car_id: str
    console: Console
    log_path: Path
    poll_s: float = 5.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    results: list[dict[str, Any]] = field(default_factory=list)
    commands: int = 0

    @property
    def car(self) -> Mapping[str, str]:
        return self.cfg.cars[self.car_id]

    # ------------------------------------------------------------------ logging

    def record(self, step: str, event: str, **data: Any) -> None:
        line = {"t": datetime.now(UTC).isoformat(), "step": step, "event": event}
        line |= scrub(data)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str) + "\n")

    def result(self, step: str, ok: bool | None, detail: str, latency_s: float | None = None):
        res = {"step": step, "ok": ok, "detail": detail, "latency_s": latency_s}
        self.results.append(res)
        self.record(step, "result", ok=ok, detail=detail, latency_s=latency_s)
        mark = "PASS" if ok else "SKIP" if ok is None else "FAIL"
        lat = f" after {latency_s:.0f} s" if latency_s is not None else ""
        self.console.say(f"  [{mark}] {step}: {detail}{lat}")

    # ------------------------------------------------------------------ reading

    async def observe(self) -> dict[str, Any]:
        site, car = self.cfg.site, self.car
        keys = {
            "wall_a": site.get("wall_current"),
            "wall_kw": site.get("wall_power"),
            "connected": site.get("wall_connected"),
            "contactor": site.get("wall_contactor"),
            "switch": car.get("switch"),
            "current": car.get("current"),
            "soc": car.get("soc"),
            "limit": car.get("limit"),
            "plugged": car.get("plugged"),
            "location": car.get("location"),
            "control_state": car.get("control_state"),
            "charger_status": car.get("charger_status"),
            "lease_renewal": car.get("lease_renewal"),
            "planner": self.cfg.planner.get("heartbeat"),
        }
        others = {
            other_id: (other.get("plugged"), other.get("location"))
            for other_id, other in self.cfg.cars.items()
            if other_id != self.car_id
        }
        ids = [e for e in keys.values() if e] + [e for pair in others.values() for e in pair if e]
        states = await self.client.states_of(ids)
        out: dict[str, Any] = {}
        for name, eid in keys.items():
            st = states.get(eid) if eid else None
            value = st.get("state") if st else None
            if name == "location":
                value = tracker_state(value)
            out[name] = value
            if name == "current" and st:
                attrs = st.get("attributes") or {}
                out["current_min"], out["current_max"] = attrs.get("min"), attrs.get("max")
            if name == "control_state" and st:
                attrs = st.get("attributes") or {}
                out["schedule_control"] = {
                    k: attrs.get(k) for k in SCHEDULE_CONTROL_ATTRS if k in attrs
                }
                out["last_provider_read"] = attrs.get("last_provider_read")
        for other_id, (plugged, location) in others.items():
            at_home = tracker_state((states.get(location) or {}).get("state")) == "home"
            here = (states.get(plugged) or {}).get("state") == "on" and at_home
            out[f"{other_id}_plugged"] = "on" if here else "off"
        return out

    async def watch(
        self,
        step: str,
        what: str,
        done: Callable[[dict[str, Any]], bool],
        timeout_s: float,
    ) -> tuple[bool, float, dict[str, Any]]:
        """Poll until ``done`` holds or the timeout passes; every sample is logged."""
        self.console.say(f"  watching: {what} (up to {timeout_s / 60:.1f} min)")
        t0 = self.clock()
        while True:
            obs = await self.observe()
            elapsed = self.clock() - t0
            self.record(step, "sample", what=what, elapsed_s=round(elapsed, 1), obs=obs)
            if done(obs):
                return True, elapsed, obs
            if elapsed >= timeout_s:
                return False, elapsed, obs
            await self.sleep(self.poll_s)

    # ------------------------------------------------------------------ commanding

    async def act(self, step: str, domain: str, service: str, entity_id: str, **data: Any):
        """Show one service call, ask, send it. 'n' skips it (returns False)."""
        shown = f"{domain}.{service} {entity_id}" + (f" {data}" if data else "")
        ans = self.console.choose(f"  send {shown}? [y/N/q]", "n")
        if ans == "q":
            raise Quit
        if ans != "y":
            self.record(step, "declined", call=shown)
            return False
        await self.client.call_service(domain, service, data, target={"entity_id": entity_id})
        self.commands += 1
        self.record(step, "sent", call=shown)
        return True

    def begin(self, step: str, text: str) -> bool:
        """Describe a step and ask whether to run it ('s' or 'n' skips it)."""
        self.console.say("")
        self.console.say(f"== {step}: {text}")
        ans = self.console.choose("  run this step? [y/s/q]", "s")
        if ans == "q":
            raise Quit
        if ans != "y":
            self.result(step, None, "skipped by the owner")
            return False
        self.record(step, "begin", text=text)
        return True


# --------------------------------------------------------------------------- preconditions


def _on_charger(obs: Mapping[str, Any], car_id: str) -> None:
    if obs.get("connected") != "on":
        raise Refused("the Wall Connector sees no vehicle: plug the car in first")
    if obs.get("plugged") != "on" or obs.get("location") != "home":
        raise Refused(f"the {car_id} does not report being plugged in at home")
    others = [k.removesuffix("_plugged") for k, v in obs.items() if k.endswith("_plugged")]
    others = [o for o in others if obs.get(f"{o}_plugged") == "on"]
    if others:
        raise Refused(
            f"{', '.join(others)} also reports plugged in: only one car can be on the "
            "Wall Connector, and the test must know which one it is watching"
        )


def _planner_quiet(obs: Mapping[str, Any]) -> None:
    """The planner sends nothing: not installed, off, or in shadow mode."""
    if obs.get("planner") == "active":
        raise Refused(
            "the planner is commanding the cars (its shadow mode is off): turn "
            "switch.solar_ev_planner_shadow_mode on first, so you are the only writer"
        )


def near(obs: Mapping[str, Any], amps: float, tol: float = 1.5) -> bool:
    a = _num(obs.get("wall_a"))
    return a is not None and abs(a - amps) <= tol


def drawing(obs: Mapping[str, Any]) -> bool:
    a = _num(obs.get("wall_a"))
    return a is not None and a > 1.0


def stopped(obs: Mapping[str, Any]) -> bool:
    a = _num(obs.get("wall_a"))
    return a is not None and a <= 0.5


# --------------------------------------------------------------------------- Tesla


async def tesla_test(s: Session, steps: Sequence[int] = TESLA_STEPS, settle_s: float = 120.0):
    car = s.car
    for key in ("switch", "current"):
        if not car.get(key):
            raise Refused(f"cars.{s.car_id}.{key} is not mapped")
    obs = await s.observe()
    s.record("preflight", "observe", obs=obs)
    for key in ("switch", "current"):
        if obs.get(key) in UNAVAILABLE:
            raise Refused(f"{car[key]} is missing or unavailable: is Tesla Fleet set up?")
    _on_charger(obs, s.car_id)
    _planner_quiet(obs)
    lo, hi = _num(obs.get("current_min")) or 0, _num(obs.get("current_max")) or 48
    plan = [int(min(hi, max(lo, a))) for a in steps]
    original_a = _num(obs.get("current"))
    original_on = obs.get("switch") == "on"
    s.console.say(
        f"The {car.get('name') or s.car_id} is on the charger at {obs.get('soc')}% (limit "
        f"{obs.get('limit')}%), current set to {obs.get('current')} A, charging "
        f"{'on' if original_on else 'off'}. Steps: {' -> '.join(f'{a} A' for a in plan)}. "
        "Each command is billed by Tesla's API."
    )
    soc, limit = _num(obs.get("soc")), _num(obs.get("limit"))
    if soc is not None and limit is not None and soc >= limit - 1:
        s.console.say("  The car is at its limit and will not draw current.")
        if s.console.choose("  continue anyway? [y/N]", "n") != "y":
            raise Refused("the car is at its charge limit; raise it in the Tesla app first")
    try:
        if (
            not original_on
            and s.begin("start", "turn charging on, so current changes show")
            and await s.act("start", "switch", "turn_on", car["switch"])
        ):
            ok, lat, _ = await s.watch("start", "the car drawing current", drawing, settle_s)
            s.result("start", ok, "charging started" if ok else "no current drawn", lat)
        for i, amps in enumerate(plan, 1):
            step = f"step {i}: {amps} A"
            if not s.begin(step, f"set the charge current to {amps} A"):
                continue
            if not await s.act(step, "number", "set_value", car["current"], value=amps):
                s.result(step, None, "command not sent")
                continue
            ok, lat, last = await s.watch(
                step, f"Wall Connector near {amps} A", lambda o, a=amps: near(o, a), settle_s
            )
            s.result(
                step,
                ok,
                f"Wall Connector {last.get('wall_a')} A for {amps} A"
                + ("" if ok else f" (no match within {settle_s:.0f} s)"),
                lat if ok else None,
            )
    finally:
        await tesla_restore(s, original_a, original_on)


async def tesla_restore(s: Session, current: float | None, was_on: bool) -> None:
    """Put the current and the charge switch back as they were (asked, default yes)."""
    car = s.car
    try:
        obs = await s.observe()
    except HassError as err:
        s.console.say(f"  cannot read the car to restore it: {err}")
        obs = {}
    todo = []
    if current is not None and _num(obs.get("current")) != current:
        todo.append(("number", "set_value", car["current"], {"value": int(current)}))
    if (obs.get("switch") == "on") != was_on:
        todo.append(("switch", "turn_on" if was_on else "turn_off", car["switch"], {}))
    if not todo:
        s.result("restore", True, "already as it was")
        return
    s.console.say("")
    s.console.say("== restore: " + "; ".join(f"{d}.{v} {e} {x or ''}" for d, v, e, x in todo))
    try:
        ans = s.console.choose("  restore now? [Y/n]", "y")
    except KeyboardInterrupt:
        ans = "n"
    if ans != "y":
        s.result("restore", False, "not restored: set it back by hand")
        return
    for domain, service, eid, data in todo:
        await s.client.call_service(domain, service, data, target={"entity_id": eid})
        s.commands += 1
        s.record("restore", "sent", call=f"{domain}.{service} {eid} {data}")
    s.result("restore", True, f"current {current} A, charging {'on' if was_on else 'off'}")


# --------------------------------------------------------------------------- Rivian


def _control(obs: Mapping[str, Any]) -> dict[str, Any]:
    return obs.get("schedule_control") or {}


def _state(obs: Mapping[str, Any]) -> Any:
    return obs.get("control_state")


async def rivian_preflight(s: Session) -> dict[str, Any]:
    car = s.car
    for key in ("switch", "current", "control_state"):
        if not car.get(key):
            raise Refused(f"cars.{s.car_id}.{key} is not mapped")
    obs = await s.observe()
    s.record("preflight", "observe", obs=obs)
    state = _state(obs)
    if state in UNAVAILABLE:
        raise Refused(
            f"{car['control_state']} is missing or unavailable: is Rivian schedule control set up?"
        )
    a = _control(obs)
    if a.get("dry_run") or state == "dry_run":
        raise Refused(
            "the schedule control is in dry run: it writes nothing. Turn dry run off in its "
            "options for the trial (and back on afterwards if you like)"
        )
    if state == "fault":
        raise Refused(
            f"the schedule control is faulted ({a.get('fault_code')}): clear the fault first"
        )
    if state == "handed_back":
        raise Refused(
            "the schedule control is handed back to you until unplug: unplug and plug in again"
        )
    _on_charger(obs, s.car_id)
    _planner_quiet(obs)
    if obs.get("charger_status") == "a":
        raise Refused(
            "the schedule control's charger status is 'a': it does not see the car at home"
        )
    if (a.get("observation") or {}).get("controller_alive") is False:
        raise Refused(
            "the schedule control sees its controller as down (its controller_liveness_entity "
            "is unavailable), so it starts nothing: the planner's heartbeat must show 'shadow'"
        )
    return obs


async def rivian_trial(s: Session, only: Sequence[str] = RIVIAN_STEPS, settle_s: float = 300.0):
    car = s.car
    obs = await rivian_preflight(s)
    s.console.say(
        f"The {car.get('name') or s.car_id} is on the charger at {obs.get('soc')}% (limit "
        f"{obs.get('limit')}%). Schedule control state {_state(obs)}. Pause other schedule "
        "writers and do not edit the schedule in the Rivian app during the test."
    )
    ctx: dict[str, Any] = {"renewal_off": False}
    try:
        for step in RIVIAN_STEPS:
            if step in only:
                await RIVIAN_FUNCS[step](s, ctx, settle_s)
    finally:
        await rivian_cleanup(s, ctx)


async def _press(s: Session, step: str, key: str) -> bool:
    eid = s.car.get(key)
    if not eid:
        s.result(step, None, f"cars.{s.car_id}.{key} is not mapped")
        return False
    return await s.act(step, "button", "press", eid)


def _lease(amps: int) -> Callable[[dict[str, Any]], bool]:
    return lambda o: _state(o) == "lease_active" and _control(o).get("lease_amps") == amps


async def r_inventory(s: Session, ctx: dict[str, Any], settle_s: float) -> None:
    step = "inventory"
    if not s.begin(step, "press Read schedules and note the owner's schedule list"):
        return
    before = _control(await s.observe()).get("last_provider_read_at")
    if not await _press(s, step, "read_schedules_button"):
        return
    ok, lat, obs = await s.watch(
        step,
        "a new schedule read",
        lambda o: _control(o).get("last_provider_read_at") not in (None, before),
        120,
    )
    count = _control(obs).get("provider_schedule_count")
    ctx["inventory_count"] = count
    s.record(step, "inventory", summary=obs.get("last_provider_read"), count=count)
    s.result(step, ok, f"{count} schedule(s) on the vehicle", lat)


async def r_t1(s: Session, ctx: dict[str, Any], settle_s: float) -> None:
    step = "T1 write and readback"
    if not s.begin(step, "16 A, on, off, then Restore owner schedule, with no charging possible"):
        return
    obs = await s.observe()
    s.console.say(
        f"  In the Rivian app, set the charge limit at or below the current level "
        f"({obs.get('soc')}%) so no session can start."
    )
    s.console.text("  Press Enter when done.")
    car = s.car
    if not await s.act(step, "number", "set_value", car["current"], value=16):
        return s.result(step, None, "command not sent")
    if not await s.act(step, "switch", "turn_on", car["switch"]):
        return s.result(step, None, "command not sent")
    ok1, lat1, _ = await s.watch(step, "lease_active at 16 A", _lease(16), settle_s)
    if not await s.act(step, "switch", "turn_off", car["switch"]):
        return s.result(step, ok1, "stopped before the pause", lat1)
    ok2, lat2, _ = await s.watch(step, "paused", lambda o: _state(o) == "paused", settle_s)
    if not await _press(s, step, "restore_button"):
        return s.result(step, ok1 and ok2, "stopped before the restore")
    ok3, lat3, obs = await s.watch(
        step, "handed_back", lambda o: _state(o) == "handed_back", settle_s
    )
    seen = s.console.text("  What did the Rivian app show at each change? (Enter to skip)")
    s.record(step, "owner_note", note=seen)
    fault = _control(obs).get("fault_code")
    s.result(
        step,
        ok1 and ok2 and ok3 and not fault,
        f"lease {lat1:.0f} s, pause {lat2:.0f} s, restore {lat3:.0f} s"
        + (f"; fault {fault}" if fault else ""),
    )
    s.console.say(
        "  Now restore the charge limit in the app. Then unplug, wait at least the release "
        "delay (60 s), and plug in again: the schedule control ignores requests until unplug."
    )
    s.console.text("  Press Enter when the car is plugged in again.")
    ok, lat, _ = await s.watch(
        step,
        "the schedule control leaving handed_back",
        lambda o: _state(o) != "handed_back",
        600,
    )
    s.result("replug", ok, "control returned" if ok else "still handed_back", lat)


async def r_t2(s: Session, ctx: dict[str, Any], settle_s: float) -> None:
    step = "T2 amps mid-session"
    if not s.begin(step, "charge at 16 A, then 10 A, then 20 A (the increase waits 2 min)"):
        return
    car = s.car
    for amps in (16, 10, 20):
        sub = f"T2 {amps} A"
        if not await s.act(sub, "number", "set_value", car["current"], value=amps):
            return s.result(step, None, "command not sent")
        if amps == 16 and not await s.act(sub, "switch", "turn_on", car["switch"]):
            return s.result(step, None, "command not sent")
        wait = settle_s + (150 if amps == 20 else 0)  # the schedule control's increase interval
        ok, lat, obs = await s.watch(
            sub,
            f"lease at {amps} A and the car near {amps} A",
            lambda o, a=amps: _lease(a)(o) and near(o, a),
            wait,
        )
        s.result(
            sub, ok, f"lease {_control(obs).get('lease_amps')} A, car {obs.get('wall_a')} A", lat
        )


async def r_t2b(s: Session, ctx: dict[str, Any], settle_s: float) -> None:
    step = "T2b continuity"
    minutes = 26.0
    if not s.begin(step, f"keep charging for {minutes:.0f} min (two lease renewals)"):
        return
    renewals, gaps, last = 0, 0, None
    t0 = s.clock()
    while s.clock() - t0 < minutes * 60:
        obs = await s.observe()
        s.record(step, "sample", obs=obs)
        written = _control(obs).get("lease_written_at")
        if last is not None and written != last:
            renewals += 1
        last = written
        if not drawing(obs) or obs.get("contactor") != "on":
            gaps += 1
        await s.sleep(max(s.poll_s, 15.0))
    s.result(
        step, gaps == 0 and renewals >= 2, f"{renewals} renewals, {gaps} samples without current"
    )


async def r_t3(s: Session, ctx: dict[str, Any], settle_s: float) -> None:
    step = "T3 window off now"
    if not s.begin(step, "turn Solar charging off; charging should stop"):
        return
    if not await s.act(step, "switch", "turn_off", s.car["switch"]):
        return s.result(step, None, "command not sent")
    ok, lat, obs = await s.watch(
        step, "the car stopping", lambda o: stopped(o) and _state(o) == "paused", settle_s
    )
    s.result(step, ok, f"car {obs.get('wall_a')} A, state {_state(obs)}", lat)


async def r_t4(s: Session, ctx: dict[str, Any], settle_s: float) -> None:
    step = "T4 lease expiry"
    if not s.begin(step, "charge, then turn lease renewal off and wait for the window to end"):
        return
    car = s.car
    if not car.get("lease_renewal"):
        return s.result(step, None, f"cars.{s.car_id}.lease_renewal is not mapped")
    if not await s.act(step, "switch", "turn_on", car["switch"]):
        return s.result(step, None, "command not sent")
    ok, _, obs = await s.watch(
        step,
        "charging under a lease",
        lambda o: drawing(o) and _state(o) == "lease_active",
        settle_s,
    )
    if not ok:
        return s.result(step, False, "never charged under a lease")
    if not await s.act(step, "switch", "turn_off", car["lease_renewal"]):
        return s.result(step, None, "command not sent")
    ctx["renewal_off"] = True
    written = _control(obs).get("last_write_at")
    expires = _control(obs).get("lease_expires_at")
    wait = 30 * 60.0
    if expires:
        try:
            left = (datetime.fromisoformat(expires) - datetime.now(UTC)).total_seconds()
            wait = max(60.0, left + 180.0)
        except ValueError:
            pass
    ok, lat, obs = await s.watch(
        step,
        "the window ending on its own",
        lambda o: _state(o) == "lease_lapsed" and stopped(o),
        wait,
    )
    unchanged = _control(obs).get("last_write_at") == written
    s.result(
        step,
        ok and unchanged,
        f"state {_state(obs)}, car {obs.get('wall_a')} A, "
        + ("no write" if unchanged else "the schedule control wrote meanwhile"),
        lat,
    )
    ok, lat, _ = await s.watch(
        step, "the lapsed window parked", lambda o: _state(o) == "paused", 240
    )
    s.result("T4 park", ok, "parked" if ok else "not parked", lat)


async def r_t5(s: Session, ctx: dict[str, Any], settle_s: float) -> None:
    step = "T5 window back on"
    if not s.begin(step, "turn lease renewal back on; charging should resume"):
        return
    eid = s.car.get("lease_renewal")
    if not eid:
        return s.result(step, None, f"cars.{s.car_id}.lease_renewal is not mapped")
    obs = await s.observe()
    if obs.get("switch") != "on":
        s.console.say("  Solar charging is off: it must be on for a new lease.")
        if not await s.act(step, "switch", "turn_on", s.car["switch"]):
            return s.result(step, None, "command not sent")
    if not await s.act(step, "switch", "turn_on", eid):
        return s.result(step, None, "command not sent")
    ctx["renewal_off"] = False
    ok, lat, obs = await s.watch(
        step, "charging again", lambda o: drawing(o) and _state(o) == "lease_active", settle_s
    )
    s.result(step, ok, f"car {obs.get('wall_a')} A, state {_state(obs)}", lat)


async def r_t6(s: Session, ctx: dict[str, Any], settle_s: float) -> None:
    step = "T6 plug in while parked"
    if not s.begin(step, "park the window, replug within the release delay, try Charge now"):
        return
    if (await s.observe()).get("switch") == "on" and not await s.act(
        step, "switch", "turn_off", s.car["switch"]
    ):
        return s.result(step, None, "command not sent")
    await s.watch(step, "parked", lambda o: _state(o) == "paused", settle_s)
    s.console.text("  Unplug and plug in again within 60 s. Press Enter when plugged in.")
    ok, _, obs = await s.watch(step, "the car waiting", stopped, 120)
    waited = s.console.choose("  Did the car wait after replugging? [y/n]", "n") == "y"
    note = s.console.text("  Now try Charge now in the Rivian app. What happened?")
    s.record(step, "owner_note", note=note)
    ok2, _, obs = await s.watch(
        step, "a fault (external_edit)", lambda o: _state(o) == "fault", 120
    )
    fault = _control(obs).get("fault_code") if ok2 else None
    hint = f"; schedule control fault {fault}: clear it with the Clear fault button"
    s.result(
        step,
        waited,
        f"car waited: {'yes' if waited else 'no'}; charge now: {note or '-'}"
        + (hint if fault else ""),
    )


async def r_t7(s: Session, ctx: dict[str, Any], settle_s: float) -> None:
    step = "T7 exact restore"
    if not s.begin(step, "Restore owner schedule, then Read schedules and compare"):
        return
    if not await _press(s, step, "restore_button"):
        return
    ok, lat, _ = await s.watch(step, "handed_back", lambda o: _state(o) == "handed_back", settle_s)
    before = _control(await s.observe()).get("last_provider_read_at")
    if await _press(s, step, "read_schedules_button"):
        _, _, obs = await s.watch(
            step,
            "a new schedule read",
            lambda o: _control(o).get("last_provider_read_at") not in (None, before),
            120,
        )
        count = _control(obs).get("provider_schedule_count")
        s.record(step, "inventory", summary=obs.get("last_provider_read"), count=count)
        same = ctx.get("inventory_count") is None or count == ctx.get("inventory_count")
        app = s.console.choose("  Does the Rivian app show your own schedules again? [y/n]", "n")
        s.result(
            step,
            ok and same and app == "y",
            f"{count} schedule(s), inventory had {ctx.get('inventory_count', '?')}",
            lat,
        )


RIVIAN_FUNCS: dict[str, Callable[[Session, dict[str, Any], float], Awaitable[None]]] = {
    "inventory": r_inventory,
    "t1": r_t1,
    "t2": r_t2,
    "t2b": r_t2b,
    "t3": r_t3,
    "t4": r_t4,
    "t5": r_t5,
    "t6": r_t6,
    "t7": r_t7,
}


async def rivian_cleanup(s: Session, ctx: dict[str, Any]) -> None:
    """Lease renewal back on; offer to stop charging and restore the owner's schedule."""
    car = s.car
    try:
        obs = await s.observe()
    except HassError as err:
        s.console.say(f"  cannot read the schedule control to clean up: {err}")
        return
    todo = []
    if car.get("lease_renewal") and (ctx.get("renewal_off") or obs.get("lease_renewal") == "off"):
        todo.append(("switch", "turn_on", car["lease_renewal"]))
    if obs.get("switch") == "on":
        todo.append(("switch", "turn_off", car["switch"]))
    if _state(obs) not in ("handed_back", "idle", "waiting_for_vehicle") and car.get(
        "restore_button"
    ):
        todo.append(("button", "press", car["restore_button"]))
    if not todo:
        return
    s.console.say("")
    s.console.say("== clean up: " + "; ".join(f"{d}.{v} {e}" for d, v, e in todo))
    try:
        ans = s.console.choose("  do it now? [Y/n]", "y")
    except KeyboardInterrupt:
        ans = "n"
    if ans != "y":
        s.result(
            "clean up",
            False,
            "left as is: restore the owner's schedule with the Restore owner schedule button",
        )
        return
    for domain, service, eid in todo:
        await s.client.call_service(domain, service, {}, target={"entity_id": eid})
        s.commands += 1
        s.record("clean up", "sent", call=f"{domain}.{service} {eid}")
    s.result("clean up", True, "renewal on, charging off, owner schedule restored")


# --------------------------------------------------------------------------- CLI


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("car", help="the car id in the config (tesla or rivian)")
    ap.add_argument(
        "--i-am-at-the-car",
        dest="present",
        action="store_true",
        help="required: you are at the car and the Wall Connector",
    )
    ap.add_argument("--config", help="entity mapping (default: tools/config.json or the example)")
    ap.add_argument("--only", help=f"Rivian steps, comma separated ({','.join(RIVIAN_STEPS)})")
    ap.add_argument("--amps", help="Tesla steps, comma separated (default 16,10,6,16)")
    ap.add_argument("--settle", type=float, default=0.0, help="seconds to wait per step")
    ap.add_argument("--poll", type=float, default=5.0, help="seconds between readings")
    ap.add_argument("--log", type=Path, help="log file (default tools/out/attended_*.jsonl)")
    return ap


async def amain(
    argv: Sequence[str] | None = None,
    *,
    client: HassClient | None = None,
    console: Console | None = None,
    interactive: bool | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    args = parser().parse_args(argv)
    console = console or Console()
    if not args.present:
        console.say(
            "attended_test sends real commands to a car. Run it only while you are at the "
            "car, with --i-am-at-the-car."
        )
        return 2
    if not (interactive if interactive is not None else sys.stdin.isatty()):
        console.say("attended_test needs an interactive terminal: every step is confirmed.")
        return 2
    try:
        cfg = load_config(args.config)
    except ConfigError as err:
        console.say(f"attended_test: {err}")
        return 2
    if args.car not in cfg.cars:
        console.say(f"attended_test: no car {args.car!r} in the config ({', '.join(cfg.cars)})")
        return 2
    kind = cfg.cars[args.car].get("kind")
    own = client is None
    try:
        client = client or HassClient.from_env(allow_service_calls=True)
    except HassError as err:
        console.say(f"attended_test: {err}")
        return 2
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log = args.log or tool_config.OUT_DIR / f"attended_{args.car}_{stamp}.jsonl"
    s = Session(client, cfg, args.car, console, log, args.poll, sleep, clock)
    s.record("start", "begin", car=args.car, kind=kind, argv=list(argv or sys.argv[1:]))
    code = 0
    try:
        if kind == "tesla":
            amps = [int(a) for a in args.amps.split(",")] if args.amps else TESLA_STEPS
            await tesla_test(s, amps, args.settle or 120.0)
        else:
            only = (
                tuple(x.strip().lower() for x in args.only.split(","))
                if args.only
                else RIVIAN_STEPS
            )
            bad = [x for x in only if x not in RIVIAN_STEPS]
            if bad:
                console.say(f"attended_test: unknown steps {', '.join(bad)}")
                return 2
            await rivian_trial(s, only, args.settle or 300.0)
    except Refused as err:
        console.say(f"REFUSED: {err}. Nothing more was sent.")
        s.record("refused", "refused", reason=str(err))
        code = 3
    except Quit:
        console.say("Stopped by the owner.")
        s.record("quit", "quit")
        code = 1
    except HassError as err:
        console.say(f"attended_test: {err}")
        s.record("error", "error", error=str(err))
        code = 2
    finally:
        if own:
            await client.close()
    failed = [r for r in s.results if r["ok"] is False]
    console.say("")
    console.say(f"{len(s.results)} results, {len(failed)} failed, {s.commands} command(s) sent.")
    console.say(f"Log: {log}")
    s.record("end", "summary", results=s.results, commands=s.commands)
    return code or (1 if failed else 0)


def main() -> None:
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
