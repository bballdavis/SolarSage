"""Shared test helpers.

Home Assistant fixtures here are opt-in. Only the Home Assistant test modules
enable custom integrations (with their own autouse fixture), so the pure core
tests stay fast. Nothing here talks to the network: the Rivian client is
replaced by an in-memory fake wherever the integration imports it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from copy import deepcopy
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from rivian.exceptions import RivianInvalidOTP

from custom_components.rivian_solar_adapter.const import (
    CONF_ACCESS_TOKEN,
    CONF_CHARGER_STATE_ENTITY,
    CONF_DRY_RUN,
    CONF_EMAIL,
    CONF_LOCATION_ENTITY,
    CONF_PLUGGED_ENTITY,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    CONF_VEHICLE_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
)

# Test data only: no real account, vehicle, token or location.
VEHICLE_ID = "test-vehicle-0001"
VEHICLE_NAME = "Test R1S"
EMAIL = "owner@example.invalid"
PASSWORD = "test-password"
OTP_CODE = "123456"
TOKENS = {
    CONF_ACCESS_TOKEN: "test-access-token",
    CONF_REFRESH_TOKEN: "test-refresh-token",
    CONF_USER_SESSION_TOKEN: "test-user-session-token",
}
NEW_TOKENS = {
    CONF_ACCESS_TOKEN: "test-access-token-2",
    CONF_REFRESH_TOKEN: "test-refresh-token-2",
    CONF_USER_SESSION_TOKEN: "test-user-session-token-2",
}

PLUGGED_ENTITY = "binary_sensor.test_r1s_charger_connection"
LOCATION_ENTITY = "device_tracker.test_r1s_location"
CHARGER_STATE_ENTITY = "sensor.test_r1s_charger_state"
WALL_CONNECTOR_ENTITY = "binary_sensor.test_wall_connector_vehicle_connected"

# The pytest Home Assistant instance is at a fictional test location
# (32.87336, -117.22743); the owner's home schedule is near it, a second one
# is elsewhere.
OWNER_SCHEDULES: list[dict[str, Any]] = [
    {
        "weekDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "startTime": 0,
        "duration": 360,
        "location": {"latitude": 32.87341, "longitude": -117.22749},
        "amperage": 48,
        "enabled": True,
    },
    {
        "weekDays": ["Saturday"],
        "startTime": 600,
        "duration": 90,
        "location": {"latitude": 33.5, "longitude": -116.5},
        "amperage": 32,
        "enabled": False,
    },
]
COORDINATE_FRAGMENTS = ("32.87", "-117.22", "33.5", "-116.5")


class FakeResponse:
    """A response whose JSON body is read asynchronously, like aiohttp's."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def json(self) -> dict[str, Any]:
        return deepcopy(self._payload)


class FakeRivian:
    """In-memory stand-in for ``rivian.Rivian``.

    The instance is patched in place of the class: calling it "creates a
    client", which resets the login state and returns the same object, so a
    test sees every client the integration makes.
    """

    def __init__(self) -> None:
        self.schedules: list[dict[str, Any]] = deepcopy(OWNER_SCHEDULES)
        self.vehicles: list[dict[str, Any]] = [
            {
                "id": VEHICLE_ID,
                "name": VEHICLE_NAME,
                "vehicle": {"model": "R1S", "modelYear": 2025},
            }
        ]
        self.init_kwargs: list[dict[str, Any]] = []
        self.writes: list[list[dict[str, Any]]] = []
        self.reads = 0
        self.otp_required = False
        self.issued_tokens = dict(TOKENS)
        self.csrf_error: Exception | None = None
        self.login_error: Exception | None = None
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None
        self.read_gate: asyncio.Event | None = None
        self._reset_login({})

    def __call__(self, *args: Any, **kwargs: Any) -> FakeRivian:
        self.init_kwargs.append(kwargs)
        self._reset_login(kwargs)
        return self

    def _reset_login(self, kwargs: dict[str, Any]) -> None:
        self._otp_needed = False
        self._access_token = kwargs.get("access_token", "")
        self._refresh_token = kwargs.get("refresh_token", "")
        self._user_session_token = kwargs.get("user_session_token", "")

    def _issue_tokens(self) -> None:
        self._access_token = self.issued_tokens[CONF_ACCESS_TOKEN]
        self._refresh_token = self.issued_tokens[CONF_REFRESH_TOKEN]
        self._user_session_token = self.issued_tokens[CONF_USER_SESSION_TOKEN]

    async def create_csrf_token(self) -> None:
        if self.csrf_error:
            raise self.csrf_error

    async def authenticate(self, username: str, password: str) -> None:
        if self.login_error:
            raise self.login_error
        if self.otp_required:
            self._otp_needed = True
        else:
            self._issue_tokens()

    async def validate_otp(self, username: str, otp_code: str) -> None:
        if otp_code != OTP_CODE:
            raise RivianInvalidOTP(400, {"errors": [{"message": "invalid otp"}]})
        self._issue_tokens()

    async def get_user_information(self, include_phones: bool = False) -> FakeResponse:
        return FakeResponse({"data": {"currentUser": {"id": "user", "vehicles": self.vehicles}}})

    async def get_charging_schedules(self, vehicle_id: str) -> FakeResponse:
        assert vehicle_id == VEHICLE_ID
        self.reads += 1
        if self.read_gate is not None:
            await self.read_gate.wait()
        if self.read_error:
            raise self.read_error
        return FakeResponse(
            {"data": {"getVehicle": {"chargingSchedules": deepcopy(self.schedules)}}}
        )

    async def set_charging_schedules(
        self, vehicle_id: str, schedules: list[dict[str, Any]]
    ) -> FakeResponse:
        assert vehicle_id == VEHICLE_ID
        if self.write_error:
            raise self.write_error
        self.writes.append(deepcopy(schedules))
        self.schedules = deepcopy(schedules)
        return FakeResponse({"data": {"setChargingSchedules": {"success": True}}})

    async def close(self) -> None:
        """Nothing to close."""


@pytest.fixture
def mock_rivian() -> Generator[FakeRivian]:
    """Replace the Rivian client wherever the integration creates one."""
    fake = FakeRivian()
    with (
        patch("custom_components.rivian_solar_adapter.Rivian", new=fake),
        patch("custom_components.rivian_solar_adapter.config_flow.Rivian", new=fake),
    ):
        yield fake


def entry_options(**overrides: Any) -> dict[str, Any]:
    """Options as the config flow creates them, with schedule writes allowed."""
    return (
        {CONF_PLUGGED_ENTITY: PLUGGED_ENTITY, CONF_LOCATION_ENTITY: LOCATION_ENTITY}
        | DEFAULT_OPTIONS
        | {CONF_DRY_RUN: False, CONF_CHARGER_STATE_ENTITY: CHARGER_STATE_ENTITY}
        | overrides
    )


def make_entry(**option_overrides: Any) -> MockConfigEntry:
    """A config entry for the test vehicle."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=VEHICLE_NAME,
        unique_id=VEHICLE_ID,
        data={CONF_EMAIL: EMAIL, CONF_VEHICLE_ID: VEHICLE_ID, **TOKENS},
        options=entry_options(**option_overrides),
    )


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, mock_rivian: FakeRivian
) -> AsyncGenerator[MockConfigEntry]:
    """A loaded entry, plugged in at home, with schedule writes allowed."""
    hass.states.async_set(PLUGGED_ENTITY, "on")
    hass.states.async_set(LOCATION_ENTITY, "home")
    hass.states.async_set(CHARGER_STATE_ENTITY, "charging_ready")
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    yield entry
