"""The entities Rivian schedule control's controller options point at: the heartbeat
(liveness) and each car's charging-wanted binary sensor."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import RIVIAN, TESLA, FakeCars, cycle, make_entry, set_rivian, setup_entry, turn

HEARTBEAT = "sensor.solar_ev_planner_heartbeat"
WANTED = "binary_sensor.r1s_planner_charging_wanted"
SHADOW = "switch.solar_ev_planner_shadow_mode"
PLANNING = "switch.solar_ev_planner_planning"


@pytest.fixture(autouse=True)
def _ha(site: HomeAssistant) -> None:
    """Noon on a sunny Monday with the Rivian plugged in (conftest.site)."""


async def test_heartbeat_follows_the_planner(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    # running in shadow mode: alive, so the schedule control's own trial is not blocked
    assert hass.states.get(HEARTBEAT).state == "shadow"
    await turn(hass, SHADOW, False)
    assert hass.states.get(HEARTBEAT).state == "active"
    # planner off: stopped, and the reason says what that means for each car
    await turn(hass, PLANNING, False)
    assert hass.states.get(HEARTBEAT).state == STATE_UNAVAILABLE
    reason = hass.states.get("sensor.solar_ev_planner_reason").state
    assert "sends nothing" in reason and "parks" in reason
    await turn(hass, PLANNING, True)
    assert hass.states.get(HEARTBEAT).state == "active"
    # failing: the cycle raises
    with patch.object(coordinator.planner, "decide", side_effect=RuntimeError("bug")):
        await cycle(hass, coordinator, freezer)
    assert not coordinator.last_update_success
    assert hass.states.get(HEARTBEAT).state == STATE_UNAVAILABLE
    await cycle(hass, coordinator, freezer)
    assert hass.states.get(HEARTBEAT).state == "active"


async def test_a_stuck_planner_makes_its_heartbeat_unavailable(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN), fake_cars)
    assert hass.states.get(HEARTBEAT).state == "shadow"
    # no decision for more than 3 minutes (the cycle hangs): the heartbeat re-checks itself
    coordinator.last_success = dt_util.now() - timedelta(minutes=4)
    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(HEARTBEAT).state == STATE_UNAVAILABLE


async def test_charging_wanted_follows_the_commands(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    hass = site
    coordinator = await setup_entry(hass, make_entry(RIVIAN, TESLA), fake_cars)
    # shadow mode: unknown, so the schedule control neither confirms nor drops requests
    assert hass.states.get(WANTED).state == "unknown"
    assert hass.states.get("binary_sensor.roxxo_planner_charging_wanted").state == "unknown"
    await turn(hass, SHADOW, False)
    assert hass.states.get(WANTED).state == "on"
    assert hass.states.get("switch.r1s_schedule_control_solar_charging").state == "on"
    # daytime solar charging off: the car pauses, in step with the schedule control's switch
    await turn(hass, "switch.solar_ev_planner_daytime_solar_charging", False)
    await cycle(hass, coordinator, freezer, minutes=16)
    assert hass.states.get("switch.r1s_schedule_control_solar_charging").state == "off"
    assert hass.states.get(WANTED).state == "off"
    # back to shadow mode: no decision again
    await turn(hass, SHADOW, True)
    assert hass.states.get(WANTED).state == "unknown"
    await turn(hass, SHADOW, False)
    # unplugged: the session (and the schedule control's request) ends
    set_rivian(hass, plugged="off")
    await cycle(hass, coordinator, freezer)
    assert hass.states.get(WANTED).state == "unknown"
    # plugged in again: a new session's first command (a pause: daytime solar is still off)
    set_rivian(hass)
    await cycle(hass, coordinator, freezer)
    assert hass.states.get(WANTED).state == "off"
    # planner off: unknown
    await turn(hass, PLANNING, False)
    assert hass.states.get(WANTED).state == "unknown"


async def test_charging_wanted_survives_a_restart(
    site: HomeAssistant, fake_cars: FakeCars, freezer: FrozenDateTimeFactory
) -> None:
    """After a reload the sensor shows the stored command at once, so Rivian schedule control
    can confirm its request from before the restart."""
    hass = site
    entry = make_entry(RIVIAN)
    await setup_entry(hass, entry, fake_cars)
    await turn(hass, SHADOW, False)
    assert hass.states.get(WANTED).state == "on"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(WANTED).state == "on"
    assert hass.states.get(HEARTBEAT).state == "active"
