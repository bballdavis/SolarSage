"""Dependency-free tests for the solar control engine."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = "_solar_engine_unit_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "custom_components/rivian/solar_ev_charging")]
sys.modules.setdefault(PACKAGE, package)


def _load(name: str):
    spec = spec_from_file_location(
        f"{PACKAGE}.{name}",
        ROOT / f"custom_components/rivian/solar_ev_charging/{name}.py",
    )
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


const = _load("const")
models = _load("models")
engine_module = _load("engine")
ControlConfig = models.ControlConfig
ControlInputs = models.ControlInputs
SolarControlEngine = engine_module.SolarControlEngine


def _inputs(now: float, **changes):
    values = {
        "now_monotonic": now,
        "grid_power_w": -4000.0,
        "battery_power_w": 500.0,
        "battery_soc": 60.0,
        "ev_power_w": 0.0,
        "wall_voltage_v": 240.0,
        "vehicle": "rivian",
        "inputs_fresh": True,
        "blocker": None,
    }
    values.update(changes)
    return ControlInputs(**values)


def test_requires_stable_surplus_before_starting():
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=120))
    assert engine.evaluate(_inputs(0), amp_step=2).state == const.STATE_WAITING
    assert engine.evaluate(_inputs(119), amp_step=2).state == const.STATE_WAITING
    decision = engine.evaluate(_inputs(120), amp_step=2)
    assert decision.state == const.STATE_SOLAR
    assert decision.target_amps == 8


def test_physical_stop_starts_cooldown_even_for_an_external_manual_session():
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=0))
    engine.stop(100)
    decision = engine.evaluate(_inputs(110), amp_step=2)
    assert not decision.should_charge
    assert decision.restart_remaining_seconds == 170
    assert engine.evaluate(_inputs(280), amp_step=2).should_charge


def test_physical_release_end_extends_an_earlier_policy_stop_cooldown():
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=0))
    assert engine.evaluate(_inputs(0), amp_step=2).should_charge
    assert not engine.evaluate(_inputs(10, blocker="test"), amp_step=2).should_charge
    engine.stop(70)
    decision = engine.evaluate(_inputs(190), amp_step=2)
    assert not decision.should_charge
    assert decision.restart_remaining_seconds == 60


def test_battery_discharge_is_removed_from_true_surplus():
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=0))
    decision = engine.evaluate(
        _inputs(0, grid_power_w=-3000, battery_power_w=-1500), amp_step=2
    )
    assert decision.available_watts == 1000
    assert decision.target_amps == 0
    assert decision.reason == "battery_discharging"


def test_active_session_stops_after_sustained_deficit():
    engine = SolarControlEngine(
        ControlConfig(start_hold_seconds=0, stop_hold_seconds=30)
    )
    assert engine.evaluate(_inputs(0), amp_step=2).should_charge
    transient = engine.evaluate(_inputs(10, grid_power_w=1000), amp_step=2)
    assert transient.should_charge
    stopped = engine.evaluate(_inputs(40, grid_power_w=1000), amp_step=2)
    assert not stopped.should_charge
    assert stopped.reason == "surplus_lost"


def test_blocker_stops_immediately_and_resets_session():
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=0))
    assert engine.evaluate(_inputs(0), amp_step=1).should_charge
    decision = engine.evaluate(_inputs(1, blocker="storm_watch"), amp_step=1)
    assert decision.state == const.STATE_BLOCKED
    assert not decision.should_charge
    assert decision.reason == "storm_watch"
    assert not engine.active


def test_downward_adjustment_is_immediate():
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=0))
    engine.evaluate(_inputs(0, grid_power_w=-12000), amp_step=2)
    raised = engine.evaluate(_inputs(60, grid_power_w=-12000), amp_step=2)
    assert raised.target_amps == 10
    lowered = engine.evaluate(_inputs(61, grid_power_w=0, ev_power_w=2880), amp_step=2)
    assert lowered.target_amps == 8


def test_reserve_floor_blocks_entry_without_becoming_hard_threshold():
    engine = SolarControlEngine(
        ControlConfig(reserve_soc=25, reserve_entry_margin=5, start_hold_seconds=0)
    )
    decision = engine.evaluate(_inputs(0, battery_soc=29.9), amp_step=2)
    assert decision.reason == "battery_reserve"
    assert not decision.should_charge


def test_active_session_stops_immediately_at_reserve_floor():
    engine = SolarControlEngine(
        ControlConfig(reserve_soc=25, reserve_entry_margin=5, start_hold_seconds=0)
    )
    assert engine.evaluate(_inputs(0, battery_soc=35), amp_step=2).should_charge
    decision = engine.evaluate(_inputs(1, battery_soc=24.9), amp_step=2)
    assert decision.state == const.STATE_BLOCKED
    assert decision.reason == "battery_reserve"
    assert not decision.should_charge


def test_stale_input_and_ambiguous_vehicle_are_hard_blocks():
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=0))
    stale = engine.evaluate(_inputs(0, inputs_fresh=False), amp_step=2)
    assert stale.reason == "stale_or_unavailable_input"
    ambiguous = engine.evaluate(_inputs(1, vehicle=None), amp_step=2)
    assert ambiguous.reason == "vehicle_identity_ambiguous"


def test_stale_active_input_stops_and_preserves_restart_cooldown():
    engine = SolarControlEngine(
        ControlConfig(start_hold_seconds=0, min_off_seconds=180)
    )
    assert engine.evaluate(_inputs(0), amp_step=2).should_charge
    stale = engine.evaluate(_inputs(1, inputs_fresh=False), amp_step=2)
    assert not stale.should_charge
    assert stale.restart_remaining_seconds == 180
    assert not engine.evaluate(_inputs(2), amp_step=2).should_charge


def test_invalid_numeric_inputs_fail_closed_before_division():
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=0))
    for changes in (
        {"wall_voltage_v": 0},
        {"wall_voltage_v": float("nan")},
        {"ev_power_w": float("nan")},
        {"battery_soc": 101},
        {"grid_power_w": None},
        {"battery_power_w": "invalid"},
        {"ev_power_w": True},
        {"now_monotonic": float("inf")},
    ):
        decision = engine.evaluate(_inputs(0, **changes), amp_step=2)
        assert not decision.should_charge
        assert decision.state == const.STATE_BLOCKED


@pytest.mark.parametrize("interval", [1, 10, 60])
@pytest.mark.parametrize("vehicle, amp_step", [("rivian", 2), ("tesla", 1)])
def test_ramp_uses_configured_rate_independent_of_evaluation_cadence(
    interval, vehicle, amp_step
):
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=0, min_off_seconds=0))
    assert (
        engine.evaluate(
            _inputs(0, vehicle=vehicle, grid_power_w=-12000), amp_step=amp_step
        ).target_amps
        == 8
    )
    targets = {}
    for now in range(interval, 121, interval):
        targets[now] = engine.evaluate(
            _inputs(now, vehicle=vehicle, grid_power_w=-12000), amp_step=amp_step
        ).target_amps
    assert targets[60] == 10
    assert targets[120] == 12


def test_waiting_time_does_not_create_ramp_credit():
    engine = SolarControlEngine(
        ControlConfig(start_hold_seconds=120, min_off_seconds=0)
    )
    assert not engine.evaluate(
        _inputs(0, grid_power_w=-12000), amp_step=2
    ).should_charge
    started = engine.evaluate(_inputs(120, grid_power_w=-12000), amp_step=2)
    assert started.target_amps == 8
    assert (
        engine.evaluate(_inputs(180, grid_power_w=-12000), amp_step=2).target_amps == 10
    )


def test_backwards_clock_does_not_create_ramp_credit():
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=0, min_off_seconds=0))
    engine.evaluate(_inputs(100, grid_power_w=-12000), amp_step=2)
    engine.evaluate(_inputs(90, grid_power_w=-12000), amp_step=2)
    assert (
        engine.evaluate(_inputs(91, grid_power_w=-12000), amp_step=2).target_amps == 8
    )


def test_duplicate_time_does_not_create_ramp_credit():
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=0, min_off_seconds=0))
    assert engine.evaluate(_inputs(0, grid_power_w=-12000), amp_step=2).target_amps == 8
    assert (
        engine.evaluate(_inputs(60, grid_power_w=-12000), amp_step=2).target_amps == 10
    )
    assert (
        engine.evaluate(_inputs(60, grid_power_w=-12000), amp_step=2).target_amps == 10
    )
    assert (
        engine.evaluate(_inputs(120, grid_power_w=-12000), amp_step=2).target_amps == 12
    )


def test_vehicle_identity_change_clears_ramp_state_and_starts_cooldown():
    engine = SolarControlEngine(
        ControlConfig(start_hold_seconds=0, min_off_seconds=180)
    )
    assert engine.evaluate(_inputs(0, vehicle="rivian"), amp_step=2).should_charge
    changed = engine.evaluate(_inputs(1, vehicle="tesla"), amp_step=1)
    assert not changed.should_charge
    assert changed.restart_remaining_seconds == 180


def test_forecast_gate_applies_only_before_starting():
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=0, min_off_seconds=0))
    waiting = engine.evaluate(
        _inputs(0, forecast_allows_start=False, forecast_reason="forecast_stale"),
        amp_step=2,
    )
    assert not waiting.should_charge and waiting.reason == "forecast_stale"
    assert engine.evaluate(_inputs(1), amp_step=2).should_charge
    active = engine.evaluate(
        _inputs(61, grid_power_w=-12000, forecast_allows_start=False), amp_step=2
    )
    assert active.should_charge and active.target_amps == 10


def test_brief_cloud_drops_to_minimum_and_recovery_does_not_restart():
    engine = SolarControlEngine(
        ControlConfig(start_hold_seconds=0, min_off_seconds=180)
    )
    assert engine.evaluate(_inputs(0, grid_power_w=-6000), amp_step=2).should_charge
    assert engine.evaluate(_inputs(10, grid_power_w=100), amp_step=2).target_amps == 8
    recovered = engine.evaluate(_inputs(11, grid_power_w=-6000), amp_step=2)
    assert recovered.should_charge and recovered.target_amps == 8


def test_cloud_reduction_is_immediate_and_soft_cloud_cycles_consume_budget():
    engine = SolarControlEngine(
        ControlConfig(
            start_hold_seconds=0,
            stop_hold_seconds=30,
            max_transient_deficit_wh=4,
        )
    )
    assert engine.evaluate(_inputs(0, grid_power_w=-12000), amp_step=2).target_amps == 8
    assert (
        engine.evaluate(_inputs(60, grid_power_w=-12000), amp_step=2).target_amps == 10
    )
    cloud = engine.evaluate(_inputs(61, grid_power_w=-2420), amp_step=2)
    assert cloud.should_charge and cloud.target_amps == 8
    assert engine.evaluate(_inputs(70, grid_power_w=600), amp_step=2).should_charge
    assert engine.evaluate(_inputs(80, grid_power_w=-6000), amp_step=2).should_charge
    assert engine.evaluate(_inputs(90, grid_power_w=600), amp_step=2).should_charge
    stopped = engine.evaluate(_inputs(100, grid_power_w=600), amp_step=2)
    assert not stopped.should_charge
    assert stopped.reason == "surplus_lost"


def test_sustained_transient_budget_stops_and_locks_out_restart():
    engine = SolarControlEngine(
        ControlConfig(
            start_hold_seconds=0, min_off_seconds=180, max_transient_deficit_wh=1
        )
    )
    assert engine.evaluate(_inputs(0), amp_step=2).should_charge
    stopped = engine.evaluate(_inputs(60, grid_power_w=1000), amp_step=2)
    assert not stopped.should_charge
    waiting = engine.evaluate(_inputs(61), amp_step=2)
    assert not waiting.should_charge
    assert waiting.restart_remaining_seconds > 0


def test_hard_import_stops_immediately():
    engine = SolarControlEngine(ControlConfig(start_hold_seconds=0))
    assert engine.evaluate(_inputs(0), amp_step=2).should_charge
    decision = engine.evaluate(_inputs(1, grid_power_w=1600), amp_step=2)
    assert decision.state == const.STATE_BLOCKED
    assert not decision.should_charge


def test_repeated_hard_blocks_do_not_extend_active_session_cooldown():
    engine = SolarControlEngine(
        ControlConfig(start_hold_seconds=0, min_off_seconds=180)
    )
    assert engine.evaluate(_inputs(0), amp_step=2).should_charge
    assert not engine.evaluate(_inputs(1, grid_power_w=1600), amp_step=2).should_charge
    assert not engine.evaluate(_inputs(2), amp_step=2).should_charge
    assert not engine.evaluate(_inputs(60, grid_power_w=1600), amp_step=2).should_charge
    waiting = engine.evaluate(_inputs(180), amp_step=2)
    assert not waiting.should_charge
    assert waiting.restart_remaining_seconds == 1
    assert engine.evaluate(_inputs(181), amp_step=2).should_charge
