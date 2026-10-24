"""Focused tests for the read-only solar input diagnostics contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.rivian.solar_ev_charging import const
from custom_components.rivian.solar_ev_charging.coordinator import SolarEvCoordinator


class _States:
    def __init__(self, values: dict[str, SimpleNamespace]) -> None:
        self.values = values

    def get(self, entity_id: str | None):
        return self.values.get(entity_id)


def _state(value: str, stamp: datetime, unit: str | None = "W") -> SimpleNamespace:
    return SimpleNamespace(
        state=value,
        last_updated=stamp,
        last_reported=stamp,
        attributes={"unit_of_measurement": unit} if unit is not None else {},
    )


def _coordinator(states: _States, config: dict[str, str]) -> SolarEvCoordinator:
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator.hass = SimpleNamespace(states=states)
    coordinator.options = {const.CONF_VEHICLE_MODE: "unsupported-test-mode"}
    coordinator.config = config
    return coordinator


def _by_label(items: list[dict[str, object]], label: str) -> dict[str, object]:
    return next(item for item in items if item["label"] == label)


def test_stale_wall_power_is_not_rescued_by_fresh_voltage() -> None:
    now = datetime.now(UTC)
    config = {
        const.CONF_WALL_POWER: "sensor.wall_power",
        const.CONF_WALL_VOLTAGE: "sensor.wall_voltage",
    }
    states = _States(
        {
            "sensor.wall_power": _state("0", now - timedelta(seconds=90)),
            "sensor.wall_voltage": _state("240", now, unit="V"),
        }
    )
    items = _coordinator(states, config)._input_diagnostics(None)
    assert _by_label(items, "Wall power")["status"] == "stale"
    assert _by_label(items, "Wall voltage")["status"] == "ok"


def test_invalid_missing_and_future_inputs_are_distinguished() -> None:
    now = datetime.now(UTC)
    config = {
        const.CONF_GRID_POWER: "sensor.grid",
        const.CONF_BATTERY_POWER: "sensor.battery",
        const.CONF_WALL_VOLTAGE: "sensor.voltage",
    }
    states = _States(
        {
            "sensor.grid": _state("4", now, unit="kWh"),
            "sensor.voltage": _state("240", now + timedelta(seconds=30), unit="V"),
        }
    )
    items = _coordinator(states, config)._input_diagnostics(None)
    assert _by_label(items, "Grid power")["status"] == "invalid"
    assert _by_label(items, "Battery power")["status"] == "missing"
    assert _by_label(items, "Wall voltage")["status"] == "future"


def test_explicit_kw_power_is_normalized_to_watts() -> None:
    now = datetime.now(UTC)
    config = {const.CONF_GRID_POWER: "sensor.grid"}
    states = _States({"sensor.grid": _state("1.5", now, unit="kW")})
    item = _by_label(
        _coordinator(states, config)._input_diagnostics(None), "Grid power"
    )
    assert item["status"] == "ok"
    assert item["normalized_value"] == pytest.approx(1500)


@pytest.mark.parametrize("state,expected", [("home", True), ("not_home", False)])
def test_tracker_home_diagnostics_match_control_reader(state, expected) -> None:
    config = {const.CONF_RIVIAN_HOME: "device_tracker.vehicle"}
    states = _States({"device_tracker.vehicle": _state(state, datetime.now(UTC), None)})
    coordinator = _coordinator(states, config)
    coordinator.options = {const.CONF_VEHICLE_MODE: "rivian"}
    item = _by_label(coordinator._input_diagnostics("rivian"), "Rivian home")
    assert item["status"] == "ok"
    assert item["normalized_value"] is expected
    assert coordinator._home_state("rivian") is expected
