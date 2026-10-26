"""Commanding the car on the charger: nothing in shadow mode, the right service calls with
the rate limits once shadow mode is off, the requests Rivian schedule control dropped, the
Tesla restore at unplug, failures, and the free hours' end published before the commands it
belongs to."""

from __future__ import annotations

from datetime import datetime, timedelta
import json

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
import pytest

from custom_components.solar_ev_planner.const import MAX_SENDS_PER_HOUR
from custom_components.solar_ev_planner.executor import (
    CarExecutor,
    CarRuntime,
    Standing,
    calls_for,
)

from .conftest import (
    RIV,
    RIVIAN,
    TES,
    TESLA,
    FakeCars,
    cycle,
    make_entry,
    set_rivian,
    set_site,
    set_tesla,
    setup_entry,
    turn,
)

SHADOW = "switch.solar_ev_planner_shadow_mode"
PLANNING = "switch.solar_ev_planner_planning"
UNTIL = "sensor.r1s_planner_charging_until"
# 06:00 on Tuesday 16 June (UTC-7): the end of Monday night's free hours (20:00-06:00)
MONDAY_NIGHT_END = datetime(2026, 6, 16, 13, 0, tzinfo=dt_util.UTC)


@pytest.fixture(autouse=True)
def _ha(site: HomeAssistant) -> None:
    """Noon on a sunny Monday with the Rivian plugged in (conftest.site)."""


def rivian_calls(cars: FakeCars) -> list[tuple[str, str, object]]:
    return [c for c in cars.calls if c[2] in (RIV["switch"],) or c[1] == "set_value"]


# --------------------------------------------------------------------------- shadow mode


async def test_shadow_mode_plans_but_sends_nothing(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    assert hass.states.get(SHADOW).state == "on"  # the default
    for _ in range(5):
        await cycle(hass, coordinator, freezer, minutes=5)
    assert coordinator.data.decision.shadow
    assert int(hass.states.get("sensor.r1s_planner_planned_current").state) >= 8
    # the night: still nothing
    set_site(hass, pv_w=0, grid_w=900, battery_w=0)
    await cycle(hass, coordinator, freezer, minutes=10 * 60)
    assert coordinator.data.mode == "night"
    # the Tesla plugs in instead: nothing either
    set_rivian(hass, plugged="off")
    set_tesla(hass, plugged="on")
    await cycle(hass, coordinator, freezer)
    assert coordinator.data.identification.car_id == "tesla"
    set_tesla(hass, plugged="off")
    await cycle(hass, coordinator, freezer)
    assert fake_cars.calls == []
    assert hass.states.get("binary_sensor.r1s_planner_charging_wanted").state == "unknown"


# --------------------------------------------------------------------------- the Rivian


async def test_rivian_gets_the_current_then_the_switch(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    await turn(hass, SHADOW, False)
    amps = coordinator.runtime["rivian"].commanded_a
    assert amps is not None and amps >= 8 and amps % 2 == 0
    assert fake_cars.calls == [
        ("number", "set_value", amps),
        ("switch", "turn_on", RIV["switch"]),
    ]
    control = hass.states.get(RIV["control_state"])
    assert control.attributes["desired_charge"] is True
    assert control.attributes["requested_amps"] == amps
    assert hass.states.get("binary_sensor.r1s_planner_charging_wanted").state == "on"
    assert hass.states.get("sensor.solar_ev_planner_heartbeat").state == "active"
    # the Tesla is not on the charger: never commanded
    assert not [c for c in fake_cars.calls if c[2] in (TES["switch"], TES["current"])]

    # minutes later nothing changed: nothing is sent (an unchanged request is not repeated)
    fake_cars.clear()
    for _ in range(3):
        await cycle(hass, coordinator, freezer)
    assert fake_cars.calls == []


async def test_changes_wait_for_the_minimum_interval(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN), fake_cars)
    await coordinator.async_set_setting("max_step_a", 2)
    await turn(hass, SHADOW, False)
    first = coordinator.runtime["rivian"].commanded_a
    assert first is not None and first > 10
    fake_cars.clear()
    # five minutes later Solcast's new poll says a dull afternoon and the battery is under
    # the floor: the plan wants much less, but the last change was five minutes ago (minimum 15
    # minutes between changes)
    freezer.tick(timedelta(minutes=5))
    set_site(hass, soc=25, pv_w=1500, grid_w=0, battery_w=-1500, forecast_ratio=0.3)
    await cycle(hass, coordinator, freezer, minutes=0)
    assert fake_cars.calls == []
    reason = hass.states.get("sensor.solar_ev_planner_reason").state
    assert reason.startswith(f"Holding {first} A until 12:15")
    want = coordinator.data.cars["rivian"].want_a
    assert want is not None and want < first
    nxt = hass.states.get("sensor.solar_ev_planner_next_change")
    assert nxt.attributes["amps"] == want
    # after the interval: a pause at once (the switch, never 0 A)
    assert want == 0
    await cycle(hass, coordinator, freezer, minutes=11)
    assert fake_cars.calls == [("switch", "turn_off", RIV["switch"])]
    assert hass.states.get("binary_sensor.r1s_planner_charging_wanted").state == "off"


async def test_a_dropped_request_is_sent_again(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN), fake_cars)
    await turn(hass, SHADOW, False)
    amps = coordinator.runtime["rivian"].commanded_a
    fake_cars.clear()
    # the schedule control dropped the request (a restart it could not confirm): switch off,
    # no request
    set_rivian(hass, desired=None, requested=amps, schedule_control_state="idle")
    await cycle(hass, coordinator, freezer)
    assert fake_cars.calls == [("switch", "turn_on", RIV["switch"])]
    # handed back to the owner: requests are ignored until unplug, so none is repeated
    fake_cars.clear()
    set_rivian(hass, desired=None, requested=amps, schedule_control_state="handed_back")
    for _ in range(3):
        await cycle(hass, coordinator, freezer)
    assert fake_cars.calls == []


async def test_night_charging_at_the_cars_maximum(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    set_rivian(hass, plugged="off")
    coordinator = await setup_entry(hass, make_entry(RIVIAN), fake_cars)
    await turn(hass, SHADOW, False)
    assert fake_cars.calls == []  # the charger is empty
    set_site(hass, pv_w=0, grid_w=900, battery_w=0, soc=60)
    await cycle(hass, coordinator, freezer, minutes=10 * 60 + 30)  # 22:30
    set_rivian(hass, soc=50)
    await cycle(hass, coordinator, freezer)
    assert coordinator.data.mode == "night"
    assert fake_cars.calls == [("number", "set_value", 48), ("switch", "turn_on", RIV["switch"])]


async def test_free_hours_start_the_rivian_at_its_full_current(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    set_rivian(hass, plugged="off")
    coordinator = await setup_entry(hass, make_entry(RIVIAN), fake_cars)
    await turn(hass, SHADOW, False)
    set_site(hass, pv_w=0, grid_w=900, battery_w=0, soc=60)
    await cycle(hass, coordinator, freezer, minutes=8 * 60 + 30)  # 20:30, free hours
    set_rivian(hass, soc=70)
    await cycle(hass, coordinator, freezer)
    assert coordinator.data.mode == "night"
    assert fake_cars.calls == [
        ("number", "set_value", 48),
        ("switch", "turn_on", RIV["switch"]),
    ]
    assert hass.states.get("binary_sensor.r1s_planner_charging_wanted").state == "on"


# --------------------------------------------------------------------------- the Tesla


async def test_tesla_is_commanded_and_restored_at_unplug(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    set_site(hass, soc=45)  # less spare battery than the Tesla's maximum would take
    set_rivian(hass, plugged="off")
    set_tesla(hass, plugged="on", charging=True, amps=48)
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    await turn(hass, SHADOW, False)
    amps = coordinator.runtime["tesla"].commanded_a
    assert amps is not None and 6 <= amps < 48
    # already charging: only the current is set (each Tesla command is billed)
    assert fake_cars.calls == [("number", "set_value", amps)]
    assert hass.states.get(TES["current"]).state == str(amps)
    fake_cars.clear()
    # unplugged at home: the owner's current comes back (the setting defaults to 48 A)
    set_tesla(hass, plugged="off", charging=False, amps=amps)
    await cycle(hass, coordinator, freezer)
    assert fake_cars.calls == [("number", "set_value", 48)]
    fake_cars.clear()
    await cycle(hass, coordinator, freezer)
    assert fake_cars.calls == []  # once


async def test_tesla_restore_can_be_turned_off_and_never_runs_in_shadow(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    set_rivian(hass, plugged="off")
    set_tesla(hass, plugged="on", charging=True, amps=48)
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    await turn(hass, SHADOW, False)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.roxxo_planner_restore_current_at_unplug", "value": 0},
        blocking=True,
    )
    fake_cars.clear()
    set_tesla(hass, plugged="off", amps=12)
    await cycle(hass, coordinator, freezer)
    assert fake_cars.calls == []


async def test_a_full_tesla_is_left_alone(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    set_rivian(hass, plugged="off")
    set_tesla(hass, plugged="on", soc=90, limit=90)
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    await turn(hass, SHADOW, False)
    await cycle(hass, coordinator, freezer)
    assert coordinator.data.mode == "full"
    assert fake_cars.calls == []


# --------------------------------------------------------------------------- failures, limits


async def test_a_failed_command_backs_off_and_is_retried(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN), fake_cars)
    fake_cars.fail = "the cloud is down"
    await turn(hass, SHADOW, False)
    rt = coordinator.runtime["rivian"]
    assert rt.commanded_a is None and rt.retry_a is not None and rt.failures == 1
    attrs = hass.states.get("sensor.r1s_planner_planned_current").attributes
    assert "the cloud is down" in attrs["last_error"]
    assert hass.states.get("binary_sensor.r1s_planner_charging_wanted").state == "unknown"
    fake_cars.fail = None
    fake_cars.clear()
    await cycle(hass, coordinator, freezer, minutes=0.5)  # inside the back-off
    assert fake_cars.calls == []
    await cycle(hass, coordinator, freezer, minutes=1)
    assert fake_cars.calls == [
        ("number", "set_value", rt.commanded_a),
        ("switch", "turn_on", RIV["switch"]),
    ]
    assert rt.failures == 0


async def test_planning_off_sends_nothing(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN), fake_cars)
    await turn(hass, PLANNING, False)
    await turn(hass, SHADOW, False)
    for _ in range(3):
        await cycle(hass, coordinator, freezer)
    assert coordinator.data.mode == "off"
    assert fake_cars.calls == []


def test_calls_for_a_target() -> None:
    assert calls_for(16, Standing(None, None)) == ["current", "on"]
    assert calls_for(16, Standing(True, 16)) == []
    assert calls_for(16, Standing(True, 20)) == ["current"]
    assert calls_for(16, Standing(False, 16)) == ["on"]
    assert calls_for(0, Standing(True, 16)) == ["off"]
    assert calls_for(0, Standing(False, 16)) == []
    assert calls_for(0, Standing(None, 16)) == ["off"]  # no request yet: a pause takes over


def test_executor_rate_limits(planner_hass: HomeAssistant) -> None:
    executor = CarExecutor(planner_hass)
    now = dt_util.now()
    rt = CarRuntime(last_sent=now - timedelta(seconds=30))
    assert executor.blocked(rt, now, 16) == "one command per minute"
    rt = CarRuntime(sends=[now - timedelta(minutes=m) for m in range(2, 2 + MAX_SENDS_PER_HOUR)])
    assert "commands in the last hour" in executor.blocked(rt, now, 16)
    assert executor.blocked(rt, now, 0) is None  # a pause is never capped
    rt.sends = [now - timedelta(minutes=61)] * MAX_SENDS_PER_HOUR
    assert executor.blocked(rt, now, 16) is None
    rt = CarRuntime(retry_at=now + timedelta(minutes=2))
    assert executor.blocked(rt, now, 0).startswith("waiting until")


def test_runtime_round_trip() -> None:
    now = dt_util.now()
    rt = CarRuntime(plugged_since=now, commanded_a=16, touched=True, last_sent=now, failures=2)
    back = CarRuntime.from_dict(rt.to_dict())
    assert back.plugged_since == now and back.commanded_a == 16 and back.touched
    assert back.failures == 2
    back.end_session()
    assert back.plugged_since is None and back.commanded_a is None


# --------------------------------------------------------------------------- the free hours' end


class WatchedCars(FakeCars):
    """The fake cars, noting what the R1S's until sensor showed as each call for the R1S
    arrived: Rivian schedule control reads it when the request changes, so the free hours'
    end must already be there."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self.until_at_call: list[tuple[str, datetime | None]] = []

    def clear(self) -> None:
        super().clear()
        self.until_at_call.clear()

    async def _handle(self, call: ServiceCall) -> None:
        if call.data["entity_id"] in (RIV["switch"], RIV["current"]):
            state = self.hass.states.get(UNTIL)
            assert state is not None
            until = None if state.state == STATE_UNKNOWN else dt_util.parse_datetime(state.state)
            self.until_at_call.append((call.service, until))
        await super()._handle(call)


@pytest.fixture
def watched(site: HomeAssistant) -> WatchedCars:
    return WatchedCars(site)


async def test_the_free_end_goes_out_before_the_command(
    site: HomeAssistant, watched: WatchedCars, freezer: FrozenDateTimeFactory
) -> None:
    """Plugged in during the free hours: their end is published, then the current and the
    switch are sent."""
    hass = site
    set_rivian(hass, plugged="off")
    coordinator = await setup_entry(hass, make_entry(RIVIAN), watched)
    await turn(hass, SHADOW, False)
    set_site(hass, pv_w=0, grid_w=900, battery_w=0, soc=60)
    await cycle(hass, coordinator, freezer, minutes=8 * 60 + 30)  # 20:30, the charger empty
    assert watched.calls == []
    set_rivian(hass, soc=70)
    await cycle(hass, coordinator, freezer)
    assert coordinator.data.mode == "night"
    assert watched.calls == [("number", "set_value", 48), ("switch", "turn_on", RIV["switch"])]
    assert watched.until_at_call == [
        ("set_value", MONDAY_NIGHT_END),
        ("turn_on", MONDAY_NIGHT_END),
    ]


async def test_the_free_end_goes_out_before_the_full_current(
    site: HomeAssistant, watched: WatchedCars, freezer: FrozenDateTimeFactory
) -> None:
    """Charging from the sun with no end; at 20:00 the end, then 48 A."""
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN), watched)
    await turn(hass, SHADOW, False)
    day = coordinator.runtime["rivian"].commanded_a
    assert day is not None and 0 < day < 48
    assert watched.until_at_call == [("set_value", None), ("turn_on", None)]
    watched.clear()
    set_site(hass, pv_w=0, grid_w=900, battery_w=0, soc=60)
    await cycle(hass, coordinator, freezer, minutes=8 * 60)  # 20:00
    assert coordinator.data.mode == "night"
    assert watched.calls == [("number", "set_value", 48)]  # the switch is already on
    assert watched.until_at_call == [("set_value", MONDAY_NIGHT_END)]


async def test_leaving_shadow_mode_in_the_free_hours_sends_the_end_first(
    site: HomeAssistant, watched: WatchedCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN), watched)
    set_site(hass, pv_w=0, grid_w=900, battery_w=0, soc=60)
    await cycle(hass, coordinator, freezer, minutes=9 * 60)  # 21:00, in shadow mode
    assert coordinator.data.mode == "night" and watched.calls == []
    await turn(hass, SHADOW, False)
    assert watched.until_at_call == [
        ("set_value", MONDAY_NIGHT_END),
        ("turn_on", MONDAY_NIGHT_END),
    ]


async def test_commands_sent_again_in_the_free_hours_follow_the_end(
    site: HomeAssistant, watched: WatchedCars, freezer: FrozenDateTimeFactory
) -> None:
    """A command retried after a failure, and a request the schedule control dropped, go out
    with the end already published."""
    hass = site
    set_rivian(hass, plugged="off")
    coordinator = await setup_entry(hass, make_entry(RIVIAN), watched)
    await turn(hass, SHADOW, False)
    set_site(hass, pv_w=0, grid_w=900, battery_w=0, soc=60)
    await cycle(hass, coordinator, freezer, minutes=8 * 60 + 30)  # 20:30
    watched.fail = "the cloud is down"
    set_rivian(hass, soc=70)
    await cycle(hass, coordinator, freezer)
    rt = coordinator.runtime["rivian"]
    assert rt.commanded_a is None and rt.retry_a == 48
    assert watched.until_at_call == [("set_value", MONDAY_NIGHT_END)]
    watched.fail = None
    watched.clear()
    await cycle(hass, coordinator, freezer, minutes=1.5)  # after the back-off
    assert rt.commanded_a == 48
    assert watched.until_at_call == [
        ("set_value", MONDAY_NIGHT_END),
        ("turn_on", MONDAY_NIGHT_END),
    ]
    # the schedule control dropped the request (a restart it could not confirm)
    watched.clear()
    set_rivian(hass, soc=70, desired=None, requested=48, schedule_control_state="idle")
    await cycle(hass, coordinator, freezer, minutes=10)
    assert watched.until_at_call == [("turn_on", MONDAY_NIGHT_END)]


def test_runtime_round_trip_keeps_the_free_end() -> None:
    """The free hours' end survives a restart (the store is JSON); a store from before it
    existed, or a value that is not a time, loads as none; the end of a session clears it."""
    now = dt_util.now()
    end = now.replace(hour=6, minute=0, second=0, microsecond=0) + timedelta(days=1)
    rt = CarRuntime(plugged_since=now, commanded_a=48, touched=True, until=end)
    back = CarRuntime.from_dict(json.loads(json.dumps(rt.to_dict())))
    assert back.until == end and back.until.utcoffset() == end.utcoffset()
    assert back.commanded_a == 48 and back.plugged_since == now
    assert CarRuntime.from_dict(CarRuntime(plugged_since=now).to_dict()).until is None
    older = {k: v for k, v in rt.to_dict().items() if k != "until"}
    assert CarRuntime.from_dict(older).until is None
    assert CarRuntime.from_dict(older).commanded_a == 48
    assert CarRuntime.from_dict(rt.to_dict() | {"until": "06:00 tomorrow"}).until is None
    back.end_session()
    assert back.until is None and back.plugged_since is None
