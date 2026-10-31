"""Tests for the Rivian schedule ownership state machine."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.rivian_schedule_control.api import (
    AuthError,
    RateLimitedError,
    ScheduleApiError,
    WriteRejectedError,
)
from custom_components.rivian_schedule_control.controller import (
    ControlState,
    Journal,
    JournalSaveError,
    Lease,
    Observation,
    Phase,
    ReleaseResult,
    RivianScheduleController,
    ScheduleControlConfig,
)

CDT = timezone(timedelta(hours=-5))
CHICAGO = ZoneInfo("America/Chicago")
T0 = datetime(2026, 10, 24, 12, 0, tzinfo=CDT)  # Saturday
HOME = {"latitude": 30.0, "longitude": -97.0}
PLUGGED = Observation(plugged=True, home=True, charging=False)
CHARGING = Observation(plugged=True, home=True, charging=True)
UNPLUGGED = Observation(plugged=False, home=True, charging=False)
AWAY_PLUGGED = Observation(plugged=True, home=False, charging=False)
UNKNOWN = Observation()

OWNER = [
    {
        "weekDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "startTime": 0,
        "duration": 360,
        "location": {"latitude": 30.00001, "longitude": -97.00001},
        "amperage": 48,
        "enabled": True,
    },
    {
        "weekDays": ["Saturday"],
        "startTime": 600,
        "duration": 60,
        "location": {"latitude": 31.0, "longitude": -96.0},
        "amperage": 32,
        "enabled": False,
    },
]


class FakeProvider:
    """In-memory Rivian schedule provider with scriptable behavior."""

    def __init__(self, schedules: list[dict[str, Any]], store: FakeStore | None = None):
        self.schedules = deepcopy(schedules)
        self.store = store
        self.sets: list[list[dict[str, Any]]] = []
        self.gets = 0
        self.set_error: Exception | None = None
        self.apply_set = True
        self.get_error: Exception | None = None
        self.reverse_on_set = False
        self.on_get = None
        self.on_set = None
        self.journal_at_set: list[dict[str, Any] | None] = []

    async def get_schedules(self):
        self.gets += 1
        if self.on_get:
            await self.on_get()
        if self.get_error:
            raise self.get_error
        return deepcopy(self.schedules)

    async def set_schedules(self, schedules):
        self.sets.append(deepcopy(schedules))
        if self.store is not None:
            self.journal_at_set.append(deepcopy(self.store.data))
        if self.on_set:
            await self.on_set()
        if self.apply_set:
            self.schedules = deepcopy(schedules[::-1] if self.reverse_on_set else schedules)
        if self.set_error:
            raise self.set_error


class FakeStore:
    def __init__(self, data: dict[str, Any] | None = None):
        self.data = deepcopy(data)
        self.saves = 0
        self.fail = False

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        if self.fail:
            raise OSError("disk full")
        self.data = deepcopy(data)
        self.saves += 1


# The increase rules tuned for evcc, which padded its requests by up to 2 A and
# chased the surplus: increases 3 A below the request, and that margin kept at
# the ceiling for 30 minutes after a decrease from 48 A. Off by default.
EVCC_RULES: dict[str, Any] = {"increase_margin_amps": 3.0, "ceiling_hold": timedelta(minutes=30)}


def make(
    schedules=OWNER, *, store: FakeStore | None = None, **config: Any
) -> tuple[RivianScheduleController, FakeProvider, FakeStore]:
    store = store or FakeStore()
    provider = FakeProvider(schedules, store)
    options = {"home_location": HOME, "time_zone": CDT, "dry_run": False} | config
    return (
        RivianScheduleController(provider, store, ScheduleControlConfig(**options)),
        provider,
        store,
    )


async def start(controller, *, amps=16, obs=PLUGGED, now=T0):
    await controller.async_request_amps(amps)
    await controller.async_request_charge(True)
    await controller.async_tick(now, obs)


def at(seconds: float = 0, minutes: float = 0, base: datetime = T0) -> datetime:
    return base + timedelta(seconds=seconds, minutes=minutes)


# Takeover --------------------------------------------------------------------


async def test_no_command_means_no_provider_access():
    controller, provider, _ = make()
    await controller.async_tick(T0, PLUGGED)
    assert provider.gets == 0
    assert controller.state(T0) is ControlState.IDLE


async def test_command_for_an_unplugged_car_is_discarded():
    controller, provider, _ = make()
    await controller.async_request_charge(True)
    await controller.async_tick(T0, UNPLUGGED)
    assert controller.journal.command_received is False
    await controller.async_tick(at(minutes=60), PLUGGED)
    assert provider.gets == 0


async def test_command_survives_an_away_glitch_while_plugged():
    controller, provider, _ = make()
    await controller.async_request_amps(16)
    await controller.async_tick(T0, AWAY_PLUGGED)
    assert controller.journal.command_received is True
    assert provider.gets == 0
    await controller.async_tick(at(minutes=1), PLUGGED)
    assert controller.state(at(minutes=1)) is ControlState.LEASE_ACTIVE


async def test_command_with_unknown_state_waits():
    controller, provider, _ = make()
    await controller.async_request_charge(True)
    await controller.async_tick(T0, UNKNOWN)
    assert provider.gets == 0
    assert controller.state(T0) is ControlState.WAITING_FOR_VEHICLE


async def test_takeover_persists_snapshot_before_first_write():
    controller, provider, store = make()
    await start(controller)
    assert len(provider.sets) == 1
    journal_then = provider.journal_at_set[0]
    assert journal_then["phase"] == "writing"
    assert journal_then["original"] == OWNER
    assert journal_then["pending"]["purpose"] == "lease"
    assert len(journal_then["written_states"]) == 1
    assert store.data["phase"] == "owned"


async def test_start_writes_padded_active_lease_at_home_schedule_location():
    controller, provider, _ = make()
    await start(controller, amps=13)
    [lease] = provider.sets[0]
    assert lease["amperage"] == 12
    assert lease["weekDays"] == ["Saturday"]
    assert lease["startTime"] == 11 * 60 + 55
    assert lease["duration"] == 30  # 20 min lease + 5 min skew at both ends
    assert lease["location"] == {"latitude": 30.00001, "longitude": -97.00001}
    assert controller.state(T0) is ControlState.LEASE_ACTIVE
    assert controller.journal.lease.expires_at == T0.astimezone(UTC) + timedelta(minutes=20)


async def test_lease_location_falls_back_to_home():
    controller, provider, _ = make([OWNER[1]])
    await start(controller)
    assert provider.sets[0][0]["location"] == HOME


async def test_current_only_first_command_means_start():
    controller, provider, _ = make()
    await controller.async_request_amps(10)
    await controller.async_tick(T0, PLUGGED)
    assert controller.journal.desired_charge is True
    assert provider.sets[0][0]["amperage"] == 10
    assert controller.state(T0) is ControlState.LEASE_ACTIVE


async def test_explicit_stop_is_not_flipped_by_later_current():
    controller, provider, _ = make()
    await controller.async_request_charge(False)
    await controller.async_request_amps(10)
    await controller.async_tick(T0, PLUGGED)
    assert controller.journal.desired_charge is False
    assert controller.state(T0) is ControlState.PAUSED


async def test_identical_requests_are_not_saved_again():
    controller, _, store = make()
    await controller.async_request_charge(True)
    await controller.async_request_amps(16)
    saves = store.saves
    await controller.async_request_charge(True)
    await controller.async_request_amps(16)
    assert store.saves == saves


async def test_wall_connector_without_vehicle_blocks_takeover():
    controller, provider, _ = make()
    await controller.async_request_charge(True)
    await controller.async_tick(T0, Observation(plugged=True, home=True, at_charger=False))
    assert provider.gets == 0


# Current and timing -----------------------------------------------------------


async def test_pause_parks_window_in_the_past_at_minimum_current():
    controller, provider, _ = make()
    await start(controller, amps=32)
    await controller.async_request_charge(False)
    await controller.async_tick(at(seconds=6), PLUGGED)
    [parked] = provider.sets[-1]
    assert parked["startTime"] + parked["duration"] == 11 * 60 + 55
    assert parked["amperage"] == 8
    assert controller.state(T0) is ControlState.PAUSED


async def test_transitions_use_short_spacing_other_writes_the_interval():
    controller, provider, _ = make()
    await start(controller, amps=20)
    await controller.async_request_amps(14)
    await controller.async_tick(at(seconds=10), CHARGING)
    assert len(provider.sets) == 1  # a decrease waits for min_write_interval
    await controller.async_request_charge(False)
    await controller.async_tick(at(seconds=11), CHARGING)
    assert len(provider.sets) == 2  # a pause only needs 5 s


async def test_increase_applies_the_exact_request_after_the_interval():
    # The planner asks for exact currents and rate-limits itself: no margin.
    controller, provider, _ = make()
    assert controller.config.increase_margin_amps == 0
    await start(controller, amps=16)
    await controller.async_request_amps(24)
    await controller.async_tick(at(seconds=119), CHARGING)
    assert len(provider.sets) == 1  # the increase interval runs from the start
    await controller.async_tick(at(seconds=120), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 24
    # Rounded down to an even current, as decreases are.
    await controller.async_request_amps(31)
    await controller.async_tick(at(minutes=4, seconds=1), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 30
    # A request that rounds down to the current lease writes nothing.
    await controller.async_request_amps(31.9)
    await controller.async_tick(at(minutes=7), CHARGING)
    assert len(provider.sets) == 3


async def test_increase_by_a_single_step_is_applied_without_a_margin():
    controller, provider, _ = make()
    await start(controller, amps=16)
    await controller.async_request_amps(18)
    await controller.async_tick(at(minutes=3), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 18


async def test_no_ceiling_hold_by_default():
    controller, provider, _ = make()
    await start(controller, amps=48)
    await controller.async_request_amps(46)
    await controller.async_tick(at(minutes=1), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 46
    await controller.async_request_amps(48)
    await controller.async_tick(at(minutes=3, seconds=1), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 48  # after the increase interval only


async def test_ceiling_hold_without_a_margin_changes_nothing():
    # The hold keeps the margin at the ceiling; with no margin there is nothing to keep.
    controller, provider, _ = make(ceiling_hold=timedelta(minutes=30))
    await start(controller, amps=48)
    await controller.async_request_amps(46)
    await controller.async_tick(at(minutes=1), CHARGING)
    await controller.async_request_amps(48)
    await controller.async_tick(at(minutes=3, seconds=1), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 48


async def test_margin_without_a_ceiling_hold_reaches_48_after_a_decrease():
    controller, provider, _ = make(increase_margin_amps=3.0)
    await start(controller, amps=48)
    await controller.async_request_amps(46.4)
    await controller.async_tick(at(minutes=1), CHARGING)
    await controller.async_request_amps(48)
    await controller.async_tick(at(minutes=3, seconds=1), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 48


async def test_decrease_is_prompt_increase_keeps_a_margin():
    controller, provider, _ = make(**EVCC_RULES)
    await start(controller, amps=20)
    await controller.async_request_amps(14)
    await controller.async_tick(at(seconds=31), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 14
    await controller.async_request_amps(24)
    await controller.async_tick(at(seconds=70), CHARGING)
    assert len(provider.sets) == 2  # increase interval not yet elapsed
    await controller.async_tick(at(seconds=152), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 20  # 24 A requested, minus the 3 A margin


async def test_increase_reaches_the_ceiling_without_a_margin():
    controller, provider, _ = make(**EVCC_RULES)
    await start(controller, amps=16)
    await controller.async_request_amps(48)
    await controller.async_tick(at(minutes=3), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 48


async def test_decrease_from_the_ceiling_holds_the_margin_there():
    # Surplus just short of 48 A: evcc's +2 A allowance, clamped at 48, must not
    # make schedule control chase it up and down.
    controller, provider, _ = make(**EVCC_RULES)
    await start(controller, amps=48)
    await controller.async_request_amps(46.4)
    await controller.async_tick(at(minutes=1), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 46
    await controller.async_request_amps(48)
    for minute in range(3, 31):
        await controller.async_tick(at(minutes=minute), CHARGING)
    # Renewals only (at 13 and 25 minutes), no increase while the hold lasts.
    assert [s[0]["amperage"] for s in provider.sets[1:]] == [46, 46, 46]
    await controller.async_tick(at(minutes=31, seconds=1), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 48  # the hold has ended


async def test_deep_decrease_from_the_ceiling_does_not_hold_it():
    # A cloud pulls evcc far below 48 A; afterwards 48 A is reached again at once.
    controller, provider, _ = make(**EVCC_RULES)
    await start(controller, amps=48)
    await controller.async_request_amps(18)
    await controller.async_tick(at(minutes=1), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 18
    await controller.async_request_amps(48)
    await controller.async_tick(at(minutes=3, seconds=1), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 48


async def test_band_just_below_an_even_step_does_not_increase():
    # evcc asks 20 A for a car at 16 A whose real headroom is about 17.9 A.
    controller, provider, _ = make(**EVCC_RULES)
    await start(controller, amps=16)
    await controller.async_request_amps(20)
    await controller.async_tick(at(minutes=3), CHARGING)
    assert len(provider.sets) == 1


async def test_renewals_do_not_reset_the_increase_timer():
    controller, provider, _ = make(increase_interval=timedelta(minutes=15))
    await start(controller, amps=10)
    await controller.async_request_amps(40)
    await controller.async_tick(at(minutes=12), CHARGING)  # renewal at 10 A
    assert provider.sets[-1][0]["amperage"] == 10
    await controller.async_tick(at(minutes=16), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 40


async def test_requests_hovering_near_the_lease_do_not_cycle_with_a_margin():
    # Without a margin they would: the planner rate-limits its own requests.
    controller, provider, _ = make(**EVCC_RULES)
    await start(controller, amps=12)
    for minute, amps in enumerate([13, 14, 15, 14, 13, 12, 13, 15], start=1):
        await controller.async_request_amps(amps)
        await controller.async_tick(at(minutes=minute * 3), CHARGING)
    assert [s[0]["amperage"] for s in provider.sets] == [12, 12, 12]  # start + renewals only


async def test_lease_renews_before_expiry_without_applying_a_deferred_increase():
    controller, provider, _ = make(increase_interval=timedelta(minutes=30))
    await start(controller, amps=16)
    await controller.async_request_amps(30)
    await controller.async_tick(at(minutes=11), CHARGING)
    assert len(provider.sets) == 1
    await controller.async_tick(at(minutes=12), CHARGING)
    assert len(provider.sets) == 2
    assert provider.sets[-1][0]["amperage"] == 16


# Fail-safe renewal conditions --------------------------------------------------


async def test_renewal_disabled_lets_lease_lapse_and_survives_restart():
    store = FakeStore()
    controller, provider, _ = make(store=store)
    await start(controller)
    await controller.async_set_renewal(False)
    restarted = RivianScheduleController(provider, store, controller.config)
    assert await restarted.async_load()
    assert restarted.renewal_enabled is False
    await restarted.async_tick(at(minutes=15), CHARGING)
    assert len(provider.sets) == 1
    assert restarted.state(at(minutes=21)) is ControlState.LEASE_LAPSED


async def test_decrease_with_renewal_disabled_keeps_the_lease_end():
    controller, provider, _ = make()
    await start(controller, amps=20)
    await controller.async_set_renewal(False)
    await controller.async_request_amps(10)
    await controller.async_tick(at(minutes=5), CHARGING)
    [window] = provider.sets[-1]
    assert window["amperage"] == 10
    # Original end: 12:20 + 5 min skew = 12:25; the rewrite must not move it.
    assert window["startTime"] + window["duration"] == 12 * 60 + 25
    assert controller.journal.lease.expires_at == T0.astimezone(UTC) + timedelta(minutes=20)


async def test_unknown_vehicle_state_stops_renewals_parks_but_does_not_release():
    controller, provider, _ = make()
    await start(controller, amps=32)
    await controller.async_tick(at(minutes=13), UNKNOWN)
    assert len(provider.sets) == 1  # no renewal
    # The window runs on the vehicle until 5 minutes (the clock skew) past the
    # lease's end; that lapse is the cloud-free stop, so nothing is written yet.
    await controller.async_tick(at(minutes=21), UNKNOWN)
    await controller.async_tick(at(minutes=25, seconds=59), UNKNOWN)
    assert len(provider.sets) == 1
    assert controller.state(at(minutes=21)) is ControlState.LEASE_LAPSED
    await controller.async_tick(at(minutes=26), UNKNOWN)
    # Then the lapsed lease is parked so it cannot recur a week later at 32 A.
    [parked] = provider.sets[-1]
    assert len(provider.sets) == 2 and parked["amperage"] == 8
    assert controller.journal.phase is Phase.OWNED
    assert controller.state(at(minutes=26)) is ControlState.PAUSED
    await controller.async_tick(at(minutes=27), CHARGING)
    assert controller.state(at(minutes=27)) is ControlState.LEASE_ACTIVE  # resumes when known


async def test_stop_request_after_lease_end_parks_at_once():
    controller, provider, _ = make()
    await start(controller, amps=32)
    await controller.async_tick(at(minutes=13), UNKNOWN)
    await controller.async_request_charge(False)
    await controller.async_tick(at(minutes=21), UNKNOWN)
    # The controller asked to stop: the window's tail on the vehicle is not waited out.
    assert len(provider.sets) == 2 and provider.sets[-1][0]["amperage"] == 8


DEAD = Observation(plugged=True, home=True, charging=True, controller_alive=False)
ALIVE = Observation(plugged=True, home=True, charging=True, controller_alive=True)


async def test_controller_down_for_the_grace_holds_its_request_and_parks():
    controller, provider, _ = make()
    await start(controller, obs=ALIVE)
    await controller.async_tick(at(minutes=1), DEAD)
    await controller.async_tick(at(minutes=3, seconds=59), DEAD)
    assert len(provider.sets) == 1  # one reading is not enough
    assert controller.snapshot(at(minutes=3))["start_blocked_by"] == ["controller_unavailable"]
    await controller.async_tick(at(minutes=4), DEAD)
    assert provider.sets[-1][0]["amperage"] == 8  # parked
    snapshot = controller.snapshot(at(minutes=4))
    assert snapshot["request_held"] is True
    assert snapshot["start_blocked_by"] == ["controller_unavailable"]
    # Back up, but nothing says the controller kept running: its old request is dropped.
    alive = Observation(plugged=True, home=True, charging=False, controller_alive=True)
    await controller.async_tick(at(minutes=30), alive)
    assert controller.journal.command_received is True  # a late enabled reading may follow
    await controller.async_tick(at(minutes=30, seconds=30), alive)
    assert controller.journal.command_received is False
    assert len(provider.sets) == 2  # the controller must ask again
    await controller.async_request_charge(True)
    await controller.async_tick(at(minutes=31), alive)
    assert controller.state(at(minutes=31)) is ControlState.LEASE_ACTIVE


async def test_held_request_resumes_when_the_controller_still_wants_charging():
    # The liveness source failed while the controller kept running: it will not ask again.
    controller, provider, _ = make()
    await start(controller, amps=48, obs=ALIVE)
    for minute in range(1, 10):
        await controller.async_tick(at(minutes=minute), DEAD)
    assert provider.sets[-1][0]["amperage"] == 8
    enabled = Observation(plugged=True, home=True, controller_alive=True, controller_charging=True)
    await controller.async_tick(at(minutes=10), enabled)
    assert controller.state(at(minutes=10)) is ControlState.LEASE_ACTIVE
    assert provider.sets[-1][0]["amperage"] == 48
    assert controller.snapshot(at(minutes=10))["request_unconfirmed"] is False


async def test_held_request_is_dropped_when_the_controller_no_longer_wants_charging():
    # The controller restarted while down: it does not want charging for a parked car.
    controller, provider, _ = make()
    await start(controller, obs=ALIVE)
    for minute in range(1, 10):
        await controller.async_tick(at(minutes=minute), DEAD)
    disabled = Observation(
        plugged=True, home=True, controller_alive=True, controller_charging=False
    )
    await controller.async_tick(at(minutes=10), disabled)
    assert controller.journal.command_received is False
    assert len(provider.sets) == 2


async def test_liveness_gap_shorter_than_the_grace_keeps_charging():
    controller, provider, _ = make()
    await start(controller, amps=48, obs=ALIVE)
    await controller.async_tick(at(minutes=12), DEAD)  # one failed poll
    assert len(provider.sets) == 1  # no renewal while the controller looks down
    await controller.async_tick(at(minutes=13), ALIVE)
    assert controller.journal.command_received is True
    assert len(provider.sets) == 2  # renewed at the same 48 A, no new command needed
    assert provider.sets[-1][0]["amperage"] == 48
    assert controller.state(at(minutes=13)) is ControlState.LEASE_ACTIVE


async def test_liveness_never_seen_since_startup_gets_the_longer_grace():
    controller, provider, _ = make()  # e.g. Home Assistant restarted before the planner
    await start(controller, obs=CHARGING)
    for minute in range(1, 11):
        await controller.async_tick(at(minutes=minute), DEAD)
    assert len(provider.sets) == 1
    await controller.async_tick(at(minutes=11), DEAD)
    assert controller.snapshot(at(minutes=11))["request_held"] is True
    assert provider.sets[-1][0]["amperage"] == 8


async def test_request_sent_while_the_controller_looks_down_is_kept_until_it_is_back():
    controller, provider, _ = make()
    await start(controller, obs=ALIVE)
    await controller.async_tick(at(minutes=1), DEAD)
    await controller.async_tick(at(minutes=4), DEAD)
    assert controller.snapshot(at(minutes=4))["request_held"] is True
    # The controller restarted and asks again before its liveness entity shows it as up.
    await controller.async_request_amps(20)
    await controller.async_request_charge(True)
    for minute in range(5, 15):
        await controller.async_tick(at(minutes=minute), DEAD)
    assert controller.journal.command_received is True
    assert controller.snapshot(at(minutes=14))["request_held"] is False
    assert provider.sets[-1][0]["amperage"] == 8  # nothing starts while it looks down
    await controller.async_tick(at(minutes=15), ALIVE)
    assert controller.state(at(minutes=15)) is ControlState.LEASE_ACTIVE
    assert provider.sets[-1][0]["amperage"] == 20


async def test_unchanged_request_while_the_controller_looks_down_shows_it_is_running():
    controller, provider, _ = make()
    await start(controller, amps=48, obs=ALIVE)
    await controller.async_tick(at(minutes=1), DEAD)
    # The controller repeats itself; nothing changes, so nothing is saved, but it counts.
    await controller.async_request_amps(48)
    await controller.async_request_charge(True)
    for minute in range(2, 6):
        await controller.async_tick(at(minutes=minute), DEAD)
    assert len(provider.sets) == 1  # not parked
    assert controller.snapshot(at(minutes=5))["request_held"] is False
    await controller.async_tick(at(minutes=6), ALIVE)
    assert controller.state(at(minutes=6)) is ControlState.LEASE_ACTIVE


async def test_unchanged_start_alone_while_the_controller_looks_down_shows_it_is_running():
    controller, provider, _ = make()
    await start(controller, amps=48, obs=ALIVE)
    await controller.async_tick(at(minutes=1), DEAD)
    await controller.async_request_charge(True)  # switch turned on again, unchanged
    for minute in range(2, 6):
        await controller.async_tick(at(minutes=minute), DEAD)
    assert len(provider.sets) == 1  # not parked
    assert controller.snapshot(at(minutes=5))["request_held"] is False


async def test_no_takeover_while_the_controller_looks_down():
    controller, provider, _ = make()
    await controller.async_request_charge(True)
    for minute in range(0, 30, 5):
        await controller.async_tick(at(minutes=minute), DEAD)
    # The owner's schedule is never taken over just to be parked.
    assert provider.gets == 0 and provider.sets == []
    assert controller.journal.phase is Phase.IDLE
    assert controller.snapshot(at(minutes=30))["start_blocked_by"] == ["controller_unavailable"]


async def restarted_after(controller, provider, store, minutes, obs, *, base=T0):
    """A new controller from the journal, as after a Home Assistant restart."""
    restarted = RivianScheduleController(provider, store, controller.config)
    assert await restarted.async_load()
    await restarted.async_tick(at(minutes=minutes, base=base), obs)
    return restarted


async def test_restart_drops_a_request_whose_lease_lapsed_meanwhile():
    # A power cut: the controller restarts and will ask again if it still wants
    # charging. The old request must not start charging from the grid.
    controller, provider, store = make()
    await start(controller, amps=32, obs=ALIVE)
    restarted = await restarted_after(controller, provider, store, 240, DEAD)
    assert restarted.journal.command_received is True  # controller not seen yet: wait
    assert provider.sets[-1][0]["amperage"] == 8  # the lapsed lease is parked
    up = Observation(plugged=True, home=True, controller_alive=True)
    await restarted.async_tick(at(minutes=244), up)
    await restarted.async_tick(at(minutes=244, seconds=30), up)
    assert restarted.journal.command_received is False
    assert len(provider.sets) == 2  # no new lease


async def test_restart_keeps_a_request_the_controller_still_wants():
    # Home Assistant was down for an hour; the controller still wants charging.
    controller, provider, store = make()
    await start(controller, amps=48, obs=ALIVE)
    enabled = Observation(plugged=True, home=True, controller_alive=True, controller_charging=True)
    restarted = await restarted_after(controller, provider, store, 60, enabled)
    assert restarted.state(at(minutes=60)) is ControlState.LEASE_ACTIVE
    assert provider.sets[-1][0]["amperage"] == 48


async def test_restart_keeps_a_live_lease_running():
    controller, provider, store = make()
    await start(controller, amps=48, obs=ALIVE)
    restarted = await restarted_after(controller, provider, store, 13, ALIVE)
    assert restarted.journal.command_received is True
    assert len(provider.sets) == 2 and provider.sets[-1][0]["amperage"] == 48  # renewed


async def test_restart_takes_no_ownership_on_an_unconfirmed_request():
    controller, provider, store = make()
    await controller.async_request_charge(True)
    await controller.async_tick(T0, AWAY_PLUGGED)  # waits for the car to be home
    saved = deepcopy(store.data)
    up = Observation(plugged=True, home=True, controller_alive=True)
    restarted = await restarted_after(controller, provider, store, 5, up)
    await restarted.async_tick(at(minutes=5, seconds=30), up)
    assert provider.gets == 0 and provider.sets == []  # the owner's schedule is untouched
    assert restarted.journal.command_received is False
    # Confirmed by the controller's charging state, the same request takes over at once.
    store.data = deepcopy(saved)
    restarted = await restarted_after(
        controller,
        provider,
        store,
        6,
        Observation(plugged=True, home=True, controller_charging=True),
    )
    assert restarted.state(at(minutes=6)) is ControlState.LEASE_ACTIVE


async def test_restart_drops_a_live_request_the_controller_no_longer_wants():
    controller, provider, store = make()
    await start(controller, amps=48, obs=ALIVE)
    disabled = Observation(
        plugged=True, home=True, controller_alive=True, controller_charging=False
    )
    restarted = await restarted_after(controller, provider, store, 2, disabled)
    assert restarted.journal.command_received is False
    assert provider.sets[-1][0]["amperage"] == 8


# Session boundaries -------------------------------------------------------------


async def test_unplug_parks_at_once_and_restores_exact_owner_list_after_delay():
    controller, provider, store = make()
    await start(controller)
    await controller.async_tick(at(seconds=10), UNPLUGGED)
    assert provider.sets[-1][0]["amperage"] == 8  # parked immediately
    assert controller.journal.command_received is False
    await controller.async_tick(at(seconds=50), UNPLUGGED)
    assert provider.schedules != OWNER
    await controller.async_tick(at(seconds=71), UNPLUGGED)
    assert provider.schedules == OWNER
    assert store.data["phase"] == "idle"
    assert store.data["original"] is None


async def test_replug_within_delay_keeps_ownership_parked_until_a_new_command():
    controller, provider, _ = make()
    await start(controller)
    await controller.async_tick(at(seconds=10), UNPLUGGED)
    await controller.async_tick(at(seconds=40), CHARGING)
    await controller.async_tick(at(minutes=5), CHARGING)
    assert controller.journal.phase is Phase.OWNED
    assert controller.state(at(minutes=5)) is ControlState.PAUSED
    await controller.async_request_charge(True)
    await controller.async_tick(at(minutes=6), PLUGGED)
    assert controller.state(at(minutes=6)) is ControlState.LEASE_ACTIVE


async def test_unknown_after_unplug_blip_resets_the_release_timer():
    controller, provider, _ = make()
    await start(controller)
    await controller.async_tick(at(seconds=10), UNPLUGGED)
    await controller.async_tick(at(seconds=30), Observation(plugged=True))
    await controller.async_tick(at(seconds=120), Observation(plugged=True))
    assert controller.journal.phase is Phase.OWNED


async def test_away_while_plugged_releases_only_after_the_long_delay():
    controller, provider, _ = make()
    await start(controller)
    await controller.async_tick(at(minutes=1), AWAY_PLUGGED)
    await controller.async_tick(at(minutes=5), AWAY_PLUGGED)
    assert controller.journal.phase is Phase.OWNED
    await controller.async_tick(at(minutes=11, seconds=1), AWAY_PLUGGED)
    assert provider.schedules == OWNER


async def test_away_glitch_while_plugged_keeps_the_controller_request():
    controller, provider, _ = make()
    await start(controller, amps=48)
    await controller.async_tick(at(minutes=1), AWAY_PLUGGED)
    await controller.async_tick(at(minutes=11, seconds=1), AWAY_PLUGGED)
    assert provider.schedules == OWNER
    assert controller.journal.phase is Phase.IDLE
    # Still plugged in: the controller's request stands (it may not repeat it).
    assert controller.journal.command_received is True
    await controller.async_tick(at(minutes=13), PLUGGED)
    assert controller.state(at(minutes=13)) is ControlState.LEASE_ACTIVE
    assert provider.sets[-1][0]["amperage"] == 48


async def test_away_then_unplugged_forgets_the_controller_request():
    controller, provider, _ = make()
    await start(controller)
    await controller.async_tick(at(minutes=1), AWAY_PLUGGED)
    await controller.async_tick(at(minutes=11, seconds=1), AWAY_PLUGGED)
    away_unplugged = Observation(plugged=False, home=False, charging=False)
    await controller.async_tick(at(minutes=12), away_unplugged)
    assert controller.journal.command_received is False
    await controller.async_tick(at(minutes=26 * 60), PLUGGED)
    assert provider.schedules == OWNER and controller.journal.phase is Phase.IDLE


# Faults and operator actions -----------------------------------------------------


async def test_external_edit_faults_and_never_overwrites():
    controller, provider, _ = make()
    await start(controller)
    provider.schedules[0]["amperage"] = 40  # owner edits in the Rivian app
    edited = deepcopy(provider.schedules)
    await controller.async_request_amps(10)
    await controller.async_tick(at(minutes=1), CHARGING)
    assert controller.journal.fault.code == "external_edit"
    assert provider.schedules == edited
    await controller.async_tick(at(minutes=5), UNPLUGGED)
    await controller.async_tick(at(minutes=7), UNPLUGGED)
    assert provider.schedules == edited
    assert len(provider.sets) == 1


async def test_clear_fault_after_owner_restored_manually():
    controller, provider, _ = make()
    await start(controller)
    provider.schedules = deepcopy(OWNER)
    await controller.async_tick(at(minutes=12), CHARGING)
    assert controller.journal.fault.code == "external_edit"
    assert await controller.async_clear_fault(at(minutes=13))
    assert controller.journal.phase is Phase.IDLE


async def test_abandon_only_while_faulted_and_archives_the_original():
    controller, provider, _ = make()
    await start(controller)
    assert await controller.async_abandon() is False
    provider.schedules[0]["amperage"] = 40
    await controller.async_tick(at(minutes=12), CHARGING)
    assert not await controller.async_clear_fault(at(minutes=13))
    assert await controller.async_abandon()
    assert controller.journal.phase is Phase.IDLE
    assert controller.journal.archived_original == OWNER


async def test_takeover_refuses_its_own_window_and_force_restore_recovers():
    controller, provider, _ = make()
    await start(controller)
    lease_list = deepcopy(provider.schedules)
    provider.schedules[0]["amperage"] = 40
    await controller.async_request_amps(10)  # the next write's guard read sees the edit
    await controller.async_tick(at(minutes=1), CHARGING)
    assert await controller.async_abandon()
    # The owner puts schedule control's own window back by accident (or a late write lands).
    provider.schedules = lease_list
    await controller.async_request_charge(True)
    await controller.async_tick(at(minutes=2), CHARGING)
    assert controller.journal.fault.code == "own_window_present"
    assert controller.journal.original is None
    assert await controller.async_force_restore(at(minutes=3))
    assert provider.schedules == OWNER
    assert controller.journal.phase is Phase.IDLE
    assert controller.journal.archived_original is None
    assert controller.journal.handed_back is True


async def test_force_restore_refused_outside_fault():
    controller, _, _ = make()
    await start(controller)
    assert await controller.async_force_restore(at(minutes=1)) is False


async def test_release_when_owner_list_is_already_back_needs_no_write_or_fault():
    controller, provider, _ = make()
    await start(controller)
    provider.schedules = deepcopy(OWNER)
    sets = len(provider.sets)
    await controller.async_request_release("manual")
    await controller.async_tick(at(minutes=1), CHARGING)
    assert controller.journal.phase is Phase.IDLE
    assert len(provider.sets) == sets


async def test_release_while_faulted_restores_after_clear():
    controller, provider, _ = make()
    await start(controller)
    provider.schedules[0]["amperage"] = 40
    await controller.async_request_amps(10)
    await controller.async_tick(at(minutes=1), CHARGING)
    assert await controller.async_request_release("manual") is ReleaseResult.AFTER_FAULT
    provider.schedules[0]["amperage"] = provider.sets[0][0]["amperage"]  # owner undoes the edit
    assert await controller.async_clear_fault(at(minutes=2))
    await controller.async_tick(at(minutes=3), CHARGING)
    assert provider.schedules == OWNER


# Release and hand-back --------------------------------------------------------------


async def test_release_in_idle_cancels_the_pending_command():
    controller, provider, _ = make()
    await controller.async_request_charge(True)
    assert await controller.async_request_release("manual") is ReleaseResult.CANCELLED
    await controller.async_tick(T0, PLUGGED)
    assert provider.gets == 0


async def test_release_during_takeover_read_cancels_the_takeover():
    controller, provider, _ = make()
    await controller.async_request_charge(True)

    async def release_meanwhile():
        provider.on_get = None
        await controller.async_request_release("manual")

    provider.on_get = release_meanwhile
    await controller.async_tick(T0, PLUGGED)
    assert provider.sets == []
    assert controller.journal.phase is Phase.IDLE


async def test_manual_release_hands_back_until_unplug():
    controller, provider, _ = make()
    await start(controller)
    await controller.async_request_release("manual")
    await controller.async_tick(at(minutes=1), CHARGING)
    assert provider.schedules == OWNER
    assert controller.state(at(minutes=1)) is ControlState.HANDED_BACK
    assert await controller.async_request_charge(True) is False
    await controller.async_tick(at(minutes=2), CHARGING)
    assert provider.schedules == OWNER
    await controller.async_tick(at(minutes=3), UNPLUGGED)
    assert await controller.async_request_charge(True) is True


async def test_requests_during_a_restore_are_kept():
    controller, provider, _ = make()
    await start(controller)
    await controller.async_request_release("dry", hand_back=False)

    async def controller_meanwhile():
        provider.on_set = None
        await controller.async_request_charge(True)

    provider.on_set = controller_meanwhile
    await controller.async_tick(at(minutes=1), CHARGING)
    assert provider.schedules == OWNER
    assert controller.journal.phase is Phase.IDLE
    assert controller.journal.command_received is True


# Write protocol ---------------------------------------------------------------------


async def test_ambiguous_write_that_landed_is_confirmed():
    controller, provider, _ = make()
    provider.set_error = ScheduleApiError("timeout")
    await start(controller)
    assert controller.state(T0) is ControlState.LEASE_ACTIVE


async def test_accepted_but_invisible_write_settles_before_counting():
    controller, provider, _ = make()
    provider.apply_set = False
    await start(controller)
    assert controller.journal.phase is Phase.WRITING  # first readback only
    await controller.async_tick(at(seconds=31), PLUGGED)
    assert controller.journal.unapplied_writes == 1
    for second in (62, 93, 124, 155, 186):
        await controller.async_tick(at(seconds=second), PLUGGED)
    assert controller.journal.fault.code == "write_not_applied"
    assert provider.schedules == OWNER


async def test_rejected_writes_count_at_once_back_off_and_fault():
    controller, provider, _ = make()
    provider.apply_set = False
    provider.set_error = WriteRejectedError("no")
    await start(controller)
    assert controller.journal.unapplied_writes == 1
    assert controller.journal.phase is Phase.OWNED
    for second in range(5, 600, 5):
        await controller.async_tick(at(seconds=second), PLUGGED)
    assert controller.journal.fault.code == "write_rejected"
    assert len(provider.sets) == 3


async def test_rate_limited_restore_is_not_counted_and_retries():
    controller, provider, _ = make()
    await start(controller)
    provider.set_error = RateLimitedError("slow down")
    provider.apply_set = False
    for minute in range(1, 40, 2):
        await controller.async_tick(at(minutes=minute), UNPLUGGED)
    assert controller.journal.phase is not Phase.FAULT
    assert controller.journal.unapplied_writes == 0
    provider.set_error = None
    provider.apply_set = True
    await controller.async_tick(at(minutes=50), UNPLUGGED)
    assert provider.schedules == OWNER


async def test_failed_guard_read_withdraws_the_unsent_write():
    controller, provider, _ = make()
    await start(controller)
    await controller.async_request_amps(10)
    provider.get_error = ScheduleApiError("502")
    await controller.async_tick(at(seconds=31), CHARGING)
    assert controller.journal.phase is Phase.OWNED
    assert controller.journal.unapplied_writes == 0
    assert controller.journal.attempts == []


async def test_invalid_restore_faults_with_restore_rejected():
    controller, provider, _ = make()
    await start(controller)
    provider.set_error = WriteRejectedError("bad", invalid_input=True)
    provider.apply_set = False
    await controller.async_request_release("manual")
    await controller.async_tick(at(minutes=1), CHARGING)
    assert controller.journal.fault.code == "restore_rejected"
    assert controller.journal.original == OWNER  # snapshot kept


async def test_failed_write_backs_off():
    controller, provider, _ = make()
    await start(controller, amps=20)
    provider.set_error = ScheduleApiError("timeout")
    provider.apply_set = False
    await controller.async_request_amps(10)
    await controller.async_tick(at(seconds=31), CHARGING)
    sets = len(provider.sets)
    await controller.async_tick(at(seconds=40), CHARGING)
    assert len(provider.sets) == sets
    assert controller.snapshot(at(seconds=40))["backoff_until"] is not None


async def test_release_with_an_outstanding_write_sends_restore_and_settles_before_confirming():
    controller, provider, _ = make()
    provider.apply_set = False
    await start(controller)  # write A: Rivian accepts it but shows no change yet
    await controller.async_request_release("manual")
    # A settles as not applied, but it could still land. Rivian still shows the
    # owner's list, yet the restore must be sent so it is the newest write.
    await controller.async_tick(at(seconds=31), PLUGGED)
    assert len(provider.sets) == 2
    assert provider.sets[-1] == OWNER
    assert controller.journal.phase is Phase.WRITING  # a matching read proves nothing yet
    await controller.async_tick(at(seconds=62), PLUGGED)
    assert controller.journal.phase is Phase.IDLE
    assert controller.journal.archived_original == OWNER  # kept in case A lands later


async def test_failed_restore_with_outstanding_write_is_not_confirmed():
    controller, provider, _ = make()
    provider.apply_set = False
    await start(controller)
    await controller.async_request_release("manual")
    provider.set_error = ScheduleApiError("timeout")
    await controller.async_tick(at(seconds=31), PLUGGED)
    assert controller.journal.phase is Phase.OWNED
    assert controller.journal.original == OWNER


async def test_an_older_write_landing_late_is_adopted_not_a_conflict():
    controller, provider, _ = make()
    provider.apply_set = False
    await start(controller, amps=20)
    await controller.async_tick(at(seconds=31), CHARGING)  # A settles as not applied
    first = deepcopy(provider.sets[0])
    await controller.async_request_amps(12)

    async def a_lands():
        provider.on_set = None
        provider.schedules = first

    provider.on_set = a_lands
    await controller.async_tick(at(seconds=62), CHARGING)
    assert controller.journal.phase is not Phase.FAULT
    assert controller.journal.lease.amps == 20


async def test_conflicting_readback_faults():
    controller, provider, _ = make()

    async def conflict():
        provider.apply_set = False
        provider.schedules = [OWNER[1]]

    provider.on_set = conflict
    await start(controller)
    assert controller.journal.fault.code == "conflict_after_write"


async def test_reordered_readback_is_the_same_list():
    controller, provider, _ = make()
    await start(controller)
    provider.reverse_on_set = True
    await controller.async_request_release("manual")
    await controller.async_tick(at(minutes=1), CHARGING)
    assert controller.journal.phase is Phase.IDLE
    assert provider.schedules == OWNER[::-1]


async def test_restart_mid_write_resolves_from_journal():
    store = FakeStore()
    controller, provider, _ = make(store=store)
    provider.set_error = RuntimeError("process stopped")  # the write lands, then the crash
    with pytest.raises(RuntimeError):
        await start(controller)
    assert store.data["phase"] == "writing"
    restarted = RivianScheduleController(provider, store, controller.config)
    await restarted.async_load()
    provider.set_error = None
    await restarted.async_tick(at(minutes=1), CHARGING)
    assert restarted.journal.phase is Phase.OWNED
    assert restarted.journal.lease.active is True
    await restarted.async_tick(at(minutes=3), UNPLUGGED)
    await restarted.async_tick(at(minutes=5), UNPLUGGED)
    assert provider.schedules == OWNER


async def test_sent_outcome_is_saved_before_the_write_settles():
    controller, provider, store = make()
    provider.apply_set = False  # accepted, not visible yet
    await start(controller)
    assert controller.journal.phase is Phase.WRITING
    assert store.data["pending"]["outcome"] == "sent"
    restarted = RivianScheduleController(provider, store, controller.config)
    await restarted.async_load()
    assert restarted.journal.pending is restarted.journal.attempts[-1]
    provider.schedules = deepcopy(provider.sets[-1])  # it lands after the restart
    await restarted.async_tick(at(seconds=40), CHARGING)
    assert restarted.journal.fault is None
    assert restarted.state(at(seconds=40)) is ControlState.LEASE_ACTIVE
    assert len(provider.sets) == 1


async def test_restart_before_the_outcome_save_keeps_the_write_as_ours():
    controller, provider, store = make()
    provider.apply_set = False
    provider.set_error = RuntimeError("process stopped")
    with pytest.raises(RuntimeError):
        await start(controller)
    assert store.data["pending"]["outcome"] == "unsent"  # it may still have been sent
    first = provider.sets[-1]
    restarted = RivianScheduleController(provider, store, controller.config)
    await restarted.async_load()
    provider.set_error = None
    provider.apply_set = True
    await restarted.async_tick(at(minutes=1), CHARGING)
    assert len(provider.sets) == 2  # sent again
    provider.schedules = deepcopy(first)  # then the first write lands late
    await restarted.async_request_amps(10)
    await restarted.async_tick(at(minutes=2), CHARGING)
    assert restarted.journal.fault is None
    assert provider.sets[-1][0]["amperage"] == 10


async def test_possibly_sent_write_is_kept_when_an_older_write_is_adopted():
    controller, provider, store = make()
    await start(controller)  # 16 A
    provider.apply_set = False  # Rivian applies writes late
    await controller.async_request_amps(10)
    await controller.async_tick(at(minutes=1), CHARGING)  # W1: sent, not visible
    first = provider.sets[-1]
    await controller.async_request_amps(12)
    provider.set_error = RuntimeError("process stopped")
    with pytest.raises(RuntimeError):
        # W1 settles as not applied (kept), then W2 is cut off while sending.
        await controller.async_tick(at(minutes=1, seconds=31), CHARGING)
    second = provider.sets[-1]
    assert store.data["pending"]["outcome"] == "unsent"
    provider.set_error = None
    provider.schedules = deepcopy(first)  # W1 lands before the restart
    restarted = RivianScheduleController(provider, store, controller.config)
    await restarted.async_load()
    await restarted.async_tick(at(minutes=2, seconds=40), CHARGING)  # adopts W1
    assert restarted.journal.fault is None
    provider.schedules = deepcopy(second)  # then W2 lands too
    provider.apply_set = True
    await restarted.async_request_amps(8)
    await restarted.async_tick(at(minutes=3, seconds=20), CHARGING)
    assert restarted.journal.fault is None  # W2 is recognized as schedule control's own
    assert provider.sets[-1][0]["amperage"] == 8


async def test_reloaded_pending_write_is_the_same_object_as_its_attempt():
    controller, provider, store = make()
    provider.apply_set = False
    await start(controller)
    loaded = Journal.from_dict(store.data)
    assert loaded.pending is not None and loaded.pending is loaded.attempts[-1]


async def test_release_during_the_takeover_save_writes_no_lease():
    controller, provider, store = make()
    await controller.async_request_charge(True)
    original_save = store.async_save
    calls = 0

    async def save_and_release(data):
        nonlocal calls
        await original_save(data)
        calls += 1
        if data["phase"] == "owned" and calls == 1:
            store.async_save = original_save
            await controller.async_request_release("manual")

    store.async_save = save_and_release
    await controller.async_tick(T0, PLUGGED)
    assert provider.sets == []


async def test_owner_edited_lease_window_is_refused_after_abandon():
    controller, provider, _ = make()
    await start(controller)
    provider.schedules[0]["amperage"] = 40  # the owner edits schedule control's window
    await controller.async_request_amps(10)
    await controller.async_tick(at(minutes=1), CHARGING)
    assert await controller.async_abandon()
    await controller.async_request_charge(True)
    await controller.async_tick(at(minutes=2), CHARGING)
    assert controller.journal.fault.code == "own_window_present"
    assert await controller.async_force_restore(at(minutes=3))
    assert provider.schedules == OWNER


async def test_force_restore_refuses_a_stale_archive_over_a_live_owner_list():
    controller, provider, _ = make()
    await start(controller)
    provider.schedules[0]["amperage"] = 40
    await controller.async_request_amps(10)
    await controller.async_tick(at(minutes=1), CHARGING)
    assert await controller.async_abandon()
    # Later the owner sets up new schedules, and a takeover hits an unsupported entry.
    new_owner = [OWNER[0] | {"amperage": 24}, OWNER[0] | {"weekDays": ["Someday"]}]
    provider.schedules = deepcopy(new_owner)
    await controller.async_request_charge(True)
    await controller.async_tick(at(minutes=24 * 60), CHARGING)
    assert controller.journal.fault.code == "unsupported_schedule"
    assert await controller.async_force_restore(at(minutes=24 * 60 + 1)) is False
    assert provider.schedules == new_owner


async def test_forced_tick_ignores_the_backoff():
    controller, provider, _ = make()
    await start(controller)
    provider.get_error = ScheduleApiError("502")
    await controller.async_request_amps(10)
    await controller.async_tick(at(seconds=31), CHARGING)
    provider.get_error = None
    await controller.async_request_release("integration disabled", hand_back=False)
    await controller.async_tick(at(seconds=33), Observation(), force=True)
    assert provider.schedules == OWNER


async def test_failed_request_save_raises_and_keeps_the_request():
    # Saves are queued in order, so a later one already holds the request; undoing
    # it in memory would only make memory and disk disagree.
    controller, _, store = make()
    store.fail = True
    with pytest.raises(JournalSaveError):
        await controller.async_request_charge(True)
    assert controller.journal.command_received is True
    assert controller.journal.desired_charge is True
    store.fail = False
    assert await controller.async_request_charge(True)  # repeated, so saved now
    assert store.data["desired_charge"] is True


async def test_failed_request_save_keeps_the_journal_a_tick_is_using():
    controller, _, store = make()
    await start(controller)
    journal, lease = controller.journal, controller.journal.lease
    store.fail = True
    with pytest.raises(JournalSaveError):
        await controller.async_request_amps(10)
    assert controller.journal is journal and journal.lease is lease
    assert journal.requested_amps == 10


async def test_repeated_request_after_a_failed_save_is_saved_again():
    # The controller repeats a stop whose save failed: it is not acknowledged unsaved.
    controller, provider, store = make()
    await start(controller)
    store.fail = True
    with pytest.raises(JournalSaveError):
        await controller.async_request_charge(False)
    with pytest.raises(JournalSaveError):
        await controller.async_request_charge(False)
    store.fail = False
    saves = store.saves
    assert await controller.async_request_charge(False)
    assert store.saves == saves + 1
    assert store.data["desired_charge"] is False
    await controller.async_tick(at(seconds=10), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 8  # the stop stands
    saves = store.saves
    assert await controller.async_request_charge(False)
    assert store.saves == saves  # saved before: acknowledged without a save


async def test_snapshot_explains_what_blocks_a_start():
    controller, _, _ = make()
    await start(controller)
    await controller.async_set_renewal(False)
    await controller.async_tick(at(minutes=1), Observation(plugged=True, home=None))
    assert controller.snapshot(at(minutes=1))["start_blocked_by"] == [
        "vehicle_state_unknown",
        "renewal_off",
    ]


async def test_failed_journal_save_sends_nothing():
    controller, provider, store = make()
    await controller.async_request_charge(True)
    store.fail = True
    await controller.async_tick(T0, PLUGGED)
    assert provider.sets == []
    assert "journal" in controller.last_error


async def test_request_save_failure_raises():
    controller, _, store = make()
    store.fail = True
    with pytest.raises(JournalSaveError):
        await controller.async_request_charge(True)


# DST ---------------------------------------------------------------------------------


async def test_fall_back_does_not_block_a_stop():
    before = datetime(2026, 11, 1, 1, 59, 30, tzinfo=CHICAGO)  # CDT
    after = datetime(2026, 11, 1, 1, 0, 30, fold=1, tzinfo=CHICAGO)  # CST, 1 minute later
    assert (after.astimezone(UTC) - before.astimezone(UTC)) == timedelta(minutes=1)
    controller, provider, _ = make(time_zone=CHICAGO)
    await start(controller, now=before)
    await controller.async_request_charge(False)
    await controller.async_tick(after, PLUGGED)
    assert len(provider.sets) == 2
    assert controller.state(after) is ControlState.PAUSED


async def test_parked_window_is_rewritten_after_fall_back():
    parked_at = datetime(2026, 11, 1, 1, 30, tzinfo=CHICAGO)  # CDT
    controller, provider, _ = make(time_zone=CHICAGO)
    await controller.async_request_charge(False)
    await controller.async_tick(parked_at, PLUGGED)
    [first] = provider.sets[0]
    assert first["startTime"] == 65  # 01:05-01:25 CDT, which recurs after fall-back
    after = datetime(2026, 11, 1, 1, 0, fold=1, tzinfo=CHICAGO)  # CST
    await controller.async_tick(after, PLUGGED)
    [second] = provider.sets[-1]
    assert len(provider.sets) == 2
    assert second["startTime"] + second["duration"] == 55  # ended 00:55, before now


# Errors ------------------------------------------------------------------------------


async def test_rate_limit_backs_off():
    controller, provider, _ = make()
    provider.get_error = RateLimitedError("slow down")
    await start(controller)
    gets = provider.gets
    await controller.async_tick(at(minutes=2), PLUGGED)
    assert provider.gets == gets
    provider.get_error = None
    await controller.async_tick(at(minutes=6), PLUGGED)
    assert controller.state(T0) is ControlState.LEASE_ACTIVE


async def test_account_lock_backs_off_longer():
    controller, provider, _ = make()
    provider.get_error = RateLimitedError("locked", timedelta(minutes=30))
    await start(controller)
    gets = provider.gets
    await controller.async_tick(at(minutes=10), PLUGGED)
    assert provider.gets == gets


async def test_generic_errors_back_off_exponentially():
    controller, provider, _ = make()
    provider.get_error = ScheduleApiError("502")
    await start(controller)
    calls = []
    for second in range(0, 300, 5):
        before = provider.gets
        await controller.async_tick(at(seconds=second), PLUGGED)
        if provider.gets > before:
            calls.append(second)
    assert calls == [15, 45, 105, 225]


async def test_auth_error_propagates():
    controller, provider, _ = make()
    provider.get_error = AuthError("login")
    with pytest.raises(AuthError):
        await start(controller)


# Dry run and misc ----------------------------------------------------------------------


async def test_dry_run_never_writes_and_reports_target():
    controller, provider, _ = make(dry_run=True)
    await start(controller, amps=20)
    assert provider.gets == 0 and provider.sets == []
    assert controller.state(T0) is ControlState.DRY_RUN
    target = controller.snapshot(T0)["dry_run_target"]
    assert target["amperage"] == 20 and "location" not in target


async def test_enabling_dry_run_releases_existing_ownership():
    store = FakeStore()
    controller, provider, _ = make(store=store)
    await start(controller)
    dry = RivianScheduleController(
        provider, store, ScheduleControlConfig(home_location=HOME, time_zone=CDT, dry_run=True)
    )
    await dry.async_load()
    await dry.async_tick(at(minutes=1), CHARGING)
    assert provider.schedules == OWNER
    assert dry.journal.phase is Phase.IDLE


async def test_empty_owner_list_is_restored_as_empty():
    controller, provider, _ = make([])
    await start(controller)
    assert provider.sets[0][0]["location"] == HOME
    await controller.async_request_release("manual")
    await controller.async_tick(at(minutes=1), CHARGING)
    assert provider.sets[-1] == []
    assert controller.journal.phase is Phase.IDLE


async def test_unsupported_provider_schedule_faults_before_any_write():
    controller, provider, _ = make([OWNER[0] | {"weekDays": ["Someday"]}])
    await start(controller)
    assert controller.journal.fault.code == "unsupported_schedule"
    assert provider.sets == []


async def test_parked_window_is_refreshed_before_it_recurs():
    controller, provider, _ = make()
    await controller.async_request_charge(False)
    await controller.async_tick(T0, PLUGGED)
    await controller.async_tick(at(minutes=5 * 24 * 60), PLUGGED)
    assert len(provider.sets) == 1
    await controller.async_tick(at(minutes=6 * 24 * 60 + 1), PLUGGED)
    assert len(provider.sets) == 2


async def test_snapshot_has_no_coordinates_or_full_fingerprints():
    controller, _, _ = make()
    await start(controller)
    snapshot = controller.snapshot(T0)
    text = repr(snapshot)
    assert "latitude" not in text and "30.0" not in text and "97" not in text
    assert len(snapshot["owned_fingerprint"]) == 12


# Free hours: one fixed window per night ---------------------------------------------------

LA = ZoneInfo("America/Los_Angeles")
# The free hours, 20:00-06:00 local time; the planner gives their end as its until time.
DUSK = datetime(2026, 10, 23, 20, 0, tzinfo=LA)  # Friday
DAWN = datetime(2026, 10, 24, 6, 0, tzinfo=LA)  # Saturday
NIGHT_MINUTES = 10 * 60


def night(until: datetime | None = DAWN, **facts: Any) -> Observation:
    """The planner up and wanting charging, its until entity at ``until`` (read in UTC)."""
    values: dict[str, Any] = {
        "plugged": True,
        "home": True,
        "charging": True,
        "controller_alive": True,
        "controller_charging": True,
        "controller_until": until.astimezone(UTC) if until is not None else None,
    }
    return Observation(**(values | facts))


NIGHT = night()
# The planner looks down: its heartbeat and charging entity unavailable, its until unknown.
PLANNER_DOWN = night(None, controller_alive=False, controller_charging=None)
# After the free hours, no sun yet: the planner has cleared its until and paused the car.
MORNING = night(None, charging=False, controller_charging=False)


def ends_at(window: dict[str, Any]) -> int:
    """Where a window ends on the vehicle's clock, in minutes after midnight."""
    return (window["startTime"] + window["duration"]) % (24 * 60)


async def start_night(
    **config: Any,
) -> tuple[RivianScheduleController, FakeProvider, FakeStore]:
    """20:00: the planner asks for 48 A through the free hours."""
    controller, provider, store = make(time_zone=LA, **config)
    await start(controller, amps=48, obs=NIGHT, now=DUSK)
    return controller, provider, store


async def test_free_hours_get_one_full_current_window_to_their_end():
    controller, provider, _ = await start_night()
    assert len(provider.sets) == 1
    [window] = provider.sets[0]
    assert window == {
        "weekDays": ["Friday"],
        "startTime": 19 * 60 + 55,  # the clock skew before now
        "duration": 10 * 60 + 5,  # across midnight to 06:00 sharp, no padding at the end
        "location": {"latitude": 30.00001, "longitude": -97.00001},
        "amperage": 48,
        "enabled": True,
    }
    lease = controller.journal.lease
    assert lease.fixed is True and lease.amps == 48
    assert lease.expires_at == DAWN.astimezone(UTC)
    assert controller.state(DUSK) is ControlState.LEASE_ACTIVE
    assert controller.snapshot(DUSK)["lease_fixed"] is True


async def test_fixed_window_is_never_renewed_through_the_night():
    controller, provider, _ = await start_night()
    for minute in range(1, NIGHT_MINUTES):  # every minute to 05:59
        if minute % 10 == 0:  # the planner repeats its unchanged request
            await controller.async_request_amps(48)
            await controller.async_request_charge(True)
        await controller.async_tick(at(minutes=minute, base=DUSK), NIGHT)
    assert len(provider.sets) == 1
    assert controller.state(at(minutes=NIGHT_MINUTES - 1, base=DUSK)) is ControlState.LEASE_ACTIVE


async def test_planner_down_at_night_neither_parks_the_window_nor_writes_when_back():
    controller, provider, _ = await start_night()
    # 02:40-03:00: Planning turned off, far longer than the liveness grace.
    for minute in range(400, 420):
        await controller.async_tick(at(minutes=minute, base=DUSK), PLANNER_DOWN)
    snapshot = controller.snapshot(at(minutes=419, base=DUSK))
    assert snapshot["request_held"] is True
    assert snapshot["start_blocked_by"] == ["controller_unavailable"]
    assert controller.state(at(minutes=419, base=DUSK)) is ControlState.LEASE_ACTIVE
    # Back on, then on through 06:00.
    for minute in range(420, NIGHT_MINUTES + 10):
        await controller.async_tick(at(minutes=minute, base=DUSK), NIGHT)
    assert len(provider.sets) == 1


async def test_fixed_window_ending_while_the_planner_looks_down_writes_nothing():
    controller, provider, _ = await start_night()
    for minute in range(9 * 60 + 30, 11 * 60):  # 05:30-06:59, the planner down
        await controller.async_tick(at(minutes=minute, base=DUSK), PLANNER_DOWN)
    assert len(provider.sets) == 1
    assert controller.state(at(minutes=11 * 60, base=DUSK)) is ControlState.PAUSED
    # Back up by day, not wanting charging: its old request is dropped, nothing is parked.
    await controller.async_tick(at(minutes=11 * 60, base=DUSK), MORNING)
    assert controller.journal.command_received is False
    assert len(provider.sets) == 1


async def test_turning_charging_off_parks_the_fixed_window():
    controller, provider, _ = await start_night()
    await controller.async_request_charge(False)
    await controller.async_tick(at(minutes=3 * 60, base=DUSK), NIGHT)  # 23:00
    assert len(provider.sets) == 2
    [parked] = provider.sets[-1]
    assert parked["amperage"] == 8
    assert parked["startTime"] + parked["duration"] == 22 * 60 + 55  # ended before now
    lease = controller.journal.lease
    assert lease.active is False and lease.fixed is False
    assert controller.state(at(minutes=3 * 60, base=DUSK)) is ControlState.PAUSED


async def test_unplugging_at_night_parks_the_fixed_window_and_restores_the_owner_list():
    controller, provider, _ = await start_night()
    unplugged = night(plugged=False, charging=False)
    await controller.async_tick(at(minutes=60, base=DUSK), unplugged)
    assert len(provider.sets) == 2 and provider.sets[-1][0]["amperage"] == 8  # parked at once
    await controller.async_tick(at(minutes=61, base=DUSK), unplugged)
    assert provider.schedules == OWNER
    assert controller.journal.phase is Phase.IDLE


async def test_ended_fixed_window_writes_nothing_nor_restarts_on_the_same_request():
    controller, provider, _ = await start_night()
    # 05:59-06:15 every 15 s, past where a lapsed lease would be parked: the planner's
    # 06:00 cycle has not run, so it still asks for charging until 06:00.
    for second in range((NIGHT_MINUTES - 1) * 60, (NIGHT_MINUTES + 15) * 60, 15):
        await controller.async_tick(at(seconds=second, base=DUSK), NIGHT)
    assert len(provider.sets) == 1
    lease = controller.journal.lease
    assert lease.active is True and lease.fixed is True
    assert lease.expires_at == DAWN.astimezone(UTC)


async def test_ended_fixed_window_shows_paused_not_lapsed():
    controller, _, _ = await start_night()
    assert controller.state(DAWN - timedelta(seconds=1)) is ControlState.LEASE_ACTIVE
    assert controller.state(DAWN) is ControlState.PAUSED
    assert controller.state(DAWN + timedelta(hours=3)) is ControlState.PAUSED
    assert controller.snapshot(DAWN)["lease_fixed"] is True
    # A lease that was not renewed, by contrast, lapsed.
    leased, _, _ = make(time_zone=LA)
    await start(leased, amps=48, obs=night(None), now=DUSK)
    assert leased.state(at(minutes=20, base=DUSK)) is ControlState.LEASE_LAPSED


async def test_new_request_after_the_free_hours_starts_a_normal_lease():
    controller, provider, _ = await start_night()
    await controller.async_request_charge(False)  # the planner's 06:00 cycle
    for minute in range(NIGHT_MINUTES, NIGHT_MINUTES + 3 * 60, 5):  # 06:00-08:55
        await controller.async_tick(at(minutes=minute, base=DUSK), MORNING)
    assert len(provider.sets) == 1  # the ended window needs no park
    # 09:00: sunshine; the planner asks for 16 A, with no until.
    nine = at(minutes=13 * 60, base=DUSK)
    await controller.async_request_amps(16)
    await controller.async_request_charge(True)
    await controller.async_tick(nine, night(None))
    assert len(provider.sets) == 2
    [window] = provider.sets[-1]
    assert (window["weekDays"], window["startTime"], window["duration"]) == (
        ["Saturday"],
        8 * 60 + 55,
        30,
    )
    assert window["amperage"] == 16
    lease = controller.journal.lease
    assert lease.fixed is False
    assert lease.expires_at == nine.astimezone(UTC) + timedelta(minutes=20)
    await controller.async_tick(nine + timedelta(minutes=12), night(None))
    assert len(provider.sets) == 3  # and renewed like any lease


async def test_ended_fixed_window_is_reparked_before_it_recurs():
    controller, provider, _ = await start_night()
    await controller.async_request_charge(False)  # the planner's 06:00 cycle
    six_days = 6 * 24 * 60
    for minute in [*range(NIGHT_MINUTES, six_days, 60), six_days - 1]:
        await controller.async_tick(at(minutes=minute, base=DUSK), MORNING)
    assert len(provider.sets) == 1
    # Thursday 20:00, six days after it was written; it would recur on Friday at 19:55.
    await controller.async_tick(at(minutes=six_days, base=DUSK), MORNING)
    assert len(provider.sets) == 2
    [parked] = provider.sets[-1]
    assert parked["weekDays"] == ["Thursday"] and parked["amperage"] == 8
    assert parked["startTime"] + parked["duration"] == 19 * 60 + 55
    assert controller.journal.lease.active is False


async def test_running_lease_becomes_fixed_on_an_increase_when_the_free_hours_begin():
    controller, provider, _ = make(time_zone=LA)
    await start(controller, amps=16, obs=night(None), now=at(minutes=-10, base=DUSK))  # 19:50
    assert controller.journal.lease.fixed is False
    # 20:00: the planner gives the end of the free hours, then asks for 48 A.
    await controller.async_request_amps(48)
    await controller.async_tick(DUSK, NIGHT)
    assert len(provider.sets) == 2
    [window] = provider.sets[-1]
    assert (window["startTime"], window["duration"], window["amperage"]) == (
        19 * 60 + 55,
        10 * 60 + 5,
        48,
    )
    lease = controller.journal.lease
    assert lease.fixed is True and lease.expires_at == DAWN.astimezone(UTC)
    for minute in range(1, NIGHT_MINUTES + 10):
        await controller.async_tick(at(minutes=minute, base=DUSK), NIGHT)
    assert len(provider.sets) == 2


async def test_running_lease_becomes_fixed_on_its_renewal_when_the_free_hours_begin():
    controller, provider, _ = make(time_zone=LA)
    # 19:50: already at 48 A, a lease to 20:10.
    await start(controller, amps=48, obs=night(None), now=at(minutes=-10, base=DUSK))
    await controller.async_tick(DUSK, NIGHT)
    await controller.async_tick(at(minutes=1, base=DUSK), NIGHT)
    assert len(provider.sets) == 1  # nothing to change until the lease is due for renewal
    await controller.async_tick(at(minutes=2, base=DUSK), NIGHT)  # 8 minutes left
    assert len(provider.sets) == 2
    [window] = provider.sets[-1]
    assert (window["startTime"], window["duration"]) == (19 * 60 + 57, 10 * 60 + 3)
    assert ends_at(window) == 6 * 60 and window["amperage"] == 48
    assert controller.journal.lease.fixed is True
    for minute in range(3, NIGHT_MINUTES + 10):
        await controller.async_tick(at(minutes=minute, base=DUSK), NIGHT)
    assert len(provider.sets) == 2


# While the window runs, the planner may still give its end, or (the free hours edited) a
# later one; either way a rewrite keeps the end the window was written with.
UNTIL_KEPT_OR_MOVED = pytest.mark.parametrize(
    "later", [timedelta(0), timedelta(hours=1)], ids=["until_kept", "until_moved"]
)


@UNTIL_KEPT_OR_MOVED
async def test_current_changes_keep_the_fixed_end(later):
    controller, provider, _ = await start_night()
    obs = night(DAWN + later)
    await controller.async_request_amps(32)
    await controller.async_tick(at(minutes=3 * 60, base=DUSK), obs)  # 23:00
    [lower] = provider.sets[-1]
    assert lower["amperage"] == 32
    assert (lower["startTime"], ends_at(lower)) == (22 * 60 + 55, 6 * 60)
    await controller.async_request_amps(48)
    await controller.async_tick(at(minutes=3 * 60 + 5, base=DUSK), obs)
    [higher] = provider.sets[-1]
    assert higher["amperage"] == 48
    assert (higher["startTime"], ends_at(higher)) == (23 * 60, 6 * 60)
    lease = controller.journal.lease
    assert lease.fixed is True and lease.expires_at == DAWN.astimezone(UTC)
    for minute in range(3 * 60 + 6, NIGHT_MINUTES):
        await controller.async_tick(at(minutes=minute, base=DUSK), obs)
    assert len(provider.sets) == 3


async def test_free_hours_moved_later_get_a_window_of_their_own_when_the_first_ends():
    controller, provider, _ = await start_night()
    later = night(DAWN + timedelta(hours=1))  # 23:00: the free hours now end at 07:00
    for minute in range(3 * 60, NIGHT_MINUTES):
        await controller.async_tick(at(minutes=minute, base=DUSK), later)
    assert len(provider.sets) == 1  # the running window is left alone
    await controller.async_tick(DAWN, later)
    assert len(provider.sets) == 2
    [window] = provider.sets[-1]
    assert (window["startTime"], ends_at(window)) == (5 * 60 + 55, 7 * 60)
    lease = controller.journal.lease
    assert lease.fixed is True
    assert lease.expires_at == (DAWN + timedelta(hours=1)).astimezone(UTC)


@UNTIL_KEPT_OR_MOVED
@pytest.mark.parametrize(
    ("dusk", "dawn", "rewrite_start"),
    [
        # Fall back: rewritten at the second 01:00 (PST), from 00:55.
        (datetime(2026, 10, 31, 20, 0, tzinfo=LA), datetime(2026, 11, 1, 6, 0, tzinfo=LA), 55),
        # Spring forward: rewritten at 03:00 (PDT), from 02:55 on the wall clock.
        (
            datetime(2027, 3, 13, 20, 0, tzinfo=LA),
            datetime(2027, 3, 14, 6, 0, tzinfo=LA),
            2 * 60 + 55,
        ),
    ],
    ids=["fall_back", "spring_forward"],
)
async def test_clock_change_rewrite_keeps_the_fixed_end(dusk, dawn, rewrite_start, later):
    controller, provider, _ = make(time_zone=LA)
    await start(controller, amps=48, obs=night(dawn), now=dusk)
    offset = controller.journal.lease.utc_offset_minutes
    obs = night(dawn + later)
    begin, end = dusk.astimezone(UTC), dawn.astimezone(UTC)
    for minute in range(1, (end - begin) // timedelta(minutes=1)):  # every real minute to 05:59
        await controller.async_tick(begin + timedelta(minutes=minute), obs)
    assert len(provider.sets) == 2  # the window, then one rewrite when the clocks change
    [first], [second] = provider.sets
    assert ends_at(first) == ends_at(second) == 6 * 60
    assert second["startTime"] == rewrite_start and second["amperage"] == 48
    lease = controller.journal.lease
    assert lease.fixed is True and lease.expires_at == end
    assert lease.utc_offset_minutes != offset


@pytest.mark.parametrize(
    "until",
    [DUSK - timedelta(hours=1), DUSK, DUSK + timedelta(seconds=30)],
    ids=["past", "now", "within_the_minute"],
)
async def test_until_not_after_now_starts_a_normal_lease(until):
    controller, provider, _ = make(time_zone=LA)
    await start(controller, amps=48, obs=night(until), now=DUSK)
    [window] = provider.sets[0]
    assert (window["startTime"], window["duration"]) == (19 * 60 + 55, 30)
    lease = controller.journal.lease
    assert lease.fixed is False
    assert lease.expires_at == DUSK.astimezone(UTC) + timedelta(minutes=20)
    assert controller.snapshot(DUSK)["lease_fixed"] is False


async def test_free_hours_over_a_day_are_cut_to_a_day_and_the_rest_written_after():
    controller, provider, _ = make(time_zone=LA)
    until = DUSK + timedelta(days=2)  # Sunday 20:00
    await start(controller, amps=48, obs=night(until), now=DUSK)
    [window] = provider.sets[0]
    assert (window["weekDays"], window["startTime"], window["duration"]) == (
        ["Friday"],
        19 * 60 + 55,
        24 * 60,
    )
    cut = DUSK.astimezone(UTC) + timedelta(minutes=24 * 60 - 5)  # Saturday 19:55
    assert controller.journal.lease.expires_at == cut
    for minute in range(10, 24 * 60 - 5, 10):
        await controller.async_tick(at(minutes=minute, base=DUSK), night(until))
    assert len(provider.sets) == 1
    # At the cut the free hours go on: the next start writes the rest, again at most a day.
    await controller.async_tick(cut, night(until))
    assert len(provider.sets) == 2
    [rest] = provider.sets[-1]
    assert (rest["weekDays"], rest["startTime"], rest["duration"]) == (
        ["Saturday"],
        19 * 60 + 50,
        24 * 60,
    )
    assert controller.journal.lease.fixed is True


@pytest.mark.parametrize("hours", [48, 23.9], ids=["cut_to_a_day", "under_a_day"])
async def test_long_free_hours_across_spring_forward_get_a_window(hours):
    dusk = datetime(2027, 3, 13, 20, 0, tzinfo=LA)  # Saturday, PST
    controller, provider, _ = make(time_zone=LA)
    until = dusk.astimezone(UTC) + timedelta(hours=hours)
    await start(controller, amps=48, obs=night(until), now=dusk)
    assert len(provider.sets) == 1
    assert provider.sets[0][0]["duration"] <= 24 * 60
    assert controller.journal.lease.fixed is True


def test_lease_dict_keeps_fixed_and_an_older_lease_loads_as_not_fixed():
    lease = Lease(True, 48, DUSK.astimezone(UTC), DAWN.astimezone(UTC), -420, fixed=True)
    assert lease.to_dict()["fixed"] is True
    assert Lease.from_dict(lease.to_dict()) == lease
    older = lease.to_dict()
    del older["fixed"]  # saved before fixed windows existed
    assert Lease.from_dict(older) == replace(lease, fixed=False)


async def test_restart_at_night_keeps_the_fixed_window_unrenewed():
    controller, provider, store = await start_night()
    assert store.data["lease"]["fixed"] is True
    restarted = await restarted_after(controller, provider, store, 3 * 60, NIGHT, base=DUSK)
    assert restarted.journal.lease.fixed is True
    for minute in range(3 * 60 + 1, NIGHT_MINUTES + 10):
        await restarted.async_tick(at(minutes=minute, base=DUSK), NIGHT)
    assert len(provider.sets) == 1


async def test_journal_saved_before_fixed_windows_keeps_renewing_its_lease():
    controller, provider, store = make()
    await start(controller, amps=48, obs=ALIVE)
    del store.data["lease"]["fixed"]
    restarted = await restarted_after(controller, provider, store, 12, ALIVE)
    assert restarted.journal.lease.fixed is False
    assert len(provider.sets) == 2  # renewed, as before


# Window location ---------------------------------------------------------------------

# About 80 m north of HOME, and the vehicle's GPS fix in the driveway, about 11 m away.
NEAR_HOME = {"latitude": 30.00072, "longitude": -97.0}
DRIVEWAY = {"latitude": 30.0001, "longitude": -97.0}


def located(location: dict[str, float] | None = DRIVEWAY, **facts: Any) -> Observation:
    """Plugged in at home, with the vehicle's tracker at ``location``."""
    return Observation(
        **({"plugged": True, "home": True, "charging": False} | facts), location=location
    )


async def test_window_is_written_at_the_vehicles_position_while_plugged_in_at_home():
    controller, provider, store = make()
    await start(controller, obs=located())
    assert provider.sets[0][0]["location"] == DRIVEWAY
    snapshot = controller.snapshot(T0)
    assert snapshot["window_location_source"] == "vehicle"
    assert snapshot["window_location_distance_m"] == 0
    # The vehicle's coordinates are reported only as that distance.
    assert snapshot["observation"]["location"] is None
    assert "30.0001" not in repr(snapshot)
    assert store.data["window_location_source"] == "vehicle"


async def test_owner_schedule_beyond_150_m_is_not_taken_as_home():
    far = deepcopy(OWNER)
    far[0]["location"] = {"latitude": 30.002, "longitude": -97.0}  # about 220 m
    controller, provider, _ = make(far)
    await start(controller)
    assert provider.sets[0][0]["location"] == HOME
    assert controller.snapshot(T0)["window_location_source"] == "home"


async def test_nearest_owner_schedule_within_150_m_is_used_without_a_vehicle_position():
    owner = deepcopy(OWNER)
    owner[1]["location"] = dict(NEAR_HOME)
    owner[0], owner[1] = owner[1], owner[0]
    controller, provider, _ = make(owner)
    await start(controller)
    assert provider.sets[0][0]["location"] == {"latitude": 30.00001, "longitude": -97.00001}
    assert controller.snapshot(T0)["window_location_source"] == "owner_schedule"


@pytest.mark.parametrize(
    "position",
    [
        {"latitude": float("nan"), "longitude": -97.0},
        {"latitude": 91.0, "longitude": -97.0},
        {"latitude": True, "longitude": -97.0},
        {"latitude": 30.0},
    ],
)
async def test_an_invalid_vehicle_position_is_never_written(position):
    controller, provider, _ = make([OWNER[1]])
    await start(controller, obs=located(position))
    assert provider.sets[0][0]["location"] == HOME


async def test_vehicle_position_away_from_home_is_not_used():
    controller, provider, _ = make()
    await controller.async_request_charge(False)
    await controller.async_tick(T0, PLUGGED)
    # Unplugged and parked elsewhere: the parked window stays at the home location.
    await controller.async_request_charge(True)
    await controller.async_tick(
        at(seconds=6), located({"latitude": 31.0, "longitude": -96.0}, plugged=False)
    )
    assert all(
        window["location"] == {"latitude": 30.00001, "longitude": -97.00001}
        for windows in provider.sets
        for window in windows
    )


async def test_window_moves_to_the_vehicle_once_its_position_is_known_and_warns_once(caplog):
    controller, provider, _ = make()
    await start(controller, amps=16)
    assert controller.snapshot(T0)["window_location_distance_m"] is None
    far_fix = {"latitude": 30.0009, "longitude": -97.0}  # about 100 m from the owner's pin
    with caplog.at_level("WARNING"):
        await controller.async_tick(at(seconds=20), located(far_fix, charging=True))
        await controller.async_tick(at(seconds=25), located(far_fix, charging=True))
    warnings = [
        r for r in caplog.records if "from the location of the charging windows" in r.message
    ]
    assert len(warnings) == 1
    assert "30.0009" not in caplog.text
    assert controller.snapshot(at(seconds=25))["window_location_distance_m"] == 99
    # The next write is at the vehicle's position.
    await controller.async_request_amps(10)
    await controller.async_tick(at(seconds=40), located(far_fix, charging=True))
    assert provider.sets[-1][0]["location"] == far_fix
    snapshot = controller.snapshot(at(seconds=40))
    assert snapshot["window_location_source"] == "vehicle"
    assert snapshot["window_location_distance_m"] == 0


async def test_window_location_is_kept_across_a_restart_and_cleared_on_release():
    controller, provider, store = make()
    await start(controller, obs=located())
    restarted, _, _ = make(store=store)
    restarted._api = provider
    assert await restarted.async_load()
    assert restarted.journal.window_location == DRIVEWAY
    await restarted.async_request_release("manual")
    await restarted.async_tick(at(seconds=10), PLUGGED)
    assert provider.schedules == OWNER
    assert store.data["window_location"] is None
    assert store.data["window_location_source"] is None


# Overlapping owner schedules ---------------------------------------------------------

OVERLAPPING = [
    OWNER[0],
    {
        "weekDays": ["Friday", "Saturday", "Sunday"],
        "startTime": 1320,
        "duration": 480,
        "location": {"latitude": 30.00001, "longitude": -97.00001},
        "amperage": 40,
        "enabled": False,
    },
]


async def test_overlapping_owner_schedules_are_reported_before_the_first_write(caplog):
    controller, provider, store = make(OVERLAPPING)
    with caplog.at_level("WARNING"):
        await start(controller)
    assert provider.sets == []
    assert store.data["phase"] == "owned"  # the snapshot is saved
    assert controller.owner_overlap_days == ["Friday"]
    assert controller.snapshot(T0)["owner_schedules_overlap_days"] == ["Friday"]
    assert "share weekdays (Friday)" in caplog.text
    # The next tick writes the window.
    await controller.async_tick(at(seconds=15), PLUGGED)
    assert len(provider.sets) == 1
    assert controller.state(at(seconds=15)) is ControlState.LEASE_ACTIVE


async def test_owner_schedules_without_shared_weekdays_are_written_at_once():
    controller, provider, _ = make()
    await start(controller)
    assert controller.owner_overlap_days == []
    assert len(provider.sets) == 1


async def test_overlap_is_known_again_after_a_restart():
    controller, provider, store = make(OVERLAPPING)
    await start(controller)
    restarted, _, _ = make(store=store)
    assert await restarted.async_load()
    assert restarted.owner_overlap_days == ["Friday"]


async def rejected_restore(**config: Any):
    """Own an overlapping list, then let Rivian refuse its full list on the release."""
    controller, provider, store = make(OVERLAPPING, **config)
    await start(controller)
    await controller.async_tick(at(seconds=15), PLUGGED)
    full = deepcopy(OVERLAPPING)

    async def refuse_full_list():
        if provider.sets[-1] == full:
            provider.apply_set = False
            provider.set_error = WriteRejectedError("bad", invalid_input=True)
        else:
            provider.apply_set = True
            provider.set_error = None

    provider.on_set = refuse_full_list
    await controller.async_request_release("manual")
    await controller.async_tick(at(minutes=1), PLUGGED)
    return controller, provider, store


async def test_restore_rejected_without_the_option_faults():
    controller, provider, _ = await rejected_restore()
    assert controller.journal.fault.code == "restore_rejected"
    assert controller.journal.original == OVERLAPPING


async def test_rejected_restore_writes_the_enabled_schedules_when_opted_in():
    controller, provider, store = await rejected_restore(restore_enabled_only=True)
    assert controller.journal.phase is Phase.OWNED
    assert controller.journal.restore_enabled_only_active is True
    assert controller.snapshot(at(minutes=1))["restore_enabled_only_active"] is True
    # After the error backoff, the release writes the enabled entries only.
    await controller.async_tick(at(minutes=2), PLUGGED)
    assert provider.sets[-1] == [OVERLAPPING[0]]
    assert provider.schedules == [OVERLAPPING[0]]
    assert controller.journal.phase is Phase.IDLE
    # The full list stays archived.
    assert store.data["archived_original"] == OVERLAPPING
    assert store.data["restore_enabled_only_active"] is False


async def test_enabled_only_restore_rejected_too_faults():
    controller, provider, _ = await rejected_restore(restore_enabled_only=True)
    provider.on_set = None
    provider.apply_set = False
    provider.set_error = WriteRejectedError("bad", invalid_input=True)
    await controller.async_tick(at(minutes=2), PLUGGED)
    assert controller.journal.fault.code == "restore_rejected"


async def test_enabled_only_fallback_needs_disabled_entries():
    owner = deepcopy(OVERLAPPING)
    owner[1]["enabled"] = True
    controller, provider, _ = make(owner, restore_enabled_only=True)
    await start(controller)
    await controller.async_tick(at(seconds=15), PLUGGED)
    provider.set_error = WriteRejectedError("bad", invalid_input=True)
    provider.apply_set = False
    await controller.async_request_release("manual")
    await controller.async_tick(at(minutes=1), PLUGGED)
    assert controller.journal.fault.code == "restore_rejected"


# Read cadence ------------------------------------------------------------------------


async def test_a_settling_write_is_read_again_only_once_settled():
    controller, provider, _ = make()
    await start(controller, amps=20)
    provider.apply_set = False
    gets = provider.gets
    await controller.async_request_amps(10)
    await controller.async_tick(at(seconds=31), CHARGING)
    assert provider.gets - gets == 2  # the guard read, and right after sending
    assert controller.journal.phase is Phase.WRITING
    await controller.async_tick(at(seconds=46), CHARGING)
    await controller.async_tick(at(seconds=60), CHARGING)
    assert provider.gets - gets == 2  # no reads while it settles
    await controller.async_request_amps(20)  # so the settled tick writes nothing new
    await controller.async_tick(at(seconds=61), CHARGING)
    assert provider.gets - gets == 3
    assert controller.journal.phase is Phase.OWNED
    assert controller.journal.unapplied_writes == 1


async def test_reads_and_writes_are_counted_per_hour():
    controller, provider, _ = make()
    await start(controller)
    snapshot = controller.snapshot(T0)
    # The takeover read, the guard read and the readback; one write.
    assert snapshot["reads_last_hour"] == 3
    assert snapshot["writes_last_hour"] == 1
    await controller.async_request_charge(False)
    await controller.async_tick(at(seconds=6), PLUGGED)
    snapshot = controller.snapshot(at(seconds=6))
    assert (snapshot["reads_last_hour"], snapshot["writes_last_hour"]) == (5, 2)
    later = controller.snapshot(at(minutes=60, seconds=1))
    assert (later["reads_last_hour"], later["writes_last_hour"]) == (2, 1)
    assert controller.snapshot(at(minutes=61))["reads_last_hour"] == 0


async def test_operator_reads_are_counted():
    controller, provider, _ = make()
    await controller.async_read_provider()
    assert controller.snapshot(datetime.now(UTC))["reads_last_hour"] == 1


# Community pattern ---------------------------------------------------------------------


def community(**config: Any):
    return make(community_pattern=True, **config)


async def test_community_pattern_charges_on_one_all_week_window():
    controller, provider, _ = community()
    await start(controller, amps=17)
    assert provider.sets == [
        [
            {
                "weekDays": [
                    "Monday",
                    "Tuesday",
                    "Wednesday",
                    "Thursday",
                    "Friday",
                    "Saturday",
                    "Sunday",
                ],
                "startTime": 0,
                "duration": 1440,
                "location": {"latitude": 30.00001, "longitude": -97.00001},
                "amperage": 16,
                "enabled": True,
            }
        ]
    ]
    assert controller.state(T0) is ControlState.LEASE_ACTIVE
    assert controller.snapshot(T0)["lease_all_week"] is True
    # Never renewed: nothing is written while the current stays.
    for minutes in range(1, 120, 5):
        await controller.async_tick(at(minutes=minutes), CHARGING)
    assert len(provider.sets) == 1
    assert controller.state(at(minutes=120)) is ControlState.LEASE_ACTIVE


async def test_community_pattern_rewrites_only_when_the_current_changes():
    controller, provider, _ = community()
    await start(controller, amps=32)
    await controller.async_request_amps(20)
    await controller.async_tick(at(seconds=31), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 20
    assert provider.sets[-1][0]["duration"] == 1440
    await controller.async_request_amps(40)
    await controller.async_tick(at(seconds=70), CHARGING)
    assert len(provider.sets) == 2  # an increase waits for the increase interval
    await controller.async_tick(at(seconds=160), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 40
    assert len(provider.sets) == 3


async def test_community_pattern_parks_an_enabled_window_12_hours_away():
    controller, provider, _ = community()
    await start(controller, amps=32)
    await controller.async_request_charge(False)
    await controller.async_tick(at(seconds=6), PLUGGED)
    [parked] = provider.sets[-1]
    # 12:00 Saturday now: the window starts at 00:00 Sunday and lasts an hour.
    assert parked["weekDays"] == ["Sunday"]
    assert parked["startTime"] == 0
    assert parked["duration"] == 60
    assert parked["enabled"] is True
    assert parked["amperage"] == 8
    assert controller.state(at(seconds=6)) is ControlState.PAUSED
    # Pushed away again once its start comes within 2 hours, not before.
    await controller.async_tick(at(minutes=9 * 60 + 59), PLUGGED)
    assert len(provider.sets) == 2
    await controller.async_tick(at(minutes=10 * 60), PLUGGED)
    assert len(provider.sets) == 3
    [again] = provider.sets[-1]
    assert again["weekDays"] == ["Sunday"]
    assert again["startTime"] == 10 * 60
    assert again["enabled"] is True


async def test_community_pattern_stop_request_at_takeover_parks_ahead():
    controller, provider, _ = community()
    await controller.async_request_charge(False)
    await controller.async_tick(T0, PLUGGED)
    [parked] = provider.sets[-1]
    assert parked["duration"] == 60 and parked["startTime"] == 0


async def test_community_pattern_unknown_vehicle_state_parks_after_a_lease_length():
    controller, provider, _ = community()
    await start(controller, amps=32)
    await controller.async_tick(at(minutes=19), UNKNOWN)
    assert len(provider.sets) == 1
    await controller.async_tick(at(minutes=20), UNKNOWN)
    assert len(provider.sets) == 2
    assert provider.sets[-1][0]["duration"] == 60
    assert controller.state(at(minutes=20)) is ControlState.PAUSED


async def test_community_pattern_held_request_parks():
    controller, provider, _ = community()
    alive = replace(PLUGGED, controller_alive=True)
    down = replace(PLUGGED, controller_alive=False)
    await start(controller, amps=32, obs=alive)
    await controller.async_tick(at(minutes=1), down)
    assert len(provider.sets) == 1  # within the grace
    await controller.async_tick(at(minutes=11), down)
    assert provider.sets[-1][0]["duration"] == 60


async def test_community_pattern_keeps_one_fixed_window_for_the_free_hours():
    controller, provider, _ = await start_night(community_pattern=True)
    [window] = provider.sets[0]
    assert ends_at(window) == 6 * 60
    assert window["weekDays"] == ["Friday"]
    # Not parked while the planner looks down at night.
    await controller.async_tick(DUSK + timedelta(hours=2), PLANNER_DOWN)
    await controller.async_tick(DUSK + timedelta(hours=3), PLANNER_DOWN)
    assert len(provider.sets) == 1
    # After the free hours, with the planner no longer charging: parked ahead.
    await controller.async_tick(DAWN + timedelta(minutes=10), MORNING)
    [parked] = provider.sets[-1]
    assert parked["duration"] == 60 and parked["enabled"] is True


async def test_community_pattern_daytime_charging_after_the_free_hours_is_all_week():
    controller, provider, _ = await start_night(community_pattern=True)
    await controller.async_tick(DAWN + timedelta(minutes=10), night(None))
    assert provider.sets[-1][0]["duration"] == 1440


async def test_community_window_is_replaced_once_the_pattern_is_turned_off():
    controller, provider, store = community()
    await start(controller, amps=32)
    restarted, _, _ = make(store=store)
    restarted._api = provider
    await restarted.async_load()
    assert restarted.journal.lease.all_week is True
    await restarted.async_request_amps(32)
    await restarted.async_request_charge(True)
    await restarted.async_tick(at(seconds=10), CHARGING)
    [lease] = provider.sets[-1]
    assert lease["duration"] == 30
    assert restarted.journal.lease.all_week is False


async def test_community_parked_window_is_parked_normally_once_the_pattern_is_off():
    controller, provider, store = community()
    await controller.async_request_charge(False)
    await controller.async_tick(T0, PLUGGED)
    restarted, _, _ = make(store=store)
    restarted._api = provider
    await restarted.async_load()
    assert restarted.journal.lease.window_start is not None
    await restarted.async_tick(at(seconds=10), PLUGGED)
    [parked] = provider.sets[-1]
    assert parked["duration"] == 20
    assert restarted.journal.lease.window_start is None


async def test_community_windows_are_recognized_as_our_own_after_an_abandon():
    for charge in (True, False):
        controller, provider, _ = community()
        await start(controller, amps=32)
        if not charge:
            await controller.async_request_charge(False)
            await controller.async_tick(at(seconds=6), PLUGGED)
        provider.schedules[0]["amperage"] = 30  # the owner edits schedule control's window
        await controller.async_request_charge(not charge)  # the next write sees the edit
        await controller.async_tick(at(minutes=1), CHARGING)
        assert controller.journal.fault.code == "external_edit"
        assert await controller.async_abandon()
        await controller.async_request_charge(True)
        await controller.async_tick(at(minutes=2), CHARGING)
        assert controller.journal.fault.code == "own_window_present"


async def test_community_dry_run_target():
    controller, provider, _ = community(dry_run=True)
    await controller.async_request_amps(20)
    await controller.async_tick(T0, PLUGGED)
    target = controller.snapshot(T0)["dry_run_target"]
    assert target["duration"] == 1440 and target["amperage"] == 20
    assert "location" not in target
    await controller.async_request_charge(False)
    await controller.async_tick(at(seconds=5), PLUGGED)
    assert controller.snapshot(T0)["dry_run_target"]["duration"] == 60
    assert provider.sets == []


def test_community_leases_round_trip():
    lease = Lease(False, 8, T0.astimezone(UTC), None, -300, window_start=at(minutes=720))
    assert Lease.from_dict(lease.to_dict()) == lease
    on = Lease(True, 32, T0.astimezone(UTC), None, -300, all_week=True)
    assert Lease.from_dict(on.to_dict()) == on
