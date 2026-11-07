"""The Tesla spending guard: every billable call the planner causes is counted and priced per
calendar month and kept across restarts; the cap allows for Tesla Fleet's own polling; near
the cap only essential commands go out, then nothing; and with Tesla Fleet's polling off the
planner refreshes the Tesla itself while it is on the Wall Connector."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_ev_planner.const import (
    CONF_STATUS,
    DEFAULT_SETTINGS,
    S_TESLA_BUDGET,
    S_TESLA_POLLING,
    S_TESLA_REFRESH,
    TESLA_CREDIT,
    TESLA_ESSENTIAL_AT,
    TESLA_FRESH_BEFORE_SEND,
    TESLA_MARGIN,
    TESLA_PRICE_COMMAND,
    TESLA_PRICE_DATA,
    TESLA_PRICE_WAKE,
    TESLA_REFRESH_DATA,
    TESLA_WAKE_CHECKS,
    TESLA_WORST_SEND,
)
from custom_components.solar_ev_planner.executor import CarExecutor, CarRuntime
from custom_components.solar_ev_planner.spend import (
    LEVEL_ESSENTIAL,
    LEVEL_NORMAL,
    LEVEL_STOPPED,
    SpendCounts,
    TeslaSpend,
    month_key,
    next_month,
)

from .conftest import (
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

STATUS = "binary_sensor.roxxo_status"
CAR: dict[str, Any] = TESLA | {CONF_STATUS: STATUS}
SPEND = "sensor.solar_ev_planner_tesla_api_spend"
BUDGET = "number.solar_ev_planner_tesla_api_budget"
SHADOW = "switch.solar_ev_planner_shadow_mode"
NOV = datetime(2026, 11, 5, 12, 0, tzinfo=dt_util.get_default_time_zone())
# With the defaults and Tesla Fleet polling: min($5 budget, $10 - $1 margin - $5.20 polling)
CAP_POLLING = TESLA_CREDIT - TESLA_MARGIN - DEFAULT_SETTINGS[S_TESLA_POLLING]


def wakes_worth(usd: float) -> int:
    """The number of wakes that cost at least ``usd``."""
    return int(usd / TESLA_PRICE_WAKE) + 1


def make_spend(
    hass: HomeAssistant, car: dict[str, Any] = CAR, **settings: Any
) -> tuple[TeslaSpend, dict[str, Any]]:
    values = dict(DEFAULT_SETTINGS) | settings
    return TeslaSpend(hass, values, {car["car_id"]: car}), values


def tesla_fleet_entry(hass: HomeAssistant, polling: bool) -> MockConfigEntry:
    """Tesla Fleet's config entry, owning the Tesla's battery level, with its "Enable
    polling for updates" system option."""
    entry = MockConfigEntry(domain="tesla_fleet", pref_disable_polling=not polling)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    state = hass.states.get(TES["soc"])
    if state is not None:  # the registry gives a taken entity ID a suffix
        hass.states.async_remove(TES["soc"])
    created = registry.async_get_or_create(
        "sensor",
        "tesla_fleet",
        "vin-charge_state_battery_level",
        suggested_object_id=TES["soc"].split(".", 1)[1],
        config_entry=entry,
    )
    assert created.entity_id == TES["soc"]
    if state is not None:
        hass.states.async_set(TES["soc"], state.state, state.attributes)
    return entry


class Refreshes:
    """A stand-in for ``homeassistant.update_entity``: records the call and lets the test
    change what Tesla Fleet reads."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[str] = []
        self.then: Any = None  # called after each refresh

    def install(self) -> None:
        self.hass.services.async_register("homeassistant", "update_entity", self._handle)

    async def _handle(self, call: ServiceCall) -> None:
        ids = call.data["entity_id"]
        self.calls.extend([ids] if isinstance(ids, str) else ids)
        if self.then is not None:
            self.then()


@pytest.fixture
def cars(planner_hass: HomeAssistant) -> FakeCars:
    """The Tesla plugged in at 63 % of a 90 % limit, not charging, at 32 A."""
    set_tesla(planner_hass, plugged="on")
    fake = FakeCars(planner_hass)
    fake.install()
    return fake


@pytest.fixture
def refreshes(planner_hass: HomeAssistant) -> Refreshes:
    r = Refreshes(planner_hass)
    r.install()
    return r


# --------------------------------------------------------------------------- counting


def test_prices_and_the_worst_send() -> None:
    counts = SpendCounts("2026-11", commands=10, wakes=2, data=5, refreshes=1)
    assert counts.usd == pytest.approx(10 * 0.001 + 2 * 0.02 + 5 * 0.002)
    assert (TESLA_PRICE_COMMAND, TESLA_PRICE_DATA, TESLA_PRICE_WAKE) == (0.001, 0.002, 0.02)
    # one more send at most: two commands, a wake and its checks, a refresh
    assert round(TESLA_WORST_SEND, 6) == round(0.002 + 0.02 + 0.002 * 5, 6)


def test_months_roll_and_survive_a_restart(planner_hass: HomeAssistant) -> None:
    spend, _ = make_spend(planner_hass)
    spend.count_command(NOV)
    spend.count_wake(NOV)
    spend.refreshed["tesla"] = NOV
    back, _ = make_spend(planner_hass)
    back.load(json.loads(json.dumps(spend.to_dict())))
    assert back.counts == SpendCounts("2026-11", commands=1, wakes=1, data=TESLA_WAKE_CHECKS)
    assert back.refreshed == {"tesla": NOV}
    # the first of the next month: from zero, the last month kept
    december = next_month(NOV)
    assert december == NOV.replace(month=12, day=1, hour=0)
    assert back.spent(december) == 0.0
    assert back.counts.month == "2026-12" and back.last_month is not None
    assert back.last_month.commands == 1
    assert month_key(datetime(2026, 12, 31, 23, 59)) == "2026-12"
    assert next_month(datetime(2026, 12, 15)) == datetime(2027, 1, 1)
    # rubbish in the store: a fresh count
    back.load({"counts": {"month": "2026-11", "commands": "x", "wakes": -3}, "refreshed": {"a": 1}})
    assert back.counts == SpendCounts("2026-11") and back.refreshed == {}


async def test_the_cap_allows_for_tesla_fleet_polling(planner_hass: HomeAssistant) -> None:
    hass = planner_hass
    set_tesla(hass, plugged="on")
    spend, settings = make_spend(hass)
    # the battery level has no config entry: polling is assumed on
    assert spend.polling(CAR) is None
    assert spend.polling_estimate == DEFAULT_SETTINGS[S_TESLA_POLLING]
    assert spend.cap == pytest.approx(CAP_POLLING)
    assert spend.cap < DEFAULT_SETTINGS[S_TESLA_BUDGET]
    # Tesla Fleet polls: the same
    entry = tesla_fleet_entry(hass, polling=True)
    assert spend.polling(CAR) is True and spend.cap == pytest.approx(CAP_POLLING)
    # its polling turned off: the whole budget
    hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    assert spend.polling(CAR) is False and spend.polling_estimate == 0.0
    assert spend.cap == DEFAULT_SETTINGS[S_TESLA_BUDGET]
    # a budget above the credit less the margin is capped
    settings[S_TESLA_BUDGET] = 10.0
    assert spend.cap == TESLA_CREDIT - TESLA_MARGIN
    settings[S_TESLA_BUDGET] = 0.0
    assert spend.level(NOV) == LEVEL_STOPPED


def test_levels(planner_hass: HomeAssistant) -> None:
    spend, _ = make_spend(planner_hass)
    assert spend.level(NOV) == LEVEL_NORMAL and spend.note(NOV) is None
    spend.counts = SpendCounts(month_key(NOV), wakes=wakes_worth(TESLA_ESSENTIAL_AT * CAP_POLLING))
    assert spend.level(NOV) == LEVEL_ESSENTIAL
    assert "only started in the free hours" in (spend.note(NOV) or "")
    spend.counts = SpendCounts(month_key(NOV), wakes=wakes_worth(CAP_POLLING - TESLA_WORST_SEND))
    assert spend.level(NOV) == LEVEL_STOPPED
    assert "nothing is sent to the Tesla until December 1" in (spend.note(NOV) or "")
    # the next month starts again
    assert spend.level(next_month(NOV)) == LEVEL_NORMAL


async def test_every_call_is_counted_with_a_wake_unless_online(
    planner_hass: HomeAssistant, cars: FakeCars
) -> None:
    hass = planner_hass
    spend, _ = make_spend(hass)
    executor, rt = CarExecutor(hass, spend), CarRuntime()
    # no status reading: the send counts a wake (and its checks) and both calls
    assert await executor.async_apply(CAR, rt, 16, NOV)
    assert spend.counts.commands == 2 and spend.counts.wakes == 1
    assert spend.counts.data == TESLA_WAKE_CHECKS
    # online: no wake
    hass.states.async_set(STATUS, "on")
    assert await executor.async_apply(CAR, rt, 0, NOV + timedelta(minutes=1))
    assert spend.counts.commands == 3 and spend.counts.wakes == 1
    # a failed command is billed too
    cars.fail = "the car does not answer"
    set_tesla(hass, plugged="on")
    assert not await executor.async_apply(CAR, rt, 16, NOV + timedelta(minutes=2))
    assert spend.counts.commands == 4
    # what is not sent costs nothing: the car at its limit
    cars.fail = None
    set_tesla(hass, plugged="on", soc=90, limit=90)
    rt = CarRuntime()
    assert not await executor.async_apply(CAR, rt, 16, NOV + timedelta(minutes=20))
    assert spend.counts.commands == 4
    # a Rivian is never counted
    set_rivian(hass)
    assert await executor.async_apply(RIVIAN, CarRuntime(), 16, NOV)
    assert spend.counts.commands == 4 and spend.counts.wakes == 1


async def test_restore_at_unplug_is_counted_and_skipped_near_the_cap(
    planner_hass: HomeAssistant, cars: FakeCars
) -> None:
    hass = planner_hass
    spend, _ = make_spend(hass)
    executor = CarExecutor(hass, spend)
    set_tesla(hass, plugged="off", amps=16)
    assert await executor.async_restore(CAR, 48, NOV)
    assert spend.counts.commands == 1 and spend.counts.wakes == 1
    set_tesla(hass, plugged="off", amps=16)
    cars.clear()
    spend.counts.wakes = wakes_worth(TESLA_ESSENTIAL_AT * CAP_POLLING)
    assert not await executor.async_restore(CAR, 48, NOV)
    assert cars.calls == [] and spend.counts.commands == 1


# --------------------------------------------------------------------------- degrading


@pytest.fixture
def essential(planner_hass: HomeAssistant) -> TeslaSpend:
    spend, _ = make_spend(planner_hass)
    spend.counts = SpendCounts(month_key(NOV), wakes=wakes_worth(TESLA_ESSENTIAL_AT * CAP_POLLING))
    assert spend.level(NOV) == LEVEL_ESSENTIAL
    return spend


async def test_near_the_cap_daytime_starts_and_increases_wait(
    planner_hass: HomeAssistant, cars: FakeCars, essential: TeslaSpend
) -> None:
    hass = planner_hass
    executor, rt = CarExecutor(hass, essential), CarRuntime()
    before = essential.counts.usd
    assert not await executor.async_apply(CAR, rt, 16, NOV)  # a solar start
    assert cars.calls == [] and rt.retry_a == 16
    assert "nearly used" in (rt.blocked or "")
    set_tesla(hass, plugged="on", charging=True, amps=10)
    assert not await executor.async_apply(CAR, rt, 16, NOV)  # an increase
    assert not await executor.async_reconcile(CAR, rt, NOV + timedelta(minutes=5))
    assert cars.calls == [] and essential.counts.usd == before


async def test_near_the_cap_a_daytime_decrease_becomes_a_stop(
    planner_hass: HomeAssistant, cars: FakeCars, essential: TeslaSpend
) -> None:
    """The free hours' end, or the paid-import safety rule cutting the current: the car is
    stopped rather than left drawing paid power."""
    hass = planner_hass
    set_tesla(hass, plugged="on", charging=True, amps=48)
    executor, rt = CarExecutor(hass, essential), CarRuntime(commanded_a=48)
    assert await executor.async_apply(CAR, rt, 12, NOV)
    assert cars.calls == [("switch", "turn_off", TES["switch"])]
    assert rt.commanded_a == 0 and "stopped instead of 12 A" in (rt.blocked or "")
    # the stop the planner asks for goes out as well
    set_tesla(hass, plugged="on", charging=True, amps=48)
    cars.clear()
    rt = CarRuntime(commanded_a=48)
    assert await executor.async_apply(CAR, rt, 0, NOV + timedelta(minutes=2))
    assert cars.calls == [("switch", "turn_off", TES["switch"])] and rt.blocked is None


async def test_near_the_cap_the_free_hours_still_start(
    planner_hass: HomeAssistant, cars: FakeCars, essential: TeslaSpend
) -> None:
    executor, rt = CarExecutor(planner_hass, essential), CarRuntime()
    assert await executor.async_apply(CAR, rt, 48, NOV, free=True)
    assert cars.calls == [("number", "set_value", 48), ("switch", "turn_on", TES["switch"])]


async def test_at_the_cap_nothing_is_sent_until_next_month(
    planner_hass: HomeAssistant, cars: FakeCars
) -> None:
    hass = planner_hass
    spend, _ = make_spend(hass)
    spend.counts = SpendCounts(month_key(NOV), wakes=wakes_worth(CAP_POLLING - TESLA_WORST_SEND))
    executor, rt = CarExecutor(hass, spend), CarRuntime()
    assert not await executor.async_apply(CAR, rt, 48, NOV, free=True)
    assert cars.calls == [] and "used up" in (rt.blocked or "")
    assert spend.counts.usd < CAP_POLLING  # never past the cap
    # a stop to a charging Tesla still goes out: it needs no wake, and paid power costs more
    set_tesla(hass, plugged="on", charging=True, amps=48)
    assert await executor.async_apply(CAR, rt, 0, NOV + timedelta(minutes=1))
    assert cars.calls == [("switch", "turn_off", TES["switch"])]


# --------------------------------------------------------------------------- refreshes


async def test_refreshes_only_with_tesla_fleet_polling_off(
    planner_hass: HomeAssistant, refreshes: Refreshes
) -> None:
    hass = planner_hass
    set_tesla(hass, plugged="on")
    spend, settings = make_spend(hass)
    entry = tesla_fleet_entry(hass, polling=True)
    await spend.async_watch(NOV, False, {"tesla": False})
    await spend.async_watch(NOV, True, {"tesla": False})  # connected, but Tesla Fleet polls
    assert refreshes.calls == [] and spend.counts.data == 0
    hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    settings[S_TESLA_REFRESH] = 0  # the owner turned the planner's refreshes off
    await spend.async_watch(NOV, False, {"tesla": False})
    await spend.async_watch(NOV, True, {"tesla": False})
    assert refreshes.calls == []


async def test_refreshes_while_the_tesla_is_on_the_charger(
    planner_hass: HomeAssistant, refreshes: Refreshes
) -> None:
    hass = planner_hass
    set_tesla(hass, plugged="on")
    spend, settings = make_spend(hass)
    tesla_fleet_entry(hass, polling=False)
    interval = timedelta(minutes=settings[S_TESLA_REFRESH])
    t = NOV
    await spend.async_watch(t, False, {"tesla": False})
    assert refreshes.calls == []  # the charger is empty
    await spend.async_watch(t, True, {"tesla": False})  # a car is connected: which one?
    assert refreshes.calls == [TES["soc"]]
    assert spend.counts.refreshes == 1 and spend.counts.data == TESLA_REFRESH_DATA
    # the Tesla is on the charger: every interval, not sooner
    for m in (1, 30, interval.total_seconds() / 60 - 1):
        await spend.async_watch(t + timedelta(minutes=m), True, {"tesla": True})
    assert len(refreshes.calls) == 1
    await spend.async_watch(t + interval, True, {"tesla": True})
    assert len(refreshes.calls) == 2
    # the Rivian is on the charger: no refresh of the Tesla
    await spend.async_watch(t + 3 * interval, True, {"tesla": False})
    assert len(refreshes.calls) == 2
    # the Tesla leaves: once, so its plug reading changes
    await spend.async_watch(t + 3 * interval, True, {"tesla": True})
    await spend.async_watch(t + 3 * interval + timedelta(minutes=1), False, {"tesla": True})
    assert len(refreshes.calls) == 4
    await spend.async_watch(t + 6 * interval, False, {"tesla": False})
    assert len(refreshes.calls) == 4


async def test_refreshes_near_and_at_the_cap(
    planner_hass: HomeAssistant, refreshes: Refreshes
) -> None:
    hass = planner_hass
    set_tesla(hass, plugged="on")
    spend, settings = make_spend(hass)
    tesla_fleet_entry(hass, polling=False)
    cap = DEFAULT_SETTINGS[S_TESLA_BUDGET]  # polling off: the whole budget
    spend.counts = SpendCounts(month_key(NOV), wakes=wakes_worth(TESLA_ESSENTIAL_AT * cap))
    interval = timedelta(minutes=settings[S_TESLA_REFRESH])
    # near the cap: no timed refresh, but the one at a plug-in still runs
    spend.wall_prev = True
    await spend.async_watch(NOV, True, {"tesla": True})
    assert refreshes.calls == []
    await spend.async_watch(NOV, False, {"tesla": False})
    await spend.async_watch(NOV + timedelta(minutes=1), True, {"tesla": False})
    assert len(refreshes.calls) == 1
    # at the cap: none at all
    spend.counts.wakes = wakes_worth(cap - TESLA_WORST_SEND)
    await spend.async_watch(NOV + interval, False, {"tesla": True})
    await spend.async_watch(NOV + 2 * interval, True, {"tesla": False})
    assert len(refreshes.calls) == 1
    assert not await spend.async_refresh(CAR, NOV + 2 * interval)


def teslamate_battery(hass: HomeAssistant, polling: bool) -> MockConfigEntry:
    """The Tesla read from TeslaMate over MQTT: the battery level in the MQTT entry, the
    charge switch in Tesla Fleet's."""
    fleet = MockConfigEntry(domain="tesla_fleet", pref_disable_polling=not polling)
    fleet.add_to_hass(hass)
    mqtt = MockConfigEntry(domain="mqtt")
    mqtt.add_to_hass(hass)
    registry = er.async_get(hass)
    for entity_id, platform, entry in (
        (TES["switch"], "tesla_fleet", fleet),
        (TES["soc"], "mqtt", mqtt),
    ):
        state = hass.states.get(entity_id)
        if state is not None:
            hass.states.async_remove(entity_id)
        domain, object_id = entity_id.split(".", 1)
        created = registry.async_get_or_create(
            domain, platform, f"uid-{object_id}", suggested_object_id=object_id, config_entry=entry
        )
        assert created.entity_id == entity_id
        if state is not None:
            hass.states.async_set(entity_id, state.state, state.attributes)
    return fleet


async def test_with_teslamate_only_a_command_brings_a_refresh(
    planner_hass: HomeAssistant, cars: FakeCars, refreshes: Refreshes
) -> None:
    """TeslaMate keeps the battery and plug current for free: Tesla Fleet's polling is read
    from its own entity, the timed refreshes stop, and the refresh before a command (so Tesla
    Fleet knows whether to wake the car) goes to a Tesla Fleet entity."""
    hass = planner_hass
    spend, settings = make_spend(hass)
    fleet = teslamate_battery(hass, polling=True)
    assert spend.fleet_entity(CAR) == TES["switch"]
    assert spend.polling(CAR) is True and spend.cap == pytest.approx(CAP_POLLING)
    hass.config_entries.async_update_entry(fleet, pref_disable_polling=True)
    assert spend.polling(CAR) is False and not spend.reads_from_fleet(CAR)
    interval = timedelta(minutes=settings[S_TESLA_REFRESH])
    await spend.async_watch(NOV, True, {"tesla": False})  # a car connected
    await spend.async_watch(NOV + interval, True, {"tesla": True})
    await spend.async_watch(NOV + 2 * interval, False, {"tesla": True})  # the Tesla left
    assert refreshes.calls == [] and spend.counts.data == 0
    executor, rt = CarExecutor(hass, spend), CarRuntime()
    assert await executor.async_apply(CAR, rt, 16, NOV + 3 * interval)
    assert refreshes.calls == [TES["switch"]]


def test_a_clock_set_back_keeps_the_count(planner_hass: HomeAssistant) -> None:
    spend, _ = make_spend(planner_hass)
    spend.count_command(NOV)
    spend.count_command(NOV.replace(month=10))  # the clock briefly a month behind
    assert spend.counts == SpendCounts("2026-11", commands=2) and spend.last_month is None


async def test_a_car_connected_at_start_up_is_refreshed(
    planner_hass: HomeAssistant, refreshes: Refreshes
) -> None:
    hass = planner_hass
    set_tesla(hass, plugged="off")  # stale: the car was plugged in while HA was down
    spend, _ = make_spend(hass)
    tesla_fleet_entry(hass, polling=False)
    await spend.async_watch(NOV, True, {"tesla": False})
    assert len(refreshes.calls) == 1
    await spend.async_watch(NOV + timedelta(minutes=1), True, {"tesla": False})
    assert len(refreshes.calls) == 1


async def test_a_failed_refresh_is_counted(planner_hass: HomeAssistant) -> None:
    """``homeassistant.update_entity`` is not registered here: the call fails."""
    spend, _ = make_spend(planner_hass)
    assert not await spend.async_refresh(CAR, NOV)
    assert spend.counts.refreshes == 1 and spend.last_error is not None
    assert spend.refreshed["tesla"] == NOV  # not tried again at once


async def test_old_data_is_refreshed_before_a_command(
    planner_hass: HomeAssistant, cars: FakeCars, refreshes: Refreshes
) -> None:
    """With Tesla Fleet's polling off the cached state is old: the planner refreshes first,
    so Tesla Fleet does not wake an awake car, and a car already there gets no command."""
    hass = planner_hass
    spend, _ = make_spend(hass)
    tesla_fleet_entry(hass, polling=False)
    hass.states.async_set(STATUS, "off")  # asleep at the last read
    refreshes.then = lambda: hass.states.async_set(STATUS, "on")
    executor, rt = CarExecutor(hass, spend), CarRuntime()
    assert await executor.async_apply(CAR, rt, 16, NOV)
    assert refreshes.calls == [TES["soc"]]
    assert spend.counts.wakes == 0 and spend.counts.commands == 2
    # fresh data: no refresh
    assert await executor.async_apply(CAR, rt, 0, NOV + TESLA_FRESH_BEFORE_SEND / 2)
    assert len(refreshes.calls) == 1
    # the refresh shows the car already paused: nothing to send
    hass.states.async_set(TES["switch"], "on")
    refreshes.then = lambda: hass.states.async_set(TES["switch"], "off")
    cars.clear()
    assert await executor.async_apply(CAR, rt, 0, NOV + TESLA_FRESH_BEFORE_SEND * 2)
    assert cars.calls == [] and rt.commanded_a == 0
    assert len(refreshes.calls) == 2


async def test_no_refresh_before_a_command_while_tesla_fleet_polls(
    planner_hass: HomeAssistant, cars: FakeCars, refreshes: Refreshes
) -> None:
    hass = planner_hass
    spend, _ = make_spend(hass)
    tesla_fleet_entry(hass, polling=True)
    assert await CarExecutor(hass, spend).async_apply(CAR, CarRuntime(), 16, NOV)
    assert refreshes.calls == [] and len(cars.calls) == 2


# --------------------------------------------------------------------------- the planner


async def test_the_sensor_the_budget_and_a_restart(
    site: HomeAssistant, fake_cars: FakeCars
) -> None:
    hass = site
    entry = make_entry(TESLA)
    coordinator = await setup_entry(hass, entry, fake_cars)
    state = hass.states.get(SPEND)
    assert float(state.state) == 0.0
    assert state.attributes["unit_of_measurement"] == "USD"
    assert state.attributes["level"] == LEVEL_NORMAL
    assert state.attributes["cap"] == round(CAP_POLLING, 2)
    assert state.attributes["tesla_fleet_polling"] == {"tesla": "unknown"}
    assert state.attributes["wakes_counted"] == {"tesla": "every send"}
    now = dt_util.now()
    coordinator.spend.count_command(now)
    coordinator.spend.count_wake(now)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": BUDGET, "value": 2.5}, blocking=True
    )
    await hass.async_block_till_done()
    state = hass.states.get(SPEND)
    assert float(state.state) == pytest.approx(0.001 + 0.02 + 0.002 * TESLA_WAKE_CHECKS)
    assert (state.attributes["commands"], state.attributes["wakes"]) == (1, 1)
    assert state.attributes["budget"] == 2.5 and state.attributes["cap"] == 2.5
    # the count and the budget survive a restart
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    spend = entry.runtime_data.spend
    assert (spend.counts.commands, spend.counts.wakes) == (1, 1)
    assert spend.budget == 2.5


async def test_without_a_tesla_there_is_no_guard(site: HomeAssistant, fake_cars: FakeCars) -> None:
    await setup_entry(site, make_entry(RIVIAN), fake_cars)
    assert site.states.get(SPEND) is None and site.states.get(BUDGET) is None


async def test_the_reason_says_when_the_guard_holds_back(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    """Noon, the Tesla plugged in by day: near the cap its solar start waits and the reason
    says so; at the cap nothing goes out."""
    hass = site
    set_rivian(hass, plugged="off")
    set_tesla(hass, plugged="on")
    coordinator = await setup_entry(hass, make_entry(TESLA), fake_cars)
    now = dt_util.now()
    spend = coordinator.spend
    spend.counts = SpendCounts(month_key(now), wakes=wakes_worth(TESLA_ESSENTIAL_AT * CAP_POLLING))
    await turn(hass, SHADOW, False)
    assert coordinator.data.identification.car_id == "tesla"
    assert fake_cars.calls == []
    assert "Tesla API spend" in coordinator.data.reason
    assert "only started in the free hours" in coordinator.data.reason
    waiting = hass.states.get("sensor.roxxo_planner_planned_current").attributes["waiting"]
    assert "nearly used" in waiting
    # at the cap: the night's free hours start nothing either
    spend.counts.wakes = wakes_worth(CAP_POLLING - TESLA_WORST_SEND)
    set_site(hass, pv_w=0, grid_w=900, battery_w=0)
    await cycle(hass, coordinator, freezer, minutes=9 * 60)
    assert coordinator.data.mode == "night"
    assert fake_cars.calls == []
    assert "nothing is sent to the Tesla" in coordinator.data.reason


async def test_the_planner_refreshes_a_plugged_in_tesla(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    set_rivian(hass, plugged="off")
    set_tesla(hass, plugged="on")
    set_site(hass, wall_connected="off")
    tesla_fleet_entry(hass, polling=False)
    refreshes = Refreshes(hass)
    refreshes.install()
    coordinator = await setup_entry(hass, make_entry(TESLA), fake_cars)
    assert refreshes.calls == []
    set_site(hass, wall_connected="on")  # plugged in
    await cycle(hass, coordinator, freezer)
    assert refreshes.calls == [TES["soc"]]
    assert coordinator.data.identification.car_id == "tesla"
    interval = int(coordinator.settings[S_TESLA_REFRESH])
    await cycle(hass, coordinator, freezer, minutes=interval - 2)
    assert len(refreshes.calls) == 1
    await cycle(hass, coordinator, freezer, minutes=2)
    assert len(refreshes.calls) == 2
    assert hass.states.get(SPEND).attributes["tesla_fleet_polling"] == {"tesla": "off"}
    # planning off: no refresh
    await turn(hass, "switch.solar_ev_planner_planning", False)
    count = len(refreshes.calls)
    await cycle(hass, coordinator, freezer, minutes=2 * interval)
    assert len(refreshes.calls) == count


def test_a_stop_to_a_charging_tesla_goes_out_when_the_budget_is_used_up(
    planner_hass: HomeAssistant,
) -> None:
    spend, _ = make_spend(planner_hass)
    spend.counts = SpendCounts(month_key(NOV), wakes=wakes_worth(CAP_POLLING - TESLA_WORST_SEND))
    assert spend.level(NOV) == LEVEL_STOPPED
    # a charging car is awake: the stop costs one command and saves paid-hours power
    assert spend.gate(0, True, 48, NOV, False) == (0, None)
    assert spend.gate(0, False, 0, NOV, False)[1] is not None
    assert spend.gate(16, True, 48, NOV, True)[1] is not None
