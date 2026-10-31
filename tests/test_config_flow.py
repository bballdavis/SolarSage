"""Tests for the config, reauth and options flows."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from conftest import (
    CHARGER_STATE_ENTITY,
    CHARGING_ENTITY,
    EMAIL,
    LIVENESS_ENTITY,
    LOCATION_ENTITY,
    NEW_TOKENS,
    OTP_CODE,
    OVERRIDE_ENTITY,
    PASSWORD,
    PLUGGED_ENTITY,
    TOKENS,
    VEHICLE_ID,
    VEHICLE_NAME,
    WALL_CONNECTOR_ENTITY,
    FakeRivian,
    make_entry,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import pytest
from rivian import Rivian as RealRivian
from rivian.exceptions import (
    RivianApiException,
    RivianInvalidCredentials,
    RivianTemporarilyLockedError,
    RivianUnauthenticated,
)

from custom_components.rivian_schedule_control.const import (
    CONF_CEILING_HOLD,
    CONF_CHARGER_STATE_ENTITY,
    CONF_CHARGING_ENTITY,
    CONF_COMMUNITY_PATTERN,
    CONF_DRY_RUN,
    CONF_EMAIL,
    CONF_INCREASE_MARGIN,
    CONF_LEASE_MINUTES,
    CONF_LIVENESS_ENTITY,
    CONF_LOCATION_ENTITY,
    CONF_OTP,
    CONF_OVERRIDE_ENTITY,
    CONF_PASSWORD,
    CONF_PLUGGED_ENTITY,
    CONF_RENEW_BEFORE_MINUTES,
    CONF_RESTORE_ENABLED_ONLY,
    CONF_VEHICLE_ID,
    CONF_WALL_CONNECTOR_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
    ENTRY_MINOR_VERSION,
    ENTRY_VERSION,
)

INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / "rivian_schedule_control"

SECOND_VEHICLE = {
    "id": "test-vehicle-0002",
    "name": None,
    "vehicle": {"model": "R1T", "modelYear": 2024},
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load this repository's integration."""


@pytest.fixture
async def mock_setup_entry(hass: HomeAssistant) -> AsyncGenerator[AsyncMock]:
    """Do not set up the entry a flow creates or reloads."""
    with (
        patch(
            "custom_components.rivian_schedule_control.async_setup_entry", return_value=True
        ) as mock,
        patch("custom_components.rivian_schedule_control.async_unload_entry", return_value=True),
    ):
        yield mock
        # Unload while the patches still apply: the entries have no runtime.
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)


async def _sign_in(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )


async def _finish(hass: HomeAssistant, result: dict, vehicle_id: str = VEHICLE_ID) -> dict:
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "vehicle"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_VEHICLE_ID: vehicle_id}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "entities"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PLUGGED_ENTITY: PLUGGED_ENTITY,
            CONF_LOCATION_ENTITY: LOCATION_ENTITY,
            CONF_CHARGER_STATE_ENTITY: CHARGER_STATE_ENTITY,
        },
    )


async def test_user_flow_without_otp(
    hass: HomeAssistant, mock_rivian: FakeRivian, mock_setup_entry: AsyncMock
) -> None:
    """Sign in, pick the vehicle, map entities; the password is never stored."""
    result = await _finish(hass, await _sign_in(hass))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == VEHICLE_NAME
    assert result["result"].unique_id == VEHICLE_ID
    assert result["data"] == {CONF_EMAIL: EMAIL, CONF_VEHICLE_ID: VEHICLE_ID, **TOKENS}
    assert PASSWORD not in str(result["data"]) + str(result["options"])
    assert result["options"] == {
        CONF_PLUGGED_ENTITY: PLUGGED_ENTITY,
        CONF_LOCATION_ENTITY: LOCATION_ENTITY,
        CONF_CHARGER_STATE_ENTITY: CHARGER_STATE_ENTITY,
        **DEFAULT_OPTIONS,
    }
    assert result["options"][CONF_DRY_RUN] is True
    # The evcc-era increase rules are off for the planner.
    assert result["options"][CONF_INCREASE_MARGIN] == 0
    assert result["options"][CONF_CEILING_HOLD] == 0
    # The restore fallback and the community pattern are opt-in.
    assert result["options"][CONF_RESTORE_ENABLED_ONLY] is False
    assert result["options"][CONF_COMMUNITY_PATTERN] is False
    entry = result["result"]
    assert (entry.version, entry.minor_version) == (ENTRY_VERSION, ENTRY_MINOR_VERSION)
    assert mock_rivian.init_kwargs[0]["request_timeout"] == 30
    assert len(mock_setup_entry.mock_calls) == 1


async def test_user_flow_with_otp(
    hass: HomeAssistant, mock_rivian: FakeRivian, mock_setup_entry: AsyncMock
) -> None:
    """A login that needs a one-time code asks for it and retries a wrong one."""
    mock_rivian.otp_required = True
    mock_rivian.vehicles.append(SECOND_VEHICLE)
    result = await _sign_in(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "otp"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_OTP: "000000"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "otp"
    assert result["errors"] == {"base": "invalid_otp"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_OTP: OTP_CODE})
    vehicle_options = result["data_schema"].schema[CONF_VEHICLE_ID].config["options"]
    assert [option["value"] for option in vehicle_options] == [VEHICLE_ID, SECOND_VEHICLE["id"]]
    assert vehicle_options[1]["label"] == "2024 R1T"

    result = await _finish(hass, result, SECOND_VEHICLE["id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "2024 R1T"
    assert result["data"][CONF_VEHICLE_ID] == SECOND_VEHICLE["id"]
    assert result["data"]["access_token"] == TOKENS["access_token"]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RivianInvalidCredentials(401, {}), "invalid_auth"),
        (RivianUnauthenticated(401, {}), "invalid_auth"),
        (RivianTemporarilyLockedError(429, {}), "locked"),
        (RivianApiException("boom"), "cannot_connect"),
        (KeyError("login"), "unknown"),
    ],
)
async def test_user_flow_login_errors(
    hass: HomeAssistant,
    mock_rivian: FakeRivian,
    mock_setup_entry: AsyncMock,
    error: Exception,
    reason: str,
) -> None:
    """Login errors are shown on the form, and the user can try again."""
    mock_rivian.login_error = error
    result = await _sign_in(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": reason}

    mock_rivian.login_error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    result = await _finish(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_duplicate_vehicle(
    hass: HomeAssistant, mock_rivian: FakeRivian, mock_setup_entry: AsyncMock
) -> None:
    """A vehicle that already has Rivian schedule control set up is not added twice."""
    make_entry().add_to_hass(hass)
    result = await _sign_in(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_VEHICLE_ID: VEHICLE_ID}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_no_vehicles(
    hass: HomeAssistant, mock_rivian: FakeRivian, mock_setup_entry: AsyncMock
) -> None:
    """An account without vehicles cannot be set up."""
    mock_rivian.vehicles = []
    result = await _sign_in(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_vehicles"


async def test_reauth_updates_tokens(
    hass: HomeAssistant, mock_rivian: FakeRivian, mock_setup_entry: AsyncMock
) -> None:
    """Reauthentication stores the new session tokens and reloads the entry."""
    entry = make_entry()
    entry.add_to_hass(hass)
    mock_rivian.issued_tokens = dict(NEW_TOKENS)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    mock_rivian.login_error = RivianInvalidCredentials(401, {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    mock_rivian.login_error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_VEHICLE_ID: VEHICLE_ID, **NEW_TOKENS}
    assert len(mock_setup_entry.mock_calls) == 1


async def test_reauth_with_otp(
    hass: HomeAssistant, mock_rivian: FakeRivian, mock_setup_entry: AsyncMock
) -> None:
    """Reauthentication asks for a one-time code when Rivian requires one."""
    entry = make_entry()
    entry.add_to_hass(hass)
    mock_rivian.issued_tokens = dict(NEW_TOKENS)
    mock_rivian.otp_required = True

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    assert result["step_id"] == "otp"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_OTP: OTP_CODE})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["user_session_token"] == NEW_TOKENS["user_session_token"]


async def test_reauth_wrong_account(
    hass: HomeAssistant, mock_rivian: FakeRivian, mock_setup_entry: AsyncMock
) -> None:
    """Signing in to an account without the vehicle does not replace the tokens."""
    entry = make_entry()
    entry.add_to_hass(hass)
    mock_rivian.vehicles = [SECOND_VEHICLE]
    mock_rivian.issued_tokens = dict(NEW_TOKENS)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["access_token"] == TOKENS["access_token"]


async def test_options_flow(
    hass: HomeAssistant, mock_rivian: FakeRivian, mock_setup_entry: AsyncMock
) -> None:
    """Options are validated, stored as integers, and optional entities can be cleared."""
    entry = make_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    base = {
        CONF_PLUGGED_ENTITY: PLUGGED_ENTITY,
        CONF_LOCATION_ENTITY: LOCATION_ENTITY,
        CONF_WALL_CONNECTOR_ENTITY: WALL_CONNECTOR_ENTITY,
        CONF_LIVENESS_ENTITY: LIVENESS_ENTITY,
        CONF_CHARGING_ENTITY: CHARGING_ENTITY,
        CONF_OVERRIDE_ENTITY: OVERRIDE_ENTITY,
        **DEFAULT_OPTIONS,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], base | {CONF_LEASE_MINUTES: 15, CONF_RENEW_BEFORE_MINUTES: 15}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_RENEW_BEFORE_MINUTES: "renew_before_too_long"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        base
        | {
            CONF_LEASE_MINUTES: 15.0,
            CONF_RENEW_BEFORE_MINUTES: 6.0,
            CONF_INCREASE_MARGIN: 3.0,
            CONF_CEILING_HOLD: 30.0,
            CONF_DRY_RUN: False,
            CONF_RESTORE_ENABLED_ONLY: True,
            CONF_COMMUNITY_PATTERN: True,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_LEASE_MINUTES] == 15
    assert isinstance(entry.options[CONF_LEASE_MINUTES], int)
    assert entry.options[CONF_RENEW_BEFORE_MINUTES] == 6
    # evcc's increase rules can be turned back on.
    assert entry.options[CONF_INCREASE_MARGIN] == 3
    assert entry.options[CONF_CEILING_HOLD] == 30
    assert entry.options[CONF_DRY_RUN] is False
    assert entry.options[CONF_RESTORE_ENABLED_ONLY] is True
    assert entry.options[CONF_COMMUNITY_PATTERN] is True
    assert entry.options[CONF_WALL_CONNECTOR_ENTITY] == WALL_CONNECTOR_ENTITY
    assert entry.options[CONF_LIVENESS_ENTITY] == LIVENESS_ENTITY
    assert entry.options[CONF_CHARGING_ENTITY] == CHARGING_ENTITY
    assert entry.options[CONF_OVERRIDE_ENTITY] == OVERRIDE_ENTITY
    # The charger state entity was cleared, so it is no longer used.
    assert CONF_CHARGER_STATE_ENTITY not in entry.options


async def test_location_accepts_only_device_trackers(
    hass: HomeAssistant, mock_rivian: FakeRivian, mock_setup_entry: AsyncMock
) -> None:
    """A person is home without the car, so only a device tracker is accepted."""
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    selector = result["data_schema"].schema[CONF_LOCATION_ENTITY]
    assert selector.config["domain"] == ["device_tracker"]

    options = {
        CONF_PLUGGED_ENTITY: PLUGGED_ENTITY,
        CONF_LOCATION_ENTITY: "person.owner",
        **DEFAULT_OPTIONS,
    }
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], options)

    # The vehicle override takes an input select or a select, nothing else.
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            options | {CONF_LOCATION_ENTITY: LOCATION_ENTITY, CONF_OVERRIDE_ENTITY: "sensor.x"},
        )
    # The controller's charging entity is a binary sensor: schedule control's own
    # charging switch must never confirm its own request.
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            options
            | {
                CONF_LOCATION_ENTITY: LOCATION_ENTITY,
                CONF_CHARGING_ENTITY: "switch.test_r1s_schedule_control_solar_charging",
            },
        )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        options | {CONF_LOCATION_ENTITY: LOCATION_ENTITY, CONF_LIVENESS_ENTITY: "sensor.planner"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_LIVENESS_ENTITY] == "sensor.planner"


async def test_second_flow_for_same_vehicle_aborts(
    hass: HomeAssistant, mock_rivian: FakeRivian, mock_setup_entry: AsyncMock
) -> None:
    """Two flows for one vehicle: the second aborts with a translated reason."""
    first = await _sign_in(hass)
    second = await _sign_in(hass)
    first = await hass.config_entries.flow.async_configure(
        first["flow_id"], {CONF_VEHICLE_ID: VEHICLE_ID}
    )
    assert first["step_id"] == "entities"
    second = await hass.config_entries.flow.async_configure(
        second["flow_id"], {CONF_VEHICLE_ID: VEHICLE_ID}
    )
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_in_progress"
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text())
    assert strings["config"]["abort"]["already_in_progress"]


def test_translations_match_strings() -> None:
    """The English translation is the source strings, and every key is translated."""
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text())
    english = json.loads((INTEGRATION_DIR / "translations" / "en.json").read_text())
    assert english == strings
    for key in ("already_configured", "already_in_progress", "no_vehicles", "wrong_account"):
        assert key in strings["config"]["abort"]
    options = strings["options"]["step"]["init"]
    for key in (CONF_LIVENESS_ENTITY, CONF_CHARGING_ENTITY, CONF_OVERRIDE_ENTITY):
        assert key in options["data"]
        assert key in options["data_description"]
        assert key in strings["config"]["step"]["entities"]["data"]
        assert key in strings["config"]["step"]["entities"]["data_description"]
    for key in (
        CONF_INCREASE_MARGIN,
        CONF_CEILING_HOLD,
        CONF_RESTORE_ENABLED_ONLY,
        CONF_COMMUNITY_PATTERN,
    ):
        assert key in options["data"]
        assert key in options["data_description"]
    assert set(strings["entity"]["sensor"]["charger_status"]["state"]) == {"a", "b", "c"}
    assert "handed_back" in strings["entity"]["sensor"]["control_state"]["state"]
    assert "force_restore" in strings["services"]
    assert "restored" in options["data_description"][CONF_DRY_RUN]
    assert "Off by default" in options["data_description"][CONF_COMMUNITY_PATTERN]
    assert "archived" in options["data_description"][CONF_RESTORE_ENABLED_ONLY]


def test_installed_client_has_the_private_login_attributes() -> None:
    """The login state the config flow reads from the client still exists.

    The client exposes it only through private attributes (the upstream Rivian
    integration reads them the same way), so a client release that renames them
    must fail here rather than in a sign-in.
    """
    client = RealRivian(
        access_token="access", refresh_token="refresh", user_session_token="session"
    )
    assert client._otp_needed is False  # noqa: SLF001
    assert client._access_token == "access"  # noqa: SLF001
    assert client._refresh_token == "refresh"  # noqa: SLF001
    assert client._user_session_token == "session"  # noqa: SLF001
    # Signing in and validating a code set them.
    for method, names in (
        (RealRivian.authenticate, ("_otp_needed", "_access_token", "_refresh_token")),
        (RealRivian.validate_otp, ("_access_token", "_refresh_token", "_user_session_token")),
    ):
        source = inspect.getsource(method)
        for name in names:
            assert f"self.{name} =" in source, (method.__name__, name)
