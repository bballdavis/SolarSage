"""Tests for the Rivian schedule ownership state machine."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.rivian_solar_adapter.api import (
    AuthError,
    RateLimitedError,
    ScheduleApiError,
    WriteRejectedError,
)
from custom_components.rivian_solar_adapter.controller import (
    AdapterConfig,
    ControlState,
    JournalSaveError,
    Observation,
    Phase,
    ReleaseResult,
    RivianScheduleController,
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


def make(
    schedules=OWNER, *, store: FakeStore | None = None, **config: Any
) -> tuple[RivianScheduleController, FakeProvider, FakeStore]:
    store = store or FakeStore()
    provider = FakeProvider(schedules, store)
    options = {"home_location": HOME, "time_zone": CDT, "dry_run": False} | config
    return RivianScheduleController(provider, store, AdapterConfig(**options)), provider, store


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
    assert len(journal_then["adapter_states"]) == 1
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


async def test_decrease_is_prompt_increase_keeps_a_margin():
    controller, provider, _ = make()
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
    controller, provider, _ = make()
    await start(controller, amps=16)
    await controller.async_request_amps(48)
    await controller.async_tick(at(minutes=3), CHARGING)
    assert provider.sets[-1][0]["amperage"] == 48


async def test_band_just_below_an_even_step_does_not_increase():
    # evcc asks 20 A for a car at 16 A whose real headroom is about 17.9 A.
    controller, provider, _ = make()
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
    assert provider.sets[-1][0]["amperage"] == 36


async def test_requests_hovering_near_the_lease_do_not_cycle():
    controller, provider, _ = make()
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
    await controller.async_tick(at(minutes=21), UNKNOWN)
    # The lapsed lease is parked so it cannot recur a week later at 32 A.
    [parked] = provider.sets[-1]
    assert len(provider.sets) == 2 and parked["amperage"] == 8
    assert controller.journal.phase is Phase.OWNED
    assert controller.state(at(minutes=21)) is ControlState.PAUSED
    await controller.async_tick(at(minutes=25), CHARGING)
    assert controller.state(at(minutes=25)) is ControlState.LEASE_ACTIVE  # resumes when known


async def test_evcc_down_clears_its_request_and_parks():
    controller, provider, _ = make()
    await start(controller, obs=Observation(plugged=True, home=True, controller_alive=True))
    dead = Observation(plugged=True, home=True, charging=True, controller_alive=False)
    await controller.async_tick(at(minutes=1), dead)
    assert controller.journal.command_received is False
    assert provider.sets[-1][0]["amperage"] == 8  # parked
    alive = Observation(plugged=True, home=True, charging=False, controller_alive=True)
    await controller.async_tick(at(minutes=30), alive)
    assert len(provider.sets) == 2  # no stale restart: evcc must ask again
    await controller.async_request_charge(True)
    await controller.async_tick(at(minutes=31), alive)
    assert controller.state(at(minutes=31)) is ControlState.LEASE_ACTIVE


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


async def test_takeover_refuses_the_adapters_own_window_and_force_restore_recovers():
    controller, provider, _ = make()
    await start(controller)
    lease_list = deepcopy(provider.schedules)
    provider.schedules[0]["amperage"] = 40
    await controller.async_request_amps(10)  # the next write's guard read sees the edit
    await controller.async_tick(at(minutes=1), CHARGING)
    assert await controller.async_abandon()
    # The owner puts the adapter's own window back by accident (or a late write lands).
    provider.schedules = lease_list
    await controller.async_request_charge(True)
    await controller.async_tick(at(minutes=2), CHARGING)
    assert controller.journal.fault.code == "adapter_window_present"
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

    async def evcc_meanwhile():
        provider.on_set = None
        await controller.async_request_charge(True)

    provider.on_set = evcc_meanwhile
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


async def test_owner_edited_adapter_window_is_refused_after_abandon():
    controller, provider, _ = make()
    await start(controller)
    provider.schedules[0]["amperage"] = 40  # the owner edits the adapter's window
    await controller.async_request_amps(10)
    await controller.async_tick(at(minutes=1), CHARGING)
    assert await controller.async_abandon()
    await controller.async_request_charge(True)
    await controller.async_tick(at(minutes=2), CHARGING)
    assert controller.journal.fault.code == "adapter_window_present"
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


async def test_failed_request_save_rolls_back():
    controller, _, store = make()
    store.fail = True
    with pytest.raises(JournalSaveError):
        await controller.async_request_charge(True)
    assert controller.journal.command_received is False
    assert controller.journal.desired_charge is None


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
        provider, store, AdapterConfig(home_location=HOME, time_zone=CDT, dry_run=True)
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
