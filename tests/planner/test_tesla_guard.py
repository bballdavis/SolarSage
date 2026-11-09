"""The Tesla's billed-command guard: no start the car cannot act on, Tesla Fleet's "already
there" refusals counted as done, no more retries after failures in a row until the car
changes, and the hourly send count kept across a restart."""

from __future__ import annotations

from datetime import timedelta
import json
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest

from custom_components.solar_ev_planner.const import (
    CONF_CHARGING_STATE,
    FAILURE_BACKOFF_MAX,
    STALL_AFTER_FAILURES,
    STALL_BACKOFF,
)
from custom_components.solar_ev_planner.executor import CarExecutor, CarRuntime, refusal

from .conftest import RIV, RIVIAN, TES, TESLA, FakeCars, set_rivian, set_tesla

CHARGING = "sensor.tesla_charging"
CAR: dict[str, Any] = TESLA | {CONF_CHARGING_STATE: CHARGING}
ON = [("number", "set_value", 16), ("switch", "turn_on", TES["switch"])]


@pytest.fixture
def cars(planner_hass: HomeAssistant) -> FakeCars:
    """The Tesla plugged in at 63 % of a 90 % limit, not charging, at 32 A."""
    set_tesla(planner_hass, plugged="on")
    planner_hass.states.async_set(CHARGING, "stopped")
    fake = FakeCars(planner_hass)
    fake.install()
    return fake


def refused(reason: str) -> HomeAssistantError:
    """Tesla Fleet's error for a command the car refused (``command_reason``)."""
    return HomeAssistantError(
        translation_domain="tesla_fleet",
        translation_key="command_reason",
        translation_placeholders={"reason": reason},
    )


def test_refusal_reasons() -> None:
    assert refusal(refused("complete")) == "complete"
    assert refusal(HomeAssistantError("Command was unsuccessful: is_charging")) == "is_charging"
    assert refusal(HomeAssistantError("the cloud is down")) is None
    assert refusal(TimeoutError()) is None


# --------------------------------------------------------------------------- no start


@pytest.mark.parametrize("state", ["complete", "disconnected", "no_power"])
async def test_no_start_while_the_charging_sensor_says_it_cannot(
    planner_hass: HomeAssistant, cars: FakeCars, state: str
) -> None:
    hass = planner_hass
    executor, rt, now = CarExecutor(hass), CarRuntime(), dt_util.now()
    hass.states.async_set(CHARGING, state)
    assert not await executor.async_apply(CAR, rt, 16, now)
    assert cars.calls == []  # not even the current
    assert rt.retry_a == 16 and state in (rt.blocked or "")
    assert rt.sends == [] and rt.last_sent is None  # the guard costs nothing
    # the car can start again: the waiting command goes out
    hass.states.async_set(CHARGING, "stopped")
    assert await executor.async_reconcile(CAR, rt, now + timedelta(minutes=1))
    assert cars.calls == ON


async def test_no_start_at_the_cars_own_limit(planner_hass: HomeAssistant, cars: FakeCars) -> None:
    hass = planner_hass
    executor, rt, now = CarExecutor(hass), CarRuntime(), dt_util.now()
    set_tesla(hass, plugged="on", soc=90, limit=90)
    assert not await executor.async_apply(TESLA, rt, 16, now)  # without the charging sensor
    assert cars.calls == [] and "charge limit" in (rt.blocked or "")
    # a pause and a current change while charging are never held back
    set_tesla(hass, plugged="on", soc=90, limit=90, charging=True, amps=20)
    assert await executor.async_apply(TESLA, rt, 16, now)
    assert cars.calls == [("number", "set_value", 16)]
    assert await executor.async_apply(TESLA, rt, 0, now + timedelta(minutes=1))
    assert cars.calls[-1] == ("switch", "turn_off", TES["switch"])


async def test_a_rivian_is_never_held_back(planner_hass: HomeAssistant, cars: FakeCars) -> None:
    hass = planner_hass
    set_rivian(hass, soc=85, limit=85)
    rt = CarRuntime()
    assert await CarExecutor(hass).async_apply(RIVIAN, rt, 16, dt_util.now())
    assert cars.calls == [("number", "set_value", 16), ("switch", "turn_on", RIV["switch"])]


# --------------------------------------------------------------------------- refusals


async def test_complete_is_done_and_holds_the_start_until_the_car_changes(
    planner_hass: HomeAssistant, cars: FakeCars
) -> None:
    """Tesla often stops just under its limit and answers a start with "complete"."""
    hass = planner_hass
    executor, rt, now = CarExecutor(hass), CarRuntime(), dt_util.now()
    set_tesla(hass, plugged="on", soc=89, limit=90)
    cars.refuse["turn_on"] = refused("complete")
    assert await executor.async_apply(TESLA, rt, 16, now)
    assert rt.failures == 0 and rt.retry_at is None and rt.last_error is None
    assert rt.commanded_a == 16 and rt.held == ["on", 89.0, 90.0]
    cars.clear()
    # a new command an hour later: no start while plug, SoC and limit are unchanged
    later = now + timedelta(hours=1)
    assert not await executor.async_apply(TESLA, rt, 20, later)
    assert cars.calls == [] and "complete" in (rt.blocked or "")
    assert not await executor.async_reconcile(TESLA, rt, later + timedelta(minutes=1))
    assert cars.calls == []
    # the owner raises the limit: the start goes out once
    cars.refuse.clear()
    set_tesla(hass, plugged="on", soc=89, limit=95, amps=16)
    assert await executor.async_reconcile(TESLA, rt, later + timedelta(minutes=2))
    assert cars.calls == [("number", "set_value", 20), ("switch", "turn_on", TES["switch"])]
    assert rt.held is None


@pytest.mark.parametrize(
    ("charging", "target", "service", "reason"),
    [(False, 16, "turn_on", "is_charging"), (True, 0, "turn_off", "not_charging")],
)
async def test_already_there_is_done(
    planner_hass: HomeAssistant,
    cars: FakeCars,
    charging: bool,
    target: int,
    service: str,
    reason: str,
) -> None:
    hass = planner_hass
    set_tesla(hass, plugged="on", charging=charging, amps=16)
    cars.refuse[service] = HomeAssistantError(f"Command was unsuccessful: {reason}")
    rt = CarRuntime()
    assert await CarExecutor(hass).async_apply(TESLA, rt, target, dt_util.now())
    assert rt.failures == 0 and rt.commanded_a == target and rt.held is None


async def test_other_refusals_still_fail(planner_hass: HomeAssistant, cars: FakeCars) -> None:
    cars.refuse["turn_on"] = refused("vehicle_unavailable")
    rt = CarRuntime()
    assert not await CarExecutor(planner_hass).async_apply(TESLA, rt, 16, dt_util.now())
    assert rt.failures == 1 and rt.retry_a == 16


# --------------------------------------------------------------------------- failures


async def test_failures_in_a_row_stop_the_retries_until_the_car_changes(
    planner_hass: HomeAssistant, cars: FakeCars
) -> None:
    hass = planner_hass
    executor, rt, now = CarExecutor(hass), CarRuntime(), dt_util.now()
    cars.fail = "the car does not answer"
    assert not await executor.async_apply(TESLA, rt, 16, now)
    for n in range(2, STALL_AFTER_FAILURES + 1):
        assert rt.retry_at is not None and rt.retry_at - now <= FAILURE_BACKOFF_MAX
        assert not await executor.async_reconcile(TESLA, rt, rt.retry_at)
        assert rt.failures == n
    assert len(cars.calls) == STALL_AFTER_FAILURES  # each try failed at the current
    assert rt.stalled == ["on", 63.0, 90.0]
    assert rt.retry_at is not None and rt.last_sent is not None
    assert rt.retry_at - rt.last_sent == STALL_BACKOFF
    sent, last = len(cars.calls), rt.last_sent
    # the 15-minute back-off is long over: still nothing, every minute for two hours
    for m in range(1, 120):
        await executor.async_reconcile(TESLA, rt, last + timedelta(minutes=m))
    assert len(cars.calls) == sent
    assert "plug, SoC or limit" in (rt.blocked or "")
    # Tesla Fleet unavailable for a while: no change, no try
    hass.states.async_set(TES["soc"], "unavailable")
    await executor.async_reconcile(TESLA, rt, last + timedelta(minutes=120))
    assert len(cars.calls) == sent
    # the SoC moves: one more try at once
    cars.fail = None
    set_tesla(hass, plugged="on", soc=62)
    assert await executor.async_reconcile(TESLA, rt, last + timedelta(minutes=121))
    assert rt.failures == 0 and rt.stalled is None and rt.retry_at is None


async def test_a_stalled_tesla_tries_again_after_the_long_back_off(
    planner_hass: HomeAssistant, cars: FakeCars
) -> None:
    executor, now = CarExecutor(planner_hass), dt_util.now()
    rt = CarRuntime(
        retry_a=16,
        failures=STALL_AFTER_FAILURES,
        retry_at=now + STALL_BACKOFF,
        stalled=["on", 63.0, 90.0],
    )
    assert not await executor.async_reconcile(TESLA, rt, now + STALL_BACKOFF - timedelta(minutes=1))
    assert cars.calls == []
    assert await executor.async_reconcile(TESLA, rt, now + STALL_BACKOFF)
    assert cars.calls == ON


async def test_a_pause_is_never_held_back_by_a_stall(
    planner_hass: HomeAssistant, cars: FakeCars
) -> None:
    hass = planner_hass
    set_tesla(hass, plugged="on", charging=True)
    executor, now = CarExecutor(hass), dt_util.now()
    rt = CarRuntime(
        retry_a=16,
        failures=STALL_AFTER_FAILURES,
        retry_at=now + STALL_BACKOFF,
        stalled=["on", 63.0, 90.0],
    )
    # the free hours are over: the stop goes out at once, not in three hours
    assert await executor.async_apply(TESLA, rt, 0, now + timedelta(minutes=1))
    assert cars.calls == [("switch", "turn_off", TES["switch"])]
    assert rt.stalled is None and rt.commanded_a == 0
    # a failing stop keeps the short back-off and never stalls
    cars.calls.clear()
    set_tesla(hass, plugged="on", charging=True)
    cars.fail = "the car does not answer"
    t = now + timedelta(minutes=2)
    rt.failures = STALL_AFTER_FAILURES
    assert not await executor.async_apply(TESLA, rt, 0, t)
    assert rt.stalled is None and rt.retry_at is not None
    assert rt.retry_at - t <= FAILURE_BACKOFF_MAX


async def test_a_rivian_keeps_its_short_back_off(
    planner_hass: HomeAssistant, cars: FakeCars
) -> None:
    hass = planner_hass
    set_rivian(hass)
    executor, rt, t = CarExecutor(hass), CarRuntime(), dt_util.now()
    cars.fail = "the cloud is down"
    assert not await executor.async_apply(RIVIAN, rt, 16, t)
    for _ in range(STALL_AFTER_FAILURES + 3):
        assert rt.retry_at is not None
        t = rt.retry_at
        await executor.async_reconcile(RIVIAN, rt, t)
    assert rt.stalled is None and rt.retry_at - t <= FAILURE_BACKOFF_MAX


# --------------------------------------------------------------------------- persistence


def test_the_hourly_sends_and_the_guards_survive_a_restart() -> None:
    now = dt_util.now()
    rt = CarRuntime(
        sends=[now - timedelta(minutes=m) for m in (5, 30)],
        held=["on", 89.0, 90.0],
        stalled=["on", 63.0, 90.0],
    )
    back = CarRuntime.from_dict(json.loads(json.dumps(rt.to_dict())))
    assert back.sends == rt.sends
    assert back.held == ["on", 89.0, 90.0] and back.stalled == ["on", 63.0, 90.0]
    assert CarRuntime.from_dict({"sends": ["nonsense", None]}).sends == []
    back.end_session()
    assert back.held is None and back.stalled is None
    assert back.sends == rt.sends  # the cap counts across sessions
