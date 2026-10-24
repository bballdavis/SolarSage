"""Tests for the Rivian schedule ownership state machine."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

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
    Observation,
    Phase,
    RivianScheduleController,
)
from custom_components.rivian_solar_adapter.schedule import provider_hash

CDT = timezone(timedelta(hours=-5))
T0 = datetime(2026, 10, 24, 12, 0, tzinfo=CDT)
HOME = {"latitude": 30.0, "longitude": -97.0}
PLUGGED = Observation(plugged=True, home=True, charging=False)
CHARGING = Observation(plugged=True, home=True, charging=True)
UNPLUGGED = Observation(plugged=False, home=True, charging=False)
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
    """In-memory Rivian schedule provider with scriptable failures."""

    def __init__(self, schedules: list[dict[str, Any]], store: FakeStore | None = None):
        self.schedules = deepcopy(schedules)
        self.store = store
        self.sets: list[list[dict[str, Any]]] = []
        self.gets = 0
        self.set_error: Exception | None = None
        self.apply_set = True
        self.get_error: Exception | None = None
        self.journal_at_set: list[dict[str, Any] | None] = []

    async def get_schedules(self):
        self.gets += 1
        if self.get_error:
            raise self.get_error
        return deepcopy(self.schedules)

    async def set_schedules(self, schedules):
        self.sets.append(deepcopy(schedules))
        if self.store is not None:
            self.journal_at_set.append(deepcopy(self.store.data))
        if self.apply_set:
            self.schedules = deepcopy(schedules)
        if self.set_error:
            raise self.set_error


class FakeStore:
    def __init__(self, data: dict[str, Any] | None = None):
        self.data = deepcopy(data)
        self.saves = 0

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        self.data = deepcopy(data)
        self.saves += 1


def make(
    schedules=OWNER, *, store: FakeStore | None = None, **config: Any
) -> tuple[RivianScheduleController, FakeProvider, FakeStore]:
    store = store or FakeStore()
    provider = FakeProvider(schedules, store)
    options = {"home_location": HOME, "dry_run": False} | config
    return RivianScheduleController(provider, store, AdapterConfig(**options)), provider, store


async def start(controller, *, amps=16, obs=PLUGGED, now=T0):
    await controller.async_request_amps(amps)
    await controller.async_request_charge(True)
    await controller.async_tick(now, obs)


async def test_no_command_means_no_provider_access():
    controller, provider, _ = make()
    await controller.async_tick(T0, PLUGGED)
    assert provider.gets == 0
    assert controller.state(T0) is ControlState.IDLE


async def test_command_while_away_waits():
    controller, provider, _ = make()
    await controller.async_request_charge(True)
    await controller.async_tick(T0, Observation(plugged=True, home=False))
    assert provider.gets == 0
    assert controller.state(T0) is ControlState.WAITING_FOR_VEHICLE


async def test_takeover_persists_snapshot_before_first_write():
    controller, provider, store = make()
    await start(controller)
    assert len(provider.sets) == 1
    journal_then = provider.journal_at_set[0]
    assert journal_then["phase"] == "writing"
    assert journal_then["original"] == OWNER
    assert journal_then["original_hash"] == provider_hash(OWNER)
    assert journal_then["pending"]["purpose"] == "lease"
    assert store.data["phase"] == "owned"


async def test_start_writes_single_active_lease_at_home_schedule_location():
    controller, provider, _ = make()
    await start(controller, amps=13)
    [lease] = provider.sets[0]
    assert lease["amperage"] == 12
    assert lease["enabled"] is True
    assert lease["weekDays"] == ["Saturday"]
    assert lease["startTime"] == 11 * 60 + 55
    assert lease["duration"] == 25
    # The owner's home schedule location is reused (within 2 km of HA home).
    assert lease["location"] == {"latitude": 30.00001, "longitude": -97.00001}
    assert controller.state(T0) is ControlState.LEASE_ACTIVE


async def test_lease_location_falls_back_to_home_when_no_schedule_is_near():
    controller, provider, _ = make([OWNER[1]])
    await start(controller)
    assert provider.sets[0][0]["location"] == HOME


async def test_pause_parks_window_in_the_past():
    controller, provider, _ = make()
    await start(controller)
    await controller.async_request_charge(False)
    await controller.async_tick(T0 + timedelta(seconds=40), PLUGGED)
    [parked] = provider.sets[-1]
    assert parked["startTime"] + parked["duration"] == 11 * 60 + 55
    assert controller.state(T0) is ControlState.PAUSED


async def test_min_write_interval_defers_but_does_not_drop():
    controller, provider, _ = make()
    await start(controller)
    await controller.async_request_charge(False)
    await controller.async_tick(T0 + timedelta(seconds=10), PLUGGED)
    assert len(provider.sets) == 1
    await controller.async_tick(T0 + timedelta(seconds=31), PLUGGED)
    assert len(provider.sets) == 2


async def test_decrease_is_prompt_increase_waits():
    controller, provider, _ = make()
    await start(controller, amps=20)
    await controller.async_request_amps(14)
    await controller.async_tick(T0 + timedelta(seconds=31), PLUGGED)
    assert provider.sets[-1][0]["amperage"] == 14
    await controller.async_request_amps(24)
    await controller.async_tick(T0 + timedelta(seconds=70), PLUGGED)
    assert len(provider.sets) == 2
    await controller.async_tick(T0 + timedelta(seconds=152), PLUGGED)
    assert provider.sets[-1][0]["amperage"] == 24


async def test_amps_within_same_even_step_do_not_write():
    controller, provider, _ = make()
    await start(controller, amps=16)
    await controller.async_request_amps(17)
    await controller.async_tick(T0 + timedelta(minutes=5), PLUGGED)
    assert len(provider.sets) == 1


async def test_lease_renews_before_expiry():
    controller, provider, _ = make()
    await start(controller)
    await controller.async_tick(T0 + timedelta(minutes=11), CHARGING)
    assert len(provider.sets) == 1
    await controller.async_tick(T0 + timedelta(minutes=12), CHARGING)
    assert len(provider.sets) == 2


async def test_renewal_disabled_lets_lease_lapse():
    controller, provider, _ = make()
    await start(controller)
    controller.renewal_enabled = False
    later = T0 + timedelta(minutes=21)
    await controller.async_tick(later, CHARGING)
    assert len(provider.sets) == 1
    assert controller.state(later) is ControlState.LEASE_LAPSED


async def test_unplug_restores_exact_owner_list_after_delay():
    controller, provider, store = make()
    await start(controller)
    await controller.async_tick(T0 + timedelta(minutes=1), UNPLUGGED)
    assert provider.schedules != OWNER
    await controller.async_tick(T0 + timedelta(minutes=2), UNPLUGGED)
    assert provider.schedules == OWNER
    assert store.data["phase"] == "idle"
    assert store.data["original"] is None
    assert controller.state(T0) is ControlState.IDLE


async def test_unknown_plug_state_does_not_release():
    controller, provider, _ = make()
    await start(controller)
    await controller.async_tick(T0 + timedelta(minutes=5), UNKNOWN)
    assert controller.journal.phase is Phase.OWNED
    assert provider.schedules != OWNER


async def test_external_edit_faults_and_never_overwrites():
    controller, provider, _ = make()
    await start(controller)
    provider.schedules[0]["amperage"] = 40  # owner edits in the Rivian app
    edited = deepcopy(provider.schedules)
    await controller.async_request_amps(10)
    await controller.async_tick(T0 + timedelta(minutes=1), CHARGING)
    assert controller.state(T0) is ControlState.FAULT
    assert controller.journal.fault.code == "external_edit"
    assert provider.schedules == edited
    # Faulted: no further writes, even on unplug.
    await controller.async_tick(T0 + timedelta(minutes=5), UNPLUGGED)
    await controller.async_tick(T0 + timedelta(minutes=7), UNPLUGGED)
    assert provider.schedules == edited
    assert len(provider.sets) == 1


async def test_clear_fault_after_owner_restored_manually():
    controller, provider, _ = make()
    await start(controller)
    provider.schedules = deepcopy(OWNER)
    await controller.async_tick(T0 + timedelta(minutes=12), CHARGING)
    assert controller.journal.fault.code == "external_edit"
    assert await controller.async_clear_fault(T0 + timedelta(minutes=13))
    assert controller.journal.phase is Phase.IDLE


async def test_clear_fault_refuses_unknown_state_and_abandon_forgets():
    controller, provider, _ = make()
    await start(controller)
    provider.schedules[0]["amperage"] = 40
    await controller.async_tick(T0 + timedelta(minutes=12), CHARGING)
    assert not await controller.async_clear_fault(T0 + timedelta(minutes=13))
    assert controller.journal.phase is Phase.FAULT
    await controller.async_abandon()
    assert controller.journal.phase is Phase.IDLE
    assert controller.journal.original is None


async def test_ambiguous_write_that_landed_is_confirmed():
    controller, provider, _ = make()
    provider.set_error = ScheduleApiError("timeout")
    await start(controller)
    assert controller.state(T0) is ControlState.LEASE_ACTIVE
    assert controller.journal.unapplied_writes == 0


async def test_rejected_writes_retry_then_fault():
    controller, provider, _ = make()
    provider.apply_set = False
    provider.set_error = WriteRejectedError("no")
    await start(controller)
    assert controller.journal.phase is Phase.OWNED
    assert controller.journal.unapplied_writes == 1
    await controller.async_tick(T0 + timedelta(seconds=31), PLUGGED)
    await controller.async_tick(T0 + timedelta(seconds=62), PLUGGED)
    assert controller.journal.fault.code == "write_not_applied"
    assert provider.schedules == OWNER


async def test_write_that_lands_after_readback_is_accepted():
    controller, provider, _ = make()
    provider.apply_set = False
    await start(controller)
    assert controller.journal.unapplied_writes == 1
    # The provider applies the earlier write late.
    provider.schedules = deepcopy(provider.sets[0])
    provider.apply_set = True
    await controller.async_request_amps(10)
    await controller.async_tick(T0 + timedelta(seconds=31), CHARGING)
    assert controller.journal.phase is Phase.OWNED
    assert provider.sets[-1][0]["amperage"] == 10


async def test_conflicting_readback_faults():
    controller, provider, _ = make()

    async def set_and_conflict(schedules):
        provider.sets.append(deepcopy(schedules))
        provider.schedules = [OWNER[1]]

    provider.set_schedules = set_and_conflict
    await start(controller)
    assert controller.journal.fault.code == "conflict_after_write"


async def test_restart_mid_write_resolves_from_journal():
    store = FakeStore()
    controller, provider, _ = make(store=store)
    provider.set_error = AuthError("expired")
    with pytest.raises(AuthError):
        await start(controller)
    assert store.data["phase"] == "writing"
    # Restart: a fresh controller loads the journal; the write did land.
    restarted = RivianScheduleController(provider, store, controller.config)
    await restarted.async_load()
    provider.set_error = None
    await restarted.async_tick(T0 + timedelta(minutes=1), CHARGING)
    assert restarted.journal.phase is Phase.OWNED
    assert restarted.journal.lease.active is True
    # And it still restores the owner's list later.
    await restarted.async_tick(T0 + timedelta(minutes=3), UNPLUGGED)
    await restarted.async_tick(T0 + timedelta(minutes=5), UNPLUGGED)
    assert provider.schedules == OWNER


async def test_rate_limit_backs_off():
    controller, provider, _ = make(rate_limit_backoff=timedelta(minutes=5))
    provider.get_error = RateLimitedError("slow down")
    await start(controller)
    gets = provider.gets
    await controller.async_tick(T0 + timedelta(minutes=2), PLUGGED)
    assert provider.gets == gets
    provider.get_error = None
    await controller.async_tick(T0 + timedelta(minutes=6), PLUGGED)
    assert controller.state(T0) is ControlState.LEASE_ACTIVE
    assert "slow down" in controller.last_error


async def test_auth_error_propagates():
    controller, provider, _ = make()
    provider.get_error = AuthError("login")
    with pytest.raises(AuthError):
        await start(controller)


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
    dry = RivianScheduleController(provider, store, AdapterConfig(home_location=HOME, dry_run=True))
    await dry.async_load()
    await dry.async_tick(T0 + timedelta(minutes=1), CHARGING)
    assert provider.schedules == OWNER
    assert dry.journal.phase is Phase.IDLE


async def test_current_only_command_keeps_running_session():
    controller, provider, _ = make()
    await controller.async_request_amps(10)
    await controller.async_tick(T0, CHARGING)
    assert controller.journal.desired_charge is True
    assert provider.sets[0][0]["amperage"] == 10
    assert controller.state(T0) is ControlState.LEASE_ACTIVE


async def test_current_only_command_while_idle_parks():
    controller, provider, _ = make()
    await controller.async_request_amps(10)
    await controller.async_tick(T0, PLUGGED)
    assert controller.state(T0) is ControlState.PAUSED


async def test_empty_owner_list_is_restored_as_empty():
    controller, provider, _ = make([])
    await start(controller)
    assert provider.sets[0][0]["location"] == HOME
    await controller.async_request_release()
    await controller.async_tick(T0 + timedelta(minutes=1), CHARGING)
    assert provider.sets[-1] == []
    assert controller.journal.phase is Phase.IDLE


async def test_unsupported_provider_schedule_faults_before_any_write():
    controller, provider, _ = make([OWNER[0] | {"weekDays": ["Someday"]}])
    await start(controller)
    assert controller.journal.fault.code == "unsupported_schedule"
    assert provider.sets == []


async def test_manual_release_while_plugged():
    controller, provider, _ = make()
    await start(controller)
    await controller.async_request_release("trial complete")
    await controller.async_tick(T0 + timedelta(minutes=1), CHARGING)
    assert provider.schedules == OWNER
    assert controller.journal.command_received is False


async def test_parked_window_is_refreshed_before_it_recurs():
    controller, provider, _ = make()
    await controller.async_request_charge(False)
    await controller.async_tick(T0, PLUGGED)
    await controller.async_tick(T0 + timedelta(days=5), PLUGGED)
    assert len(provider.sets) == 1
    await controller.async_tick(T0 + timedelta(days=6, minutes=1), PLUGGED)
    assert len(provider.sets) == 2


async def test_snapshot_has_no_coordinates():
    controller, _, _ = make()
    await start(controller)
    text = repr(controller.snapshot(T0))
    assert "latitude" not in text and "30.0" not in text
