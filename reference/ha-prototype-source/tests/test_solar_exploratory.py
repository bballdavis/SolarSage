"""Replay sanitized observed and synthetic solar-controller snapshots."""

import asyncio
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "exploratory"
PACKAGE = "_solar_exploratory_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "custom_components/rivian/solar_ev_charging")]
sys.modules.setdefault(PACKAGE, package)


def _load(name):
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
diagnostics = _load("input_diagnostics")


def _read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _inputs(case, now=0):
    values = {
        "now_monotonic": now,
        "blocker": None,
        "forecast_allows_start": True,
        "forecast_reason": None,
    }
    values.update(case["inputs"])
    values["vehicle"] = case["vehicle"]
    return models.ControlInputs(**values)


@pytest.mark.parametrize(
    "case", _read("synthetic_controller_cases.json")["cases"], ids=lambda c: c["name"]
)
def test_synthetic_vehicle_branches_replay_actual_engine(case):
    config = models.ControlConfig(start_hold_seconds=0, min_off_seconds=0)
    decision = engine_module.SolarControlEngine(config).evaluate(
        _inputs(case), amp_step=case["amp_step"]
    )
    expected = case["expected"]
    assert decision.should_charge is expected["should_charge"]
    if "target_amps" in expected:
        assert decision.target_amps == expected["target_amps"]
        assert decision.state == expected["state"]
    else:
        assert decision.reason == expected["reason"]


def test_synthetic_stale_wall_transition_stops_an_active_session():
    transition = _read("synthetic_controller_cases.json")["transition"]
    engine = engine_module.SolarControlEngine(
        models.ControlConfig(start_hold_seconds=0, min_off_seconds=0)
    )
    assert engine.evaluate(
        models.ControlInputs(**transition["fresh_start"]), amp_step=1
    ).should_charge
    stopped = engine.evaluate(
        models.ControlInputs(**transition["stale_wall"]), amp_step=1
    )
    assert stopped.should_charge is False
    assert stopped.reason == "stale_or_unavailable_input"


def test_synthetic_cloud_import_sequence_replays_timing_contract():
    sequence = _read("synthetic_controller_cases.json")["cloud_import_sequence"]
    engine = engine_module.SolarControlEngine(
        models.ControlConfig(
            start_hold_seconds=0, stop_hold_seconds=30, min_off_seconds=0
        )
    )
    for raw_step in sequence:
        step = dict(raw_step)
        expected = step.pop("expected")
        decision = engine.evaluate(models.ControlInputs(**step), amp_step=2)
        assert decision.should_charge is expected["should_charge"]
        if expected["should_charge"]:
            assert decision.target_amps == expected["target_amps"]
        else:
            assert decision.reason == expected["reason"]


def test_coordinator_reader_replays_observed_stale_gate():
    pytest.importorskip("homeassistant")
    from custom_components.rivian.solar_ev_charging import (
        const,
        coordinator as coordinator_module,
    )
    from custom_components.rivian.solar_ev_charging.coordinator import (
        SolarEvCoordinator,
    )

    snapshot = _read("observed_ha_snapshot.json")
    now = datetime.now(UTC)
    observed_at = datetime.fromisoformat(snapshot["provenance"]["observed_at"])

    def stamp(signal):
        age = (
            observed_at - datetime.fromisoformat(signal["last_reported"])
        ).total_seconds()
        return now - timedelta(seconds=age)

    def state(value, signal, unit):
        when = stamp(signal)
        return SimpleNamespace(
            state=str(value),
            last_updated=when,
            last_reported=when,
            attributes={"unit_of_measurement": unit},
        )

    class States:
        def __init__(self, values):
            self.values = values

        def get(self, entity):
            return self.values.get(entity)

    config = {
        const.CONF_GRID_POWER: "sensor.grid",
        const.CONF_BATTERY_POWER: "sensor.battery",
        const.CONF_BATTERY_SOC: "sensor.soc",
        const.CONF_WALL_POWER: "sensor.wall",
        const.CONF_WALL_VOLTAGE: "sensor.voltage",
    }
    states = States(
        {
            "sensor.grid": state(
                snapshot["power"]["grid"]["value"], snapshot["power"]["grid"], "W"
            ),
            "sensor.battery": state(
                snapshot["power"]["battery"]["value"], snapshot["power"]["battery"], "W"
            ),
            "sensor.soc": state(
                snapshot["power"]["home_battery_soc"]["value"],
                snapshot["power"]["home_battery_soc"],
                "%",
            ),
            "sensor.wall": state(
                snapshot["wall_connector"]["power_kw"],
                {"last_reported": snapshot["wall_connector"]["power_last_reported"]},
                "kW",
            ),
            "sensor.voltage": state(
                240, {"last_reported": snapshot["provenance"]["observed_at"]}, "V"
            ),
        }
    )
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator.hass = SimpleNamespace(states=states)
    coordinator.config = config
    coordinator.options = {}
    coordinator._forecast_data = lambda _soc, _voltage: {
        "allow_start": True,
        "reason": None,
    }
    coordinator._active_blocker = lambda: None
    coordinator._vehicle_at_limit = lambda _vehicle: False
    inputs = coordinator._control_inputs("tesla")
    assert inputs.inputs_fresh is False
    decision = coordinator_module.SolarControlEngine(
        models.ControlConfig(start_hold_seconds=0)
    ).evaluate(inputs, amp_step=1)
    assert decision.should_charge is False
    assert decision.reason == "stale_or_unavailable_input"


@pytest.mark.parametrize(
    ("tesla_connected", "rivian_connected", "selected", "reason"),
    [
        ("on", "off", "tesla", None),
        ("off", "on", "rivian", None),
        ("off", "off", None, "vehicle_identity_ambiguous"),
        ("on", "on", None, "vehicle_identity_ambiguous"),
    ],
)
def test_coordinator_vehicle_selection_handles_one_two_or_neither_connection(
    tesla_connected, rivian_connected, selected, reason
):
    pytest.importorskip("homeassistant")
    from custom_components.rivian.solar_ev_charging import const
    from custom_components.rivian.solar_ev_charging.coordinator import (
        SolarEvCoordinator,
    )

    stamp = datetime.now(UTC)

    class States:
        def __init__(self, values):
            self.values = values

        def get(self, entity):
            return self.values.get(entity)

    def state(value):
        return SimpleNamespace(
            state=value, last_updated=stamp, last_reported=stamp, attributes={}
        )

    coordinator = object.__new__(SolarEvCoordinator)
    coordinator.hass = SimpleNamespace(
        states=States(
            {
                "binary_sensor.wall": state("on"),
                "binary_sensor.tesla": state(tesla_connected),
                "binary_sensor.rivian": state(rivian_connected),
            }
        )
    )
    coordinator.options = {const.CONF_VEHICLE_MODE: const.VEHICLE_MODE_BOTH}
    coordinator.config = {
        const.CONF_WALL_CONNECTED: "binary_sensor.wall",
        const.CONF_TESLA_CONNECTED: "binary_sensor.tesla",
        const.CONF_RIVIAN_CONNECTED: "binary_sensor.rivian",
    }
    coordinator._runtime = {"manual_override": False}
    assert coordinator._connected_vehicle() == selected
    assert coordinator._connection_reason() == reason
    coordinator._runtime["manual_override"] = True
    assert coordinator.manual_override is True


def test_manual_override_dry_run_evaluation_does_not_apply_vehicle_command():
    pytest.importorskip("homeassistant")
    from custom_components.rivian.solar_ev_charging import const
    from custom_components.rivian.solar_ev_charging.coordinator import (
        SolarEvCoordinator,
    )

    coordinator = object.__new__(SolarEvCoordinator)
    coordinator.options = {
        const.CONF_DRY_RUN: True,
        const.CONF_VEHICLE_MODE: const.VEHICLE_TESLA,
    }
    coordinator.config = {}
    coordinator._runtime = {"enabled": True, "manual_override": True}
    coordinator._fault = None
    coordinator._initialized = True
    coordinator._last_vehicle = None
    coordinator._connected_vehicle = lambda: const.VEHICLE_TESLA
    coordinator._connection_reason = lambda: None
    coordinator._command_readiness_reason = lambda *_args, **_kwargs: None
    coordinator._control_inputs = lambda _vehicle: models.ControlInputs(
        now_monotonic=0,
        grid_power_w=-4000,
        battery_power_w=500,
        battery_soc=60,
        ev_power_w=0,
        wall_voltage_v=240,
        vehicle=const.VEHICLE_TESLA,
        inputs_fresh=True,
    )
    coordinator._engine = engine_module.SolarControlEngine(
        models.ControlConfig(start_hold_seconds=0)
    )
    coordinator._async_apply_vehicle = AsyncMock()
    coordinator._async_safe_release_locked = AsyncMock()
    coordinator._status = lambda *args: {"state": args[0], "reason": args[-1]}

    result = asyncio.run(coordinator._async_evaluate())
    assert result["state"] == const.STATE_MANUAL
    coordinator._async_apply_vehicle.assert_not_awaited()


def test_manual_override_live_mode_requires_commissioned_stop_path():
    pytest.importorskip("homeassistant")
    from custom_components.rivian.solar_ev_charging import const
    from custom_components.rivian.solar_ev_charging.coordinator import (
        SolarEvCoordinator,
    )

    stamp = datetime.now(UTC)

    def state(value, unit=None):
        return SimpleNamespace(
            state=value,
            last_reported=stamp,
            last_updated=stamp,
            attributes={"unit_of_measurement": unit} if unit else {},
        )

    config = {
        const.CONF_WALL_CONNECTED: "binary_sensor.wall_connected",
        const.CONF_WALL_POWER: "sensor.wall_power",
        const.CONF_WALL_VOLTAGE: "sensor.wall_voltage",
        const.CONF_TESLA_CONNECTED: "binary_sensor.tesla_connected",
        const.CONF_TESLA_SOC: "sensor.tesla_soc",
        const.CONF_TESLA_LIMIT: "sensor.tesla_limit",
        const.CONF_TESLA_HOME: "device_tracker.tesla",
        const.CONF_TESLA_SWITCH: "switch.tesla_charge",
        const.CONF_TESLA_AMPS: "number.tesla_amps",
    }
    values = {
        "binary_sensor.wall_connected": state("on"),
        "sensor.wall_power": state("0", "kW"),
        "sensor.wall_voltage": state("240", "V"),
        "binary_sensor.tesla_connected": state("on"),
        "sensor.tesla_soc": state("40", "%"),
        "sensor.tesla_limit": state("80", "%"),
        "device_tracker.tesla": state("home"),
        "switch.tesla_charge": state("off"),
        "number.tesla_amps": state("48", "A"),
    }
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator.options = {
        const.CONF_DRY_RUN: False,
        const.CONF_VEHICLE_MODE: const.VEHICLE_TESLA,
    }
    coordinator.config = config
    coordinator.hass = SimpleNamespace(states=SimpleNamespace(get=values.get))
    coordinator._runtime = {
        "enabled": True,
        "manual_override": True,
        "commission": {},
    }
    coordinator._fault = None
    coordinator._initialized = True
    coordinator._last_vehicle = None
    # Registry identity is checked separately; this case tests the actual
    # connection, fresh-state, and binding-fingerprint readiness path.
    coordinator._vehicle_identity_reason = lambda *_args, **_kwargs: None
    coordinator._control_inputs = lambda _vehicle: models.ControlInputs(
        now_monotonic=0,
        grid_power_w=-4000,
        battery_power_w=500,
        battery_soc=60,
        ev_power_w=0,
        wall_voltage_v=240,
        vehicle=const.VEHICLE_TESLA,
        inputs_fresh=True,
    )
    coordinator._engine = engine_module.SolarControlEngine(
        models.ControlConfig(start_hold_seconds=0)
    )
    coordinator._async_safe_release_locked = AsyncMock()
    coordinator._async_apply_vehicle = AsyncMock()
    coordinator._status = lambda *args: {"state": args[0], "reason": args[-1]}

    assert coordinator._connected_vehicle() == const.VEHICLE_TESLA
    assert (
        coordinator._command_readiness_reason(const.VEHICLE_TESLA, commissioning=False)
        == "physical_stop_not_commissioned"
    )
    result = asyncio.run(coordinator._async_evaluate())
    assert result == {
        "state": const.STATE_FAULT,
        "reason": "physical_stop_not_commissioned",
    }
    coordinator._async_safe_release_locked.assert_awaited_once_with(
        "physical_stop_not_commissioned"
    )
    coordinator._async_apply_vehicle.assert_not_awaited()


def test_observed_snapshot_is_sanitized_and_fails_closed_at_unplugged_gate():
    snapshot = _read("observed_ha_snapshot.json")
    assert snapshot["synthetic"] is False
    assert snapshot["provenance"]["redactions"]
    forbidden_keys = {
        "vin",
        "latitude",
        "longitude",
        "email",
        "password",
        "token",
        "access_token",
        "api_key",
    }

    def inspect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert key.lower() not in forbidden_keys
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
        elif isinstance(value, str):
            assert "@" not in value

    inspect(snapshot)
    assert snapshot["wall_connector"]["connected"] == "off"
    assert snapshot["wall_connector"]["status"] == "not_connected"

    observed_at = datetime.fromisoformat(snapshot["provenance"]["observed_at"])

    def age(signal):
        return diagnostics.state_age(
            SimpleNamespace(
                last_reported=datetime.fromisoformat(signal["last_reported"])
            ),
            observed_at,
        )

    grid_age = age(snapshot["power"]["grid"])
    wall_age = diagnostics.state_age(
        SimpleNamespace(
            last_reported=datetime.fromisoformat(
                snapshot["wall_connector"]["power_last_reported"]
            )
        ),
        observed_at,
    )
    soc_age = age(snapshot["power"]["home_battery_soc"])
    identity_age = diagnostics.state_age(
        SimpleNamespace(
            last_reported=datetime.fromisoformat(
                snapshot["tesla"]["connected_last_reported"]
            )
        ),
        observed_at,
    )
    assert grid_age is not None and grid_age <= 180
    assert wall_age is not None and wall_age > 75
    assert soc_age is not None and soc_age > 300
    assert identity_age is not None and identity_age > 300
    inputs_fresh = all(
        value is not None and value <= limit
        for value, limit in (
            (grid_age, 180),
            (wall_age, 75),
            (soc_age, 300),
            (identity_age, 300),
        )
    )
    assert inputs_fresh is False

    observed = {
        "now_monotonic": 0,
        "grid_power_w": snapshot["power"]["grid"]["value"],
        "battery_power_w": snapshot["power"]["battery"]["value"],
        "battery_soc": snapshot["power"]["home_battery_soc"]["value"],
        "ev_power_w": snapshot["wall_connector"]["power_kw"] * 1000,
        # Synthetic voltage keeps this replay independent of the later 21:59:13 readout.
        "wall_voltage_v": 240,
        "vehicle": "tesla",
        "inputs_fresh": inputs_fresh,
    }
    decision = engine_module.SolarControlEngine(
        models.ControlConfig(start_hold_seconds=0)
    ).evaluate(models.ControlInputs(**observed), amp_step=1)
    assert decision.should_charge is False
    assert decision.reason == "stale_or_unavailable_input"


def test_observed_zero_wall_power_is_stale_and_cannot_prove_physical_feedback():
    wall = _read("observed_ha_snapshot.json")["wall_connector"]
    assert wall["power_kw"] == 0.0 and wall["current_a"] == 0.0
    assert wall["connected"] == "off"
    assert wall["status"] == "not_connected"
    observed_at = datetime.fromisoformat(
        _read("observed_ha_snapshot.json")["provenance"]["observed_at"]
    )
    stamp = datetime.fromisoformat(wall["power_last_reported"])
    assert diagnostics.state_age(SimpleNamespace(last_reported=stamp), observed_at) > 75


def test_stale_zero_after_off_ack_does_not_count_as_physical_stop_feedback(monkeypatch):
    pytest.importorskip("homeassistant")
    from datetime import UTC, datetime, timedelta

    from test_solar_feedback import _coordinator, _state, _States, _with_clock

    from homeassistant.exceptions import HomeAssistantError

    stamp = datetime.now(UTC) - timedelta(seconds=120)
    states = _States(
        {"switch.ev": _state("off", stamp), "sensor.wall": _state("0", stamp)}
    )
    coordinator = _coordinator(states)
    _with_clock(monkeypatch)
    journal = {"switch_entity": "switch.ev", "wall_power_entity": "sensor.wall"}
    with pytest.raises(HomeAssistantError, match="Physical stop feedback"):
        __import__("asyncio").run(
            coordinator._async_stop_with_feedback(journal, "stop")
        )


@pytest.mark.parametrize(
    ("switch_entity", "capability_reason", "evaluation_reason"),
    [
        (None, "physical_stop_switch_not_mapped", "stale_vehicle_input"),
        (
            "switch.r1s_charge",
            "physical_stop_not_commissioned",
            "physical_stop_not_commissioned",
        ),
    ],
)
def test_non_dry_run_solar_rivian_cannot_actuate_without_physical_stop_proof(
    switch_entity, capability_reason, evaluation_reason
):
    pytest.importorskip("homeassistant")
    from custom_components.rivian.solar_ev_charging.coordinator import (
        SolarEvCoordinator,
    )

    stamp = datetime.now(UTC)

    def state(value, unit=None):
        return SimpleNamespace(
            state=value,
            last_reported=stamp,
            last_updated=stamp,
            attributes={"unit_of_measurement": unit} if unit else {},
        )

    config = {
        const.CONF_VEHICLE_MODE: const.VEHICLE_RIVIAN,
        const.CONF_DRY_RUN: False,
        const.CONF_RIVIAN_CONTROL_CONFIRMED: True,
        const.CONF_WALL_CONNECTED: "binary_sensor.wall_connected",
        const.CONF_WALL_POWER: "sensor.wall_power",
        const.CONF_WALL_VOLTAGE: "sensor.wall_voltage",
        const.CONF_RIVIAN_CONNECTED: "binary_sensor.r1s_connected",
        const.CONF_RIVIAN_SOC: "sensor.r1s_soc",
        const.CONF_RIVIAN_LIMIT: "sensor.r1s_limit",
        const.CONF_RIVIAN_HOME: "device_tracker.r1s",
        const.CONF_RIVIAN_SWITCH: switch_entity,
    }
    values = {
        "binary_sensor.wall_connected": state("on"),
        "binary_sensor.r1s_connected": state("on"),
        "sensor.r1s_soc": state("40", "%"),
        "sensor.r1s_limit": state("80", "%"),
        "device_tracker.r1s": state("home"),
    }
    if switch_entity:
        values[switch_entity] = state("off")

    coordinator = object.__new__(SolarEvCoordinator)
    coordinator.config = coordinator.options = config
    coordinator.hass = SimpleNamespace(states=SimpleNamespace(get=values.get))
    coordinator._runtime = {
        "enabled": True,
        "manual_override": False,
        "rivian_compatible": True,
        "commission": {},
    }
    coordinator._fault = None
    coordinator._last_vehicle = None
    coordinator._initialized = True
    coordinator._engine = engine_module.SolarControlEngine(
        models.ControlConfig(start_hold_seconds=0)
    )
    coordinator._control_inputs = lambda _vehicle: models.ControlInputs(
        now_monotonic=0,
        grid_power_w=-4000,
        battery_power_w=500,
        battery_soc=60,
        ev_power_w=0,
        wall_voltage_v=240,
        vehicle=const.VEHICLE_RIVIAN,
        inputs_fresh=True,
    )
    coordinator._vehicle_identity_reason = lambda *_args, **_kwargs: None
    coordinator._async_safe_release_locked = AsyncMock()
    coordinator._async_apply_vehicle = AsyncMock()
    coordinator._status = lambda *_args: {"reason": _args[-1]}

    assert (
        coordinator._capability_reason(const.VEHICLE_RIVIAN, commissioning=False)
        == capability_reason
    )
    result = asyncio.run(coordinator._async_evaluate())

    assert coordinator.dry_run is False
    assert result["reason"] == evaluation_reason
    coordinator._async_apply_vehicle.assert_not_awaited()
