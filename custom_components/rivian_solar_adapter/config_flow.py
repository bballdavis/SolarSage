"""Config flow for the Rivian Solar Charge Adapter.

The password is used once to sign in and is never stored; only the session
tokens are kept in the entry.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import probatio as vol
from rivian import Rivian
from rivian.exceptions import (
    RivianApiException,
    RivianInvalidCredentials,
    RivianInvalidOTP,
    RivianTemporarilyLockedError,
    RivianUnauthenticated,
)

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CHARGER_STATE_ENTITY,
    CONF_CHARGING_ENTITY,
    CONF_DRY_RUN,
    CONF_EMAIL,
    CONF_LEASE_MINUTES,
    CONF_LIVENESS_ENTITY,
    CONF_LOCATION_ENTITY,
    CONF_OTP,
    CONF_OVERRIDE_ENTITY,
    CONF_PASSWORD,
    CONF_PLUGGED_ENTITY,
    CONF_REFRESH_TOKEN,
    CONF_RENEW_BEFORE_MINUTES,
    CONF_USER_SESSION_TOKEN,
    CONF_VEHICLE_ID,
    CONF_WALL_CONNECTOR_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
    ENTITY_OPTIONS,
    ENTRY_MINOR_VERSION,
    ENTRY_VERSION,
    OPTION_RANGES,
    REQUEST_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR})
OTP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OTP): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="one-time-code")
        )
    }
)

ENTITY_SELECTORS: dict[str, EntitySelector] = {
    CONF_PLUGGED_ENTITY: EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
    # The vehicle's own tracker: a person is home without the car.
    CONF_LOCATION_ENTITY: EntitySelector(EntitySelectorConfig(domain="device_tracker")),
    CONF_CHARGER_STATE_ENTITY: EntitySelector(EntitySelectorConfig(domain="sensor")),
    CONF_WALL_CONNECTOR_ENTITY: EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
    # Any entity that is unavailable or unknown while the controller is down,
    # such as the planner's heartbeat.
    CONF_LIVENESS_ENTITY: EntitySelector(EntitySelectorConfig()),
    # On while the controller wants this vehicle charging. A binary sensor only:
    # the adapter's own charging switch must never confirm its own request.
    CONF_CHARGING_ENTITY: EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
    CONF_OVERRIDE_ENTITY: EntitySelector(EntitySelectorConfig(domain=["input_select", "select"])),
}
REQUIRED_ENTITY_OPTIONS = (CONF_PLUGGED_ENTITY, CONF_LOCATION_ENTITY)


def _entity_schema() -> dict[vol.Marker, Any]:
    return {
        (vol.Required(key) if key in REQUIRED_ENTITY_OPTIONS else vol.Optional(key)): (
            ENTITY_SELECTORS[key]
        )
        for key in ENTITY_OPTIONS
    }


ENTITIES_SCHEMA = vol.Schema(_entity_schema())


def _options_schema() -> vol.Schema:
    schema = _entity_schema()
    for key, (minimum, maximum, unit) in OPTION_RANGES.items():
        schema[vol.Required(key, default=DEFAULT_OPTIONS[key])] = NumberSelector(
            NumberSelectorConfig(
                min=minimum,
                max=maximum,
                step=1,
                unit_of_measurement=unit,
                mode=NumberSelectorMode.BOX,
            )
        )
    schema[vol.Required(CONF_DRY_RUN, default=DEFAULT_OPTIONS[CONF_DRY_RUN])] = BooleanSelector()
    return vol.Schema(schema)


OPTIONS_SCHEMA = _options_schema()


def _clean_options(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the mapped entities that are set and store tuning as integers."""
    options: dict[str, Any] = {
        key: user_input[key] for key in ENTITY_OPTIONS if user_input.get(key)
    }
    for key in OPTION_RANGES:
        options[key] = int(user_input.get(key, DEFAULT_OPTIONS[key]))
    options[CONF_DRY_RUN] = bool(user_input.get(CONF_DRY_RUN, DEFAULT_OPTIONS[CONF_DRY_RUN]))
    return options


def _validate_options(options: Mapping[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if options[CONF_RENEW_BEFORE_MINUTES] >= options[CONF_LEASE_MINUTES]:
        errors[CONF_RENEW_BEFORE_MINUTES] = "renew_before_too_long"
    return errors


def _vehicle_label(vehicle: Mapping[str, Any]) -> str:
    details = vehicle.get("vehicle") or {}
    model = " ".join(str(part) for part in (details.get("modelYear"), details.get("model")) if part)
    name = vehicle.get("name") or model or "Rivian"
    return f"{name} ({model})" if model and model != name else name


class RivianSolarAdapterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in to Rivian, pick a vehicle and map the Home Assistant entities."""

    VERSION = ENTRY_VERSION
    MINOR_VERSION = ENTRY_MINOR_VERSION

    def __init__(self) -> None:
        self._client: Rivian | None = None
        self._email: str = ""
        self._tokens: dict[str, str] = {}
        self._vehicles: dict[str, Mapping[str, Any]] = {}
        self._vehicle_id: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RivianSolarAdapterOptionsFlow:
        """Return the options flow."""
        return RivianSolarAdapterOptionsFlow()

    # Sign in -------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the Rivian account credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            if not (error := await self._async_login(user_input[CONF_PASSWORD])):
                return await self._async_after_login()
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {CONF_EMAIL: self._email} if self._email else {}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after Rivian rejected the session."""
        self._email = entry_data[CONF_EMAIL]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (error := await self._async_login(user_input[CONF_PASSWORD])):
                return await self._async_after_login()
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"name": self._get_reauth_entry().title},
        )

    async def async_step_otp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the one-time code Rivian sent."""
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._client is not None
            try:
                await self._client.validate_otp(self._email, user_input[CONF_OTP].strip())
            except RivianInvalidOTP:
                errors["base"] = "invalid_otp"
            except RivianTemporarilyLockedError:
                errors["base"] = "locked"
            except RivianInvalidCredentials, RivianUnauthenticated:
                errors["base"] = "invalid_auth"
            except RivianApiException:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating the Rivian one-time code")
                errors["base"] = "unknown"
            else:
                return await self._async_after_login()
        return self.async_show_form(step_id="otp", data_schema=OTP_SCHEMA, errors=errors)

    async def _async_login(self, password: str) -> str | None:
        """Sign in; return an error key, or None on success or when a code is needed."""
        self._client = client = Rivian(
            session=async_get_clientsession(self.hass), request_timeout=REQUEST_TIMEOUT
        )
        try:
            await client.create_csrf_token()
            await client.authenticate(self._email, password)
        except RivianTemporarilyLockedError:
            return "locked"
        except RivianInvalidCredentials, RivianUnauthenticated:
            return "invalid_auth"
        except RivianApiException:
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error signing in to Rivian")
            return "unknown"
        return None

    async def _async_after_login(self) -> ConfigFlowResult:
        """Continue with a code request, or with the vehicles of a signed-in account."""
        client = self._client
        assert client is not None
        # The client exposes the login state only through these attributes; the
        # upstream Rivian integration reads them the same way.
        if client._otp_needed and not client._access_token:  # noqa: SLF001
            return await self.async_step_otp()
        self._tokens = {
            CONF_ACCESS_TOKEN: client._access_token,  # noqa: SLF001
            CONF_REFRESH_TOKEN: client._refresh_token,  # noqa: SLF001
            CONF_USER_SESSION_TOKEN: client._user_session_token,  # noqa: SLF001
        }
        try:
            self._vehicles = await self._async_fetch_vehicles(client)
        except RivianApiException:
            return self._async_show_login_error("cannot_connect")
        except Exception:
            _LOGGER.exception("Unexpected error reading the Rivian vehicles")
            return self._async_show_login_error("unknown")

        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            if entry.data[CONF_VEHICLE_ID] not in self._vehicles:
                return self.async_abort(reason="wrong_account")
            return self.async_update_reload_and_abort(
                entry, data_updates={CONF_EMAIL: self._email, **self._tokens}
            )
        if not self._vehicles:
            return self.async_abort(reason="no_vehicles")
        return await self.async_step_vehicle()

    @callback
    def _async_show_login_error(self, error: str) -> ConfigFlowResult:
        if self.source == SOURCE_REAUTH:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=REAUTH_SCHEMA,
                errors={"base": error},
                description_placeholders={"name": self._get_reauth_entry().title},
            )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, {CONF_EMAIL: self._email}),
            errors={"base": error},
        )

    @staticmethod
    async def _async_fetch_vehicles(client: Rivian) -> dict[str, Mapping[str, Any]]:
        response = await client.get_user_information()
        payload = await response.json()
        user = ((payload or {}).get("data") or {}).get("currentUser") or {}
        return {
            vehicle["id"]: vehicle
            for vehicle in user.get("vehicles") or []
            if isinstance(vehicle, Mapping) and vehicle.get("id")
        }

    # Vehicle and entities ------------------------------------------------

    async def async_step_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the vehicle this adapter controls."""
        if user_input is not None:
            self._vehicle_id = user_input[CONF_VEHICLE_ID]
            await self.async_set_unique_id(self._vehicle_id)
            self._abort_if_unique_id_configured()
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="vehicle",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_VEHICLE_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=vehicle_id, label=_vehicle_label(vehicle))
                                for vehicle_id, vehicle in self._vehicles.items()
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Map the Home Assistant entities that report plug, location and charging."""
        if user_input is not None:
            assert self._vehicle_id is not None
            vehicle = self._vehicles[self._vehicle_id]
            return self.async_create_entry(
                title=vehicle.get("name") or _vehicle_label(vehicle),
                data={CONF_EMAIL: self._email, CONF_VEHICLE_ID: self._vehicle_id, **self._tokens},
                options=_clean_options(user_input),
            )
        return self.async_show_form(step_id="entities", data_schema=ENTITIES_SCHEMA)


class RivianSolarAdapterOptionsFlow(OptionsFlowWithReload):
    """Change the mapped entities and the tuning; saving reloads the entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and validate the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options = _clean_options(user_input)
            if not (errors := _validate_options(options)):
                return self.async_create_entry(data=options)
            suggested: Mapping[str, Any] = user_input
        else:
            suggested = DEFAULT_OPTIONS | dict(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, suggested),
            errors=errors,
        )
