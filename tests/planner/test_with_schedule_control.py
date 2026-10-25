"""The planner driving the real Rivian schedule control (with the in-memory Rivian fake).

The other planner tests stand in for the schedule control with plain states. Here both
integrations run, wired as docs/PLANNER.md says: the schedule control's controller liveness
option points at the planner's heartbeat, its controller charging option at the R1S's
charging-wanted sensor, and its vehicle override at the planner's charger car override. The
planner's car maps the schedule control's switch, number and control state. Nothing talks to
a real system.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.rivian_schedule_control.const import (
    CONF_ACCESS_TOKEN,
    CONF_CHARGING_ENTITY,
    CONF_DRY_RUN,
    CONF_EMAIL,
    CONF_LIVENESS_ENTITY,
    CONF_LOCATION_ENTITY,
    CONF_OVERRIDE_ENTITY,
    CONF_PLUGGED_ENTITY,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    CONF_VEHICLE_ID,
    CONF_WALL_CONNECTOR_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN as SCHEDULE_CONTROL_DOMAIN,
    ENTRY_MINOR_VERSION,
    ENTRY_VERSION,
)
from custom_components.solar_ev_planner.coordinator import PlannerCoordinator

from .conftest import (
    NOON_UTC,
    RIV,
    RIVIAN,
    TESLA,
    E,
    cycle,
    import_statistics,
    make_entry,
    remove_free_hours,
    set_site,
    set_tesla,
    setup_entry,
    setup_free_hours,
    turn,
)

HEARTBEAT = "sensor.solar_ev_planner_heartbeat"
WANTED = "binary_sensor.r1s_planner_charging_wanted"
OVERRIDE = "select.solar_ev_planner_charger_car_override"
SHADOW = "switch.solar_ev_planner_shadow_mode"
PLANNING = "switch.solar_ev_planner_planning"


def set_rivian_telemetry(hass: HomeAssistant, *, plugged: str = "on", soc: float = 46) -> None:
    """The upstream Rivian integration's entities (Rivian schedule control provides the
    controls)."""
    hass.states.async_set(RIV["soc"], str(soc), {"unit_of_measurement": "%"})
    hass.states.async_set(RIV["limit"], "85", {"unit_of_measurement": "%"})
    hass.states.async_set(RIV["plugged"], plugged)
    hass.states.async_set(RIV["location"], "home")


def schedule_control_entry(vehicle_id: str, **options: Any) -> MockConfigEntry:
    """Rivian schedule control for the R1S (entry title R1S), mapped to the planner's
    entities."""
    return MockConfigEntry(
        domain=SCHEDULE_CONTROL_DOMAIN,
        title="R1S",
        unique_id=vehicle_id,
        data={
            CONF_EMAIL: "owner@example.invalid",
            CONF_VEHICLE_ID: vehicle_id,
            CONF_ACCESS_TOKEN: "test-access-token",
            CONF_REFRESH_TOKEN: "test-refresh-token",
            CONF_USER_SESSION_TOKEN: "test-user-session-token",
        },
        options=DEFAULT_OPTIONS
        | {
            CONF_PLUGGED_ENTITY: RIV["plugged"],
            CONF_LOCATION_ENTITY: RIV["location"],
            CONF_WALL_CONNECTOR_ENTITY: E["wall_connected"],
            CONF_LIVENESS_ENTITY: HEARTBEAT,
            CONF_CHARGING_ENTITY: WANTED,
            CONF_OVERRIDE_ENTITY: OVERRIDE,
            CONF_DRY_RUN: False,
        }
        | options,
        version=ENTRY_VERSION,
        minor_version=ENTRY_MINOR_VERSION,
    )


def control(hass: HomeAssistant) -> dict[str, Any]:
    state = hass.states.get(RIV["control_state"])
    assert state is not None
    return {"state": state.state, **state.attributes}


def lease_amps(writes: list[list[dict[str, Any]]]) -> list[int]:
    """The amperage of each window Rivian schedule control wrote (one window per write)."""
    return [w[0]["amperage"] for w in writes if len(w) == 1]


async def tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    """Let time pass in Rivian schedule control's 15-second ticks, without a planning cycle."""
    step = timedelta(seconds=15)
    left = timedelta(seconds=seconds)
    while left > timedelta(0):
        freezer.tick(step)
        left -= step
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture
async def both(
    planner_hass: HomeAssistant, mock_rivian: Any, freezer: FrozenDateTimeFactory
) -> AsyncGenerator[tuple[HomeAssistant, PlannerCoordinator, Any]]:
    """Noon on a sunny Monday, the R1S plugged in at home; the planner (in its default shadow
    mode) and then Rivian schedule control (dry run off) set up."""
    freezer.move_to(NOON_UTC)
    hass = planner_hass
    await import_statistics(hass)
    set_site(hass)
    set_rivian_telemetry(hass)
    set_tesla(hass)
    await setup_free_hours(hass)
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA))
    entry = schedule_control_entry(mock_rivian.vehicles[0]["id"])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await cycle(hass, coordinator, freezer)
    yield hass, coordinator, mock_rivian
    await hass.config_entries.async_unload(entry.entry_id)
    await remove_free_hours(hass)


async def test_shadow_mode_leaves_the_rivian_alone(
    both: tuple[HomeAssistant, PlannerCoordinator, Any], freezer: FrozenDateTimeFactory
) -> None:
    hass, coordinator, rivian = both
    for _ in range(3):
        await cycle(hass, coordinator, freezer, minutes=5)
    assert coordinator.data.mode == "day"
    assert int(hass.states.get("sensor.r1s_planner_planned_current").state) >= 8
    # the schedule control sees the planner alive (so the attended trial is not blocked),
    # no request
    assert hass.states.get(HEARTBEAT).state == "shadow"
    assert hass.states.get(WANTED).state == "unknown"
    c = control(hass)
    assert c["state"] == "idle"
    assert c["desired_charge"] is None
    assert c["observation"]["controller_alive"] is True
    assert c["observation"]["controller_charging"] is None
    assert "controller_unavailable" not in c["start_blocked_by"]
    # nothing written to Rivian, the owner's schedule untouched
    assert rivian.writes == []
    assert hass.states.get("sensor.r1s_schedule_control_charger_status").state == "b"


async def test_planner_commands_schedule_control(
    both: tuple[HomeAssistant, PlannerCoordinator, Any], freezer: FrozenDateTimeFactory
) -> None:
    hass, coordinator, rivian = both
    await turn(hass, SHADOW, False)
    await tick(hass, freezer, 30)
    amps = coordinator.runtime["rivian"].commanded_a
    assert amps is not None and amps >= 8 and amps % 2 == 0
    c = control(hass)
    assert c["desired_charge"] is True
    assert c["requested_amps"] == amps
    assert c["state"] == "lease_active"
    assert c["lease_amps"] == amps
    assert lease_amps(rivian.writes) == [amps]  # the owner's schedule was snapshotted first
    assert hass.states.get(WANTED).state == "on"
    assert c["observation"]["controller_charging"] is True
    assert hass.states.get(HEARTBEAT).state == "active"

    # unchanged decisions: the planner sends nothing, the schedule control writes nothing new
    writes = len(rivian.writes)
    for _ in range(3):
        await cycle(hass, coordinator, freezer)
    await tick(hass, freezer, 30)
    assert len(rivian.writes) == writes

    # a pause is the switch, never 0 A
    await coordinator.executor.async_apply(
        coordinator.cars["rivian"], coordinator.runtime["rivian"], 0, dt_util.now()
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    await tick(hass, freezer, 30)
    c = control(hass)
    assert c["desired_charge"] is False
    assert c["requested_amps"] == amps
    assert c["state"] == "paused"
    coordinator.async_update_listeners()
    assert hass.states.get(WANTED).state == "off"


async def test_planning_off_parks_the_rivian_and_on_resumes(
    both: tuple[HomeAssistant, PlannerCoordinator, Any], freezer: FrozenDateTimeFactory
) -> None:
    hass, coordinator, rivian = both
    await turn(hass, SHADOW, False)
    await tick(hass, freezer, 30)
    amps = coordinator.runtime["rivian"].commanded_a
    assert control(hass)["state"] == "lease_active"

    # Planning off: the heartbeat goes unavailable; the schedule control stops renewing at
    # once and parks the window after 3 minutes
    await turn(hass, PLANNING, False)
    assert hass.states.get(HEARTBEAT).state == "unavailable"
    assert hass.states.get(WANTED).state == "unknown"
    await tick(hass, freezer, 15)
    c = control(hass)
    assert c["observation"]["controller_alive"] is False
    assert "controller_unavailable" in c["start_blocked_by"]
    await tick(hass, freezer, 4 * 60)
    c = control(hass)
    assert c["request_held"] is True
    assert c["state"] == "paused"

    # Planning on again: the heartbeat is back and charging-wanted (the last command
    # accepted) confirms the held request, so charging resumes
    await turn(hass, PLANNING, True)
    assert hass.states.get(HEARTBEAT).state == "active"
    await tick(hass, freezer, 30)
    c = control(hass)
    assert c["request_held"] is False
    assert c["desired_charge"] is True
    assert c["state"] == "lease_active"
    assert rivian.writes[-1][0]["amperage"] == c["lease_amps"]
    assert c["lease_amps"] <= max(amps or 0, 48)


async def test_unplug_ends_the_session_on_both_sides(
    both: tuple[HomeAssistant, PlannerCoordinator, Any], freezer: FrozenDateTimeFactory
) -> None:
    hass, coordinator, rivian = both
    await turn(hass, SHADOW, False)
    await tick(hass, freezer, 30)
    assert control(hass)["state"] == "lease_active"
    set_rivian_telemetry(hass, plugged="off")
    hass.states.async_set(E["wall_connected"], "off")
    await cycle(hass, coordinator, freezer)
    await tick(hass, freezer, 120)  # past the schedule control's release delay (60 s)
    c = control(hass)
    assert c["desired_charge"] is None
    assert c["state"] == "idle"
    assert hass.states.get(WANTED).state == "unknown"
    assert coordinator.runtime["rivian"].commanded_a is None
    # the owner's schedule is back exactly
    assert rivian.schedules == rivian.writes[-1]
    assert len(rivian.schedules) == 2

    # the next plug-in is a new session: the planner commands again
    set_rivian_telemetry(hass, plugged="on")
    hass.states.async_set(E["wall_connected"], "on")
    await cycle(hass, coordinator, freezer)
    await tick(hass, freezer, 30)
    c = control(hass)
    assert c["desired_charge"] is True
    assert c["state"] == "lease_active"


async def test_override_naming_the_tesla(
    both: tuple[HomeAssistant, PlannerCoordinator, Any], freezer: FrozenDateTimeFactory
) -> None:
    """The planner's override select is also Rivian schedule control's vehicle override."""
    hass, coordinator, rivian = both
    await hass.services.async_call(
        "select", "select_option", {"entity_id": OVERRIDE, "option": "tesla"}, blocking=True
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await tick(hass, freezer, 15)
    assert hass.states.get("sensor.r1s_schedule_control_charger_status").state == "a"
    assert coordinator.data.identification.car_id is None  # the Tesla is not plugged in
    await turn(hass, SHADOW, False)
    await tick(hass, freezer, 30)
    assert rivian.writes == []
    assert control(hass)["desired_charge"] is None
