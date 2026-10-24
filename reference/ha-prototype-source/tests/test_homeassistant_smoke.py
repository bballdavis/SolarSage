"""Home Assistant-facing smoke and safety tests.

These tests are skipped by the dependency-free local unit run and execute in CI,
where the pinned Home Assistant runtime is installed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("homeassistant")

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.rivian import (
    SERVICE_GET_SCHEDULES,
    SERVICE_SET_SCHEDULES,
    _register_services,
    _unregister_services,
)
from custom_components.rivian.config_flow import (
    OPTIONS_SCHEMA,
    validate_vehicle_control,
)
from custom_components.rivian.const import DOMAIN
from custom_components.rivian.solar_ev_charging import const as solar_const
from custom_components.rivian.solar_ev_charging.coordinator import SolarEvCoordinator
from custom_components.rivian.solar_ev_charging.engine import SolarControlEngine
from custom_components.rivian.solar_ev_charging.models import (
    ControlConfig,
    ControlDecision,
    ControlInputs,
)
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.schema_config_entry_flow import SchemaFlowError


def _entity_id(option: str) -> str:
    if "schedule" in option:
        return f"schedule.{option}"
    if "amps" in option or "limit" in option:
        return f"number.{option}"
    if "switch" in option:
        return f"switch.{option}"
    if "connected" in option:
        return f"binary_sensor.{option}"
    return f"sensor.{option}"


def _mode_options(mode: str) -> dict[str, object]:
    required = list(solar_const.SOLAR_BASE_REQUIRED_OPTIONS)
    if mode in (solar_const.VEHICLE_TESLA, solar_const.VEHICLE_MODE_BOTH):
        required.extend(solar_const.SOLAR_TESLA_REQUIRED_OPTIONS)
    if mode in (solar_const.VEHICLE_RIVIAN, solar_const.VEHICLE_MODE_BOTH):
        required.extend(solar_const.SOLAR_RIVIAN_REQUIRED_OPTIONS)
    return {
        **{key: _entity_id(key) for key in required},
        solar_const.CONF_SOLAR_ENABLED: True,
        solar_const.CONF_VEHICLE_MODE: mode,
        solar_const.CONF_DRY_RUN: True,
    }


@pytest.mark.parametrize(
    "mode",
    [
        solar_const.VEHICLE_RIVIAN,
        solar_const.VEHICLE_TESLA,
        solar_const.VEHICLE_MODE_BOTH,
    ],
)
def test_options_schema_accepts_each_vehicle_mode(mode: str) -> None:
    validated = OPTIONS_SCHEMA(_mode_options(mode))
    assert validated[solar_const.CONF_VEHICLE_MODE] == mode


def test_solar_validation_rejects_missing_mode_fields_before_api_access() -> None:
    async def run() -> None:
        with pytest.raises(SchemaFlowError, match="solar_required"):
            await validate_vehicle_control(
                SimpleNamespace(),
                {
                    solar_const.CONF_SOLAR_ENABLED: True,
                    solar_const.CONF_VEHICLE_MODE: solar_const.VEHICLE_RIVIAN,
                },
            )

    asyncio.run(run())


def test_schedule_services_are_response_only() -> None:
    class Services:
        def __init__(self) -> None:
            self.registered: dict[tuple[str, str], SupportsResponse] = {}

        def async_register(self, domain, service, handler, **kwargs) -> None:
            self.registered[(domain, service)] = kwargs["supports_response"]

        def async_remove(self, domain, service) -> None:
            self.registered.pop((domain, service))

    hass = SimpleNamespace(data={DOMAIN: {}}, services=Services())
    _register_services(hass)
    assert (
        hass.services.registered[(DOMAIN, SERVICE_GET_SCHEDULES)]
        is SupportsResponse.ONLY
    )
    assert (
        hass.services.registered[(DOMAIN, SERVICE_SET_SCHEDULES)]
        is SupportsResponse.ONLY
    )
    _unregister_services(hass)
    assert not hass.services.registered


def test_rivian_only_dry_run_never_calls_vehicle_commands() -> None:
    class States:
        def __init__(self, values: dict[str, str]) -> None:
            now = datetime.now(UTC)
            self.values = {
                entity_id: SimpleNamespace(
                    state=value, last_updated=now, last_reported=now
                )
                for entity_id, value in values.items()
            }

        def get(self, entity_id: str):
            return self.values.get(entity_id)

    options = _mode_options(solar_const.VEHICLE_RIVIAN)
    options[solar_const.CONF_START_HOLD_SECONDS] = 0
    values = {
        options[solar_const.CONF_GRID_POWER]: "-4000",
        options[solar_const.CONF_BATTERY_POWER]: "500",
        options[solar_const.CONF_BATTERY_SOC]: "60",
        options[solar_const.CONF_WALL_CONNECTED]: "on",
        options[solar_const.CONF_WALL_POWER]: "0",
        options[solar_const.CONF_WALL_VOLTAGE]: "240",
        options[solar_const.CONF_RIVIAN_CONNECTED]: "on",
        options[solar_const.CONF_RIVIAN_SOC]: "40",
        options[solar_const.CONF_RIVIAN_LIMIT]: "80",
    }
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator.config = options
    coordinator.options = options
    coordinator._runtime = {"enabled": True, "manual_override": False}
    coordinator._engine = SolarControlEngine(ControlConfig(start_hold_seconds=0))
    coordinator._last_vehicle = None
    coordinator._last_command = None
    coordinator._fault = None
    coordinator.hass = SimpleNamespace(states=States(values))
    coordinator._async_apply_vehicle = AsyncMock()
    coordinator._async_release_control = AsyncMock()

    decision = asyncio.run(coordinator._async_evaluate())

    assert decision["state"] == solar_const.STATE_SOLAR
    assert decision["reason"] == "dry_run"
    coordinator._async_apply_vehicle.assert_not_awaited()
    coordinator._async_release_control.assert_not_awaited()


def test_external_rivian_conflict_keeps_cleanup_journal() -> None:
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator._runtime = {
        "rivian_compatible": True,
        "journal": {
            "vehicle": solar_const.VEHICLE_RIVIAN,
            "switch_entity": "switch.rivian",
            "wall_power_entity": "sensor.wall_power",
            "vehicle_id": "vehicle",
            "base_schedules": [],
            "base_hash": "base",
            "last_hash": "owned",
            "pending_hash": None,
        },
    }
    coordinator._fault = None
    coordinator.store = SimpleNamespace(async_save=AsyncMock())
    coordinator._async_stop_with_feedback = AsyncMock()
    coordinator._async_rivian_get = AsyncMock(
        return_value={"schedules": [], "hash": "external"}
    )

    assert asyncio.run(coordinator._async_safe_release_locked("test")) is False

    assert coordinator._runtime["journal"] is not None
    assert coordinator._runtime["rivian_write_blocked"] is True
    assert "externally" in coordinator._fault


def test_rivian_restore_is_bracketed_by_physical_stops() -> None:
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator._runtime = {
        "journal": {
            "vehicle": solar_const.VEHICLE_RIVIAN,
            "switch_entity": "switch.rivian",
            "wall_power_entity": "sensor.wall_power",
            "vehicle_id": "vehicle",
            "base_schedules": [],
            "base_hash": "base",
            "last_hash": "owned",
            "pending_hash": None,
            "stop_required": True,
        }
    }
    coordinator.store = SimpleNamespace(async_save=AsyncMock())
    coordinator._last_rivian_renewal = 4
    events: list[str] = []

    async def stop(_journal, phase: str) -> None:
        events.append(phase)

    async def set_schedule(*_args) -> dict[str, str]:
        events.append("restore")
        return {"hash": "base"}

    coordinator._async_stop_with_feedback = stop
    coordinator._async_rivian_get = AsyncMock(return_value={"hash": "owned"})
    coordinator._async_rivian_set = set_schedule

    assert asyncio.run(coordinator._async_safe_release_locked("test")) is True
    assert events == ["stop_test", "restore", "post_restore_stop_test"]
    assert coordinator._runtime["journal"] is None


def test_noop_switch_service_does_not_count_as_a_physical_stop() -> None:
    coordinator = object.__new__(SolarEvCoordinator)
    now = datetime.now(UTC)
    coordinator.hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(
                state="off" if entity_id == "switch.rivian" else "200",
                last_updated=now,
                last_reported=now,
                attributes={},
            )
        )
    )
    coordinator._feedback_timeout = lambda: 0
    journal = {"switch_entity": "switch.rivian", "wall_power_entity": "sensor.wall"}

    with pytest.raises(HomeAssistantError, match="Physical stop feedback"):
        asyncio.run(coordinator._async_confirm_stop(journal, now))


def test_hard_safety_block_never_falls_through_to_normal_schedule() -> None:
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator.config = {solar_const.CONF_DRY_RUN: False}
    coordinator.options = coordinator.config
    coordinator._runtime = {"enabled": True, "manual_override": False}
    coordinator._fault = None
    coordinator._last_vehicle = None
    coordinator._connected_vehicle = lambda: solar_const.VEHICLE_TESLA
    coordinator._control_inputs = lambda vehicle: ControlInputs(
        now_monotonic=1,
        grid_power_w=2500,
        battery_power_w=0,
        battery_soc=60,
        ev_power_w=0,
        wall_voltage_v=240,
        vehicle=vehicle,
    )
    coordinator._engine = SimpleNamespace(
        evaluate=lambda *_args, **_kwargs: ControlDecision(
            solar_const.STATE_BLOCKED, 0, 0, "grid_importing", False
        )
    )
    coordinator._async_safe_release_locked = AsyncMock(return_value=True)
    coordinator._async_manage_normal_tesla = AsyncMock()
    coordinator._status_from_decision = lambda *_args: {}

    asyncio.run(coordinator._async_evaluate())

    coordinator._async_manage_normal_tesla.assert_not_awaited()


def test_cloud_stop_latch_never_falls_through_to_normal_schedule() -> None:
    coordinator = object.__new__(SolarEvCoordinator)
    coordinator.config = {solar_const.CONF_DRY_RUN: False}
    coordinator.options = coordinator.config
    coordinator._runtime = {"enabled": True, "manual_override": False}
    coordinator._fault = None
    coordinator._last_vehicle = None
    coordinator._connected_vehicle = lambda: solar_const.VEHICLE_TESLA
    coordinator._control_inputs = lambda vehicle: ControlInputs(
        now_monotonic=1,
        grid_power_w=0,
        battery_power_w=0,
        battery_soc=60,
        ev_power_w=0,
        wall_voltage_v=240,
        vehicle=vehicle,
    )
    coordinator._engine = SimpleNamespace(
        evaluate=lambda *_args, **_kwargs: ControlDecision(
            solar_const.STATE_NORMAL, 0, 0, "cloud_stop", False
        )
    )

    async def cloud_stop(_reason: str) -> bool:
        coordinator._fault = "cloud_stop"
        return False

    coordinator._async_safe_release_locked = cloud_stop
    coordinator._async_manage_normal_tesla = AsyncMock()
    coordinator._status_from_decision = lambda *_args: {}

    asyncio.run(coordinator._async_evaluate())

    coordinator._async_manage_normal_tesla.assert_not_awaited()
