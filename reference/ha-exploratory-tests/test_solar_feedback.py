"""Fault-injection tests for feedback-confirmed EV actuation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("homeassistant")

from custom_components.rivian.solar_ev_charging import (
    const,
    coordinator as coordinator_module,
)
from custom_components.rivian.solar_ev_charging.coordinator import SolarEvCoordinator
from custom_components.rivian.solar_ev_charging.engine import SolarControlEngine
from custom_components.rivian.solar_ev_charging.models import (
    ControlConfig,
    ControlInputs,
)
from homeassistant.exceptions import HomeAssistantError


class _Clock:
    """Advance feedback polling without sleeping the test process."""

    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep = None

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, _seconds: float) -> None:
        self.now += 1.0
        if self.on_sleep:
            self.on_sleep()


class _States:
    def __init__(self, values: dict[str, SimpleNamespace]) -> None:
        self.values = values

    def get(self, entity_id: str):
        return self.values.get(entity_id)


def _state(value: str, stamp: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        state=value,
        last_updated=stamp,
        last_reported=stamp,
        attributes={"unit_of_measurement": "W"}
        if value != "on" and value != "off"
        else {},
    )


def _coordinator(states: _States) -> SolarEvCoordinator:
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator.hass = SimpleNamespace(
        states=states,
        services=SimpleNamespace(async_call=AsyncMock(return_value=None)),
    )
    coordinator.store = SimpleNamespace(async_save=AsyncMock())
    coordinator._runtime = {"journal": None}
    coordinator._fault = None
    coordinator._command_acknowledged = False
    coordinator._last_feedback_at = None
    coordinator._observed_power_w = None
    coordinator._observed_amps = None
    coordinator.options = {}
    coordinator.config = {const.CONF_WALL_VOLTAGE: "sensor.voltage"}
    coordinator._feedback_timeout = lambda: 2
    coordinator._feedback_safety_reason = lambda _journal: None
    return coordinator


def _with_clock(monkeypatch, callback=None) -> _Clock:
    clock = _Clock()
    clock.on_sleep = callback
    monkeypatch.setattr(coordinator_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(coordinator_module.asyncio, "sleep", clock.sleep)
    return clock


def test_stop_rejects_accepted_off_when_switch_stays_on(monkeypatch) -> None:
    base = datetime.now(UTC) + timedelta(seconds=1)
    states = _States(
        {"switch.ev": _state("on", base), "sensor.wall": _state("500", base)}
    )
    coordinator = _coordinator(states)
    _with_clock(monkeypatch)
    journal = {"switch_entity": "switch.ev", "wall_power_entity": "sensor.wall"}

    with pytest.raises(HomeAssistantError, match="Physical stop feedback"):
        asyncio.run(coordinator._async_stop_with_feedback(journal, "stop"))

    coordinator.hass.services.async_call.assert_awaited_once()
    assert journal["stop_required"] is True
    assert coordinator._command_acknowledged is False


def test_tesla_number_readback_wait_stops_on_new_safety_failure(monkeypatch):
    coordinator = _coordinator(_States({}))
    coordinator._runtime["journal"] = {"vehicle": "tesla"}
    coordinator._feedback_safety_reason = lambda _journal: (
        "vehicle_connection_changed_or_ambiguous"
    )
    _with_clock(monkeypatch)
    with pytest.raises(HomeAssistantError, match="Current readback safety gate failed"):
        asyncio.run(coordinator._async_set_tesla_amps(8, "number.current"))
    coordinator.hass.services.async_call.assert_awaited_once()


def test_stop_requires_two_distinct_low_power_reports(monkeypatch) -> None:
    base = datetime.now(UTC) + timedelta(seconds=1)
    states = _States(
        {"switch.ev": _state("off", base), "sensor.wall": _state("100", base)}
    )
    coordinator = _coordinator(states)
    _with_clock(monkeypatch)
    journal = {"switch_entity": "switch.ev", "wall_power_entity": "sensor.wall"}

    with pytest.raises(HomeAssistantError, match="Physical stop feedback"):
        asyncio.run(coordinator._async_stop_with_feedback(journal, "stop"))

    assert coordinator._command_acknowledged is False


def test_stop_accepts_two_distinct_post_command_low_power_reports(monkeypatch) -> None:
    base = datetime.now(UTC) - timedelta(seconds=1)
    states = _States(
        {"switch.ev": _state("off", base), "sensor.wall": _state("100", base)}
    )
    coordinator = _coordinator(states)

    def next_report() -> None:
        states.values["sensor.wall"] = _state("90", datetime.now(UTC))

    coordinator._feedback_timeout = lambda: 3
    _with_clock(monkeypatch, next_report)
    journal = {"switch_entity": "switch.ev", "wall_power_entity": "sensor.wall"}

    asyncio.run(coordinator._async_stop_with_feedback(journal, "stop"))

    assert coordinator._command_acknowledged is True
    assert coordinator._observed_power_w == 90


def test_start_rejects_precommand_reports(monkeypatch) -> None:
    base = datetime.now(UTC)
    states = _States(
        {
            "switch.ev": _state("on", base),
            "sensor.wall": _state("1900", base),
            "sensor.voltage": SimpleNamespace(
                state="240",
                last_updated=base,
                last_reported=base,
                attributes={"unit_of_measurement": "V"},
            ),
        }
    )
    coordinator = _coordinator(states)
    _with_clock(monkeypatch)
    journal = {
        "switch_entity": "switch.ev",
        "wall_power_entity": "sensor.wall",
        "wall_voltage_entity": "sensor.voltage",
        "target_amps": 8,
    }

    with pytest.raises(HomeAssistantError, match="start/current-limit feedback"):
        asyncio.run(
            coordinator._async_confirm_start(journal, base + timedelta(seconds=10))
        )


def test_overlimit_start_latches_fault_and_requests_cleanup(monkeypatch) -> None:
    base = datetime.now(UTC)
    states = _States(
        {
            "switch.ev": _state("on", base),
            "sensor.wall": _state("3000", base),
            "sensor.voltage": SimpleNamespace(
                state="240",
                last_updated=base,
                last_reported=base,
                attributes={"unit_of_measurement": "V"},
            ),
        }
    )
    coordinator = _coordinator(states)
    coordinator._lock = asyncio.Lock()
    coordinator._last_vehicle = None
    coordinator._initialized = True
    coordinator._status = lambda *_args: {}
    journal = {
        "switch_entity": "switch.ev",
        "wall_power_entity": "sensor.wall",
        "wall_voltage_entity": "sensor.voltage",
        "target_amps": 8,
        "stop_required": True,
    }
    coordinator._runtime["journal"] = journal
    coordinator._async_safe_release_locked = AsyncMock(return_value=False)
    _with_clock(monkeypatch)

    async def evaluate() -> dict:
        await coordinator._async_confirm_start(
            journal, datetime.now(UTC) - timedelta(seconds=1)
        )
        return {}

    coordinator._async_evaluate = evaluate
    asyncio.run(coordinator._async_update_data())

    assert "exceeds commanded limit" in coordinator._fault
    coordinator._async_safe_release_locked.assert_awaited_once_with("fault")


def test_initialize_keeps_saved_fault_and_uses_saved_journal_binding() -> None:
    old_journal = {
        "vehicle": "tesla",
        "switch_entity": "switch.old",
        "wall_power_entity": "sensor.old",
        "stop_required": True,
    }
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator.options = {
        const.CONF_SOLAR_ENABLED: False,
        const.CONF_VEHICLE_MODE: const.VEHICLE_TESLA,
    }
    coordinator.config = coordinator.options
    coordinator.store = SimpleNamespace(
        async_load=AsyncMock(
            return_value={"fault": "old_failure", "journal": old_journal}
        ),
        async_save=AsyncMock(),
    )
    coordinator._lock = asyncio.Lock()
    coordinator._initialized = False
    coordinator._async_safe_release_locked = AsyncMock(return_value=False)

    asyncio.run(coordinator.async_initialize())

    assert coordinator._fault == "old_failure"
    assert coordinator.enabled is False
    coordinator._async_safe_release_locked.assert_awaited_once_with("startup_recovery")
    assert coordinator._runtime["journal"]["switch_entity"] == "switch.old"


def test_unchanged_active_tesla_target_does_not_cycle_switch() -> None:
    coordinator = object.__new__(SolarEvCoordinator)
    journal = {"vehicle": "tesla", "phase": "charging_confirmed", "last_amps": 16}
    coordinator._async_prepare_journal = AsyncMock(return_value=journal)
    coordinator._active_feedback_valid = lambda _journal, _amps: True
    coordinator._async_stop_with_feedback = AsyncMock()
    coordinator._async_command_switch = AsyncMock()
    coordinator._async_set_tesla_amps = AsyncMock()

    asyncio.run(coordinator._async_apply_vehicle(const.VEHICLE_TESLA, 16))

    coordinator._async_stop_with_feedback.assert_not_awaited()
    coordinator._async_command_switch.assert_not_awaited()
    coordinator._async_set_tesla_amps.assert_not_awaited()


def test_future_or_out_of_range_measurements_are_not_safe_manual_inputs() -> None:
    now = datetime.now(UTC)
    states = _States(
        {
            "sensor.soc": SimpleNamespace(
                state="120",
                last_updated=now,
                last_reported=now,
                attributes={"unit_of_measurement": "%"},
            ),
            "sensor.voltage": SimpleNamespace(
                state="0",
                last_updated=now,
                last_reported=now,
                attributes={"unit_of_measurement": "V"},
            ),
            "sensor.future_soc": SimpleNamespace(
                state="60",
                last_updated=now + timedelta(seconds=1),
                last_reported=now + timedelta(seconds=1),
                attributes={"unit_of_measurement": "%"},
            ),
        }
    )
    coordinator = _coordinator(states)

    assert coordinator._percent_state("sensor.soc", 300) is None
    assert coordinator._voltage_state("sensor.voltage", 180) is None
    assert coordinator._percent_state("sensor.future_soc", 300) is None


def _manual_coordinator(inputs: ControlInputs) -> SolarEvCoordinator:
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator.config = {const.CONF_DRY_RUN: False, const.CONF_RESERVE_SOC: 25}
    coordinator.options = coordinator.config
    coordinator._runtime = {"enabled": True, "manual_override": True}
    coordinator._fault = None
    coordinator._last_vehicle = None
    coordinator._initialized = True
    coordinator._engine = SolarControlEngine(ControlConfig(reserve_soc=25))
    coordinator._connected_vehicle = lambda: const.VEHICLE_TESLA
    coordinator._connection_reason = lambda: None
    coordinator._command_readiness_reason = lambda *_args, **_kwargs: None
    coordinator._control_inputs = lambda _vehicle: inputs
    coordinator._status = lambda *_args: {}
    coordinator._async_apply_vehicle = AsyncMock()
    coordinator._async_latch_fault = AsyncMock()
    return coordinator


def test_valid_manual_path_uses_real_engine_validation() -> None:
    coordinator = _manual_coordinator(
        ControlInputs(
            now_monotonic=1,
            grid_power_w=0,
            battery_power_w=0,
            battery_soc=60,
            ev_power_w=0,
            wall_voltage_v=240,
            vehicle=const.VEHICLE_TESLA,
        )
    )

    asyncio.run(coordinator._async_evaluate())

    coordinator._async_apply_vehicle.assert_awaited_once_with(
        const.VEHICLE_TESLA, 48, force=True
    )


def test_manual_reserve_or_blocker_latches_without_actuation() -> None:
    coordinator = _manual_coordinator(
        ControlInputs(
            now_monotonic=1,
            grid_power_w=0,
            battery_power_w=0,
            battery_soc=20,
            ev_power_w=0,
            wall_voltage_v=240,
            vehicle=const.VEHICLE_TESLA,
            blocker="selected_blocker",
        )
    )

    asyncio.run(coordinator._async_evaluate())

    coordinator._async_apply_vehicle.assert_not_awaited()
    coordinator._async_latch_fault.assert_awaited_once_with(
        "selected_blocker", cleanup=True
    )


def test_feedback_rejects_connection_change_before_accepting_live_power() -> None:
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator._connection_reason = lambda: "vehicle_identity_ambiguous"
    coordinator._connected_vehicle = lambda: None
    coordinator._vehicle_identity_reason = lambda _vehicle: None
    coordinator._binding = lambda _vehicle: {"binding_fingerprint": "unchanged"}
    coordinator.config = {}
    coordinator.options = {}
    coordinator._runtime = {"manual_override": False}

    reason = coordinator._feedback_safety_reason(
        {"vehicle": const.VEHICLE_TESLA, "binding_fingerprint": "unchanged"}
    )

    assert reason == "vehicle_connection_changed_or_ambiguous"


def test_solar_feedback_soft_deficit_stops_before_long_ack_wait(monkeypatch) -> None:
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator._connection_reason = lambda: None
    coordinator._connected_vehicle = lambda: const.VEHICLE_TESLA
    coordinator._vehicle_identity_reason = lambda _vehicle: None
    coordinator._binding = lambda _vehicle: {"binding_fingerprint": "stable"}
    coordinator._control_inputs = lambda _vehicle: ControlInputs(
        now_monotonic=0,
        grid_power_w=600,
        battery_power_w=0,
        battery_soc=60,
        ev_power_w=0,
        wall_voltage_v=240,
        vehicle=const.VEHICLE_TESLA,
    )
    coordinator._engine = SolarControlEngine(ControlConfig(stop_hold_seconds=30))
    coordinator._home_state = lambda _vehicle: True
    coordinator._vehicle_at_limit = lambda _vehicle: False
    coordinator.config = {}
    coordinator.options = {const.CONF_STOP_HOLD_SECONDS: 30}
    coordinator._runtime = {"manual_override": False}
    clock = _Clock()
    monkeypatch.setattr(coordinator_module.time, "monotonic", clock.monotonic)
    journal = {"vehicle": const.VEHICLE_TESLA, "binding_fingerprint": "stable"}

    assert coordinator._feedback_safety_reason(journal) is None
    clock.now = 31
    assert coordinator._feedback_safety_reason(journal) == "feedback_surplus_lost"


def test_rivian_renewal_pending_phase_does_not_cycle_switch(monkeypatch) -> None:
    coordinator = object.__new__(SolarEvCoordinator)
    journal = {
        "vehicle": "rivian",
        "phase": "charging_confirmed",
        "last_amps": 8,
        "switch_entity": "switch.rivian",
        "wall_power_entity": "sensor.wall",
        "wall_voltage_entity": "sensor.voltage",
        "vehicle_id": "vehicle",
        "last_hash": "old",
    }
    coordinator._async_prepare_journal = AsyncMock(return_value=journal)
    coordinator._active_feedback_valid = lambda _journal, _amps: True
    coordinator._async_stop_with_feedback = AsyncMock()
    coordinator._async_command_switch = AsyncMock()
    coordinator._async_confirm_start = AsyncMock()
    coordinator.store = SimpleNamespace(async_save=AsyncMock())
    coordinator._runtime = {"journal": journal}
    coordinator._last_rivian_renewal = 0
    coordinator._rivian_lease_schedule = lambda amps: [{"amperage": amps}]
    coordinator._async_rivian_set = AsyncMock(return_value={"hash": "renewed"})
    coordinator._async_rivian_get = AsyncMock(return_value={"hash": "renewed"})
    monkeypatch.setattr(
        coordinator_module, "canonical_schedule_hash", lambda _schedules: "pending"
    )

    asyncio.run(coordinator._async_apply_vehicle(const.VEHICLE_RIVIAN, 8))
    asyncio.run(coordinator._async_apply_vehicle(const.VEHICLE_RIVIAN, 8))

    coordinator._async_stop_with_feedback.assert_not_awaited()
    coordinator._async_command_switch.assert_not_awaited()
    assert journal["phase"] == "charging_confirmed"


def test_rivian_schedule_replay_stub_tracks_current_and_restores_base() -> None:
    """Check coordinator sequencing with stubbed schedules, not physical pause behavior."""
    base_schedule = {
        "id": "base-schedule",
        "weekDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "startTime": 420,
        "duration": 900,
        "location": {"latitude": 1.0, "longitude": 2.0},
        "amperage": 24,
        "enabled": True,
    }
    journal = {
        "vehicle": const.VEHICLE_RIVIAN,
        "phase": "charging_confirmed",
        "last_amps": 8,
        "switch_entity": "switch.rivian",
        "wall_power_entity": "sensor.wall",
        "wall_voltage_entity": "sensor.voltage",
        "vehicle_id": "vehicle-under-test",
        "base_schedules": [base_schedule],
        "base_hash": coordinator_module.canonical_schedule_hash([base_schedule]),
        "last_hash": coordinator_module.canonical_schedule_hash([base_schedule]),
    }
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator._runtime = {"journal": journal}
    coordinator.store = SimpleNamespace(async_save=AsyncMock())
    coordinator._async_prepare_journal = AsyncMock(return_value=journal)
    coordinator._active_feedback_valid = lambda _journal, _amps: True
    coordinator._async_stop_with_feedback = AsyncMock()
    coordinator._async_command_switch = AsyncMock()
    coordinator._async_confirm_start = AsyncMock()
    coordinator._engine_stop = lambda: None
    coordinator._last_rivian_renewal = 0.0
    coordinator._last_command = None
    written: list[tuple[list[dict], str, str]] = []
    remote = {
        "schedules": [base_schedule],
        "hash": coordinator_module.canonical_schedule_hash([base_schedule]),
    }
    window_modes = iter(("inside", "outside", "inside"))

    def daylight_schedule(amps: int) -> dict:
        mode = next(window_modes)
        return {
            "id": "solar-lease",
            "weekDays": [
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
                "Sunday",
            ],
            "startTime": 0 if mode == "inside" else 1200,
            "duration": 1440 if mode == "inside" else 60,
            "location": {"latitude": 1.0, "longitude": 2.0},
            "amperage": amps,
            "enabled": True,
        }

    async def set_schedule(
        _vehicle_id: str, schedules: list[dict], expected_hash: str, reason: str
    ) -> dict[str, str]:
        assert expected_hash == remote["hash"]
        assert len(schedules) == 1
        written.append((schedules, expected_hash, reason))
        remote["schedules"] = [dict(schedules[0])]
        remote["hash"] = coordinator_module.canonical_schedule_hash(schedules)
        return {"hash": remote["hash"]}

    # The builder is stubbed to model window payloads; this proves sequencing only.
    coordinator._rivian_lease_schedule = daylight_schedule
    coordinator._async_rivian_set = set_schedule
    coordinator._async_rivian_get = AsyncMock(
        side_effect=lambda _vehicle: {
            "schedules": [dict(remote["schedules"][0])],
            "hash": remote["hash"],
        }
    )

    async def replay() -> None:
        for amps in (8, 12, 8):
            await coordinator._async_apply_vehicle(const.VEHICLE_RIVIAN, amps)
        assert journal["base_schedules"] == [base_schedule]
        assert journal["last_hash"] == remote["hash"]
        assert await coordinator._async_safe_release_locked("solar_exit") is True

    asyncio.run(replay())

    assert [entry[0][0]["amperage"] for entry in written] == [8, 12, 8, 24]
    assert [
        (entry[0][0]["startTime"], entry[0][0]["duration"], entry[0][0]["enabled"])
        for entry in written[:3]
    ] == [(0, 1440, True), (1200, 60, True), (0, 1440, True)]
    trial_time = 600  # 10:00 local trial time
    assert 0 <= trial_time < 1440
    assert not 1200 <= trial_time < 1200 + 60
    assert [entry[1] for entry in written] == [
        coordinator_module.canonical_schedule_hash([base_schedule]),
        coordinator_module.canonical_schedule_hash([written[0][0][0]]),
        coordinator_module.canonical_schedule_hash([written[1][0][0]]),
        coordinator_module.canonical_schedule_hash([written[2][0][0]]),
    ]
    assert [entry[2] for entry in written] == [
        "solar_lease",
        "solar_lease",
        "solar_lease",
        "restore_solar_exit",
    ]
    assert written[-1][0] == [base_schedule]
    assert coordinator._runtime["journal"] is None
    coordinator._async_rivian_get.assert_awaited()


def test_rivian_external_schedule_conflict_blocks_restore_and_keeps_journal() -> None:
    """An external schedule change must remain recoverable for a later release."""
    base_schedule = {
        "id": "base-schedule",
        "weekDays": ["Monday"],
        "startTime": 420,
        "duration": 900,
        "location": {"latitude": 1.0, "longitude": 2.0},
        "amperage": 24,
        "enabled": True,
    }
    journal = {
        "vehicle": const.VEHICLE_RIVIAN,
        "switch_entity": "switch.rivian",
        "wall_power_entity": "sensor.wall",
        "vehicle_id": "vehicle-under-test",
        "base_schedules": [base_schedule],
        "base_hash": "base-hash",
        "last_hash": "solar-8",
        "pending_hash": None,
    }
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator._runtime = {"journal": journal}
    coordinator._fault = None
    coordinator.store = SimpleNamespace(async_save=AsyncMock())
    coordinator._async_stop_with_feedback = AsyncMock()
    coordinator._async_rivian_get = AsyncMock(return_value={"hash": "external-hash"})
    coordinator._async_rivian_set = AsyncMock()

    assert asyncio.run(coordinator._async_safe_release_locked("solar_exit")) is False

    assert coordinator._async_rivian_set.await_count == 0
    assert coordinator._runtime["rivian_write_blocked"] is True
    assert coordinator._runtime["journal"] is journal
    assert journal["phase"] == "cleanup_pending"
    assert journal["base_schedules"] == [base_schedule]
    assert "rivian_schedule_changed_externally" in journal["last_cleanup_error"]
