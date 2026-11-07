"""Config flow for the Solar EV planner.

The main entry maps the site in three steps (solar, grid and battery; the Wall Connector;
the forecast and the free hours), prefilled with the owner's entity IDs where they exist
(``core.entities``). Cars and extra price windows are subentries: one per car (kind, the
entities it is read and commanded through, its currents and pack size) and one per Schedule
helper painted with a price. Every mapping can be changed later (options, reconfigure).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
import probatio as vol

from .const import (
    CAR_DEFAULTS,
    CAR_KINDS,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_POWER,
    CONF_BATTERY_RESERVE_PCT,
    CONF_BATTERY_SOC,
    CONF_CAPACITY_KWH,
    CONF_CAR_ID,
    CONF_CHARGER_MAX_A,
    CONF_CHARGING_STATE,
    CONF_CONTROL_STATE,
    CONF_CURRENT,
    CONF_EXPORT_PRICE,
    CONF_FORECAST_POLLED,
    CONF_FORECAST_TODAY,
    CONF_FORECAST_TOMORROW,
    CONF_FREE_HOURS,
    CONF_GRID_POWER,
    CONF_KIND,
    CONF_LABEL,
    CONF_LIMIT,
    CONF_LOCATION,
    CONF_MAX_A,
    CONF_MIN_A,
    CONF_NAME,
    CONF_PLUGGED,
    CONF_PRICE,
    CONF_PV_POWER,
    CONF_SCHEDULE,
    CONF_SOC,
    CONF_STATUS,
    CONF_STEP_A,
    CONF_SWITCH,
    CONF_VOLTS,
    CONF_WALL_CONNECTED,
    CONF_WALL_CONTACTOR,
    CONF_WALL_CURRENT,
    CONF_WALL_POWER,
    DEFAULT_SITE_OPTIONS,
    DOMAIN,
    ENTRY_TITLE,
    KIND_RIVIAN,
    POWER_UNITS,
    SUBENTRY_CAR,
    SUBENTRY_PRICE,
    invert_key,
    unit_key,
)
from .core.entities import (
    CAR_ENTITIES,
    CAR_ENTITY_ALTERNATIVES,
    CAR_ENTITY_PREFERRED,
    SITE_ENTITIES,
)


def _entity(domain: str | list[str]) -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain=domain))


def _number(
    minimum: float, maximum: float, step: float = 1, unit: str | None = None
) -> NumberSelector:
    config = NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX)
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


UNIT_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=list(POWER_UNITS), mode=SelectSelectorMode.DROPDOWN, translation_key="power_unit"
    )
)

# Step "user": solar, grid and the home battery. Each power has a unit (the sensor's own
# unit wins when it has one) and a sign flip: the planner wants grid + import / - export,
# and battery + charging.
SITE_FIELDS: dict[str, tuple[bool, Any]] = {
    CONF_PV_POWER: (True, _entity("sensor")),
    unit_key(CONF_PV_POWER): (True, UNIT_SELECTOR),
    invert_key(CONF_PV_POWER): (True, BooleanSelector()),
    CONF_GRID_POWER: (True, _entity("sensor")),
    unit_key(CONF_GRID_POWER): (True, UNIT_SELECTOR),
    invert_key(CONF_GRID_POWER): (True, BooleanSelector()),
    CONF_BATTERY_POWER: (True, _entity("sensor")),
    unit_key(CONF_BATTERY_POWER): (True, UNIT_SELECTOR),
    invert_key(CONF_BATTERY_POWER): (True, BooleanSelector()),
    CONF_BATTERY_SOC: (True, _entity("sensor")),
    CONF_BATTERY_CAPACITY: (False, _entity("sensor")),
    CONF_BATTERY_CAPACITY_KWH: (True, _number(1, 500, 0.1, "kWh")),
    CONF_BATTERY_MAX_CHARGE_KW: (True, _number(0.5, 50, 0.1, "kW")),
    CONF_BATTERY_MAX_DISCHARGE_KW: (True, _number(0.5, 50, 0.1, "kW")),
    CONF_BATTERY_RESERVE_PCT: (True, _number(0, 50, 1, "%")),
}
# Step "charger": the Wall Connector (read only; it tells whether a vehicle is connected).
CHARGER_FIELDS: dict[str, tuple[bool, Any]] = {
    CONF_WALL_POWER: (True, _entity("sensor")),
    unit_key(CONF_WALL_POWER): (True, UNIT_SELECTOR),
    CONF_WALL_CURRENT: (False, _entity("sensor")),
    CONF_WALL_CONNECTED: (False, _entity("binary_sensor")),
    CONF_WALL_CONTACTOR: (False, _entity("binary_sensor")),
    CONF_CHARGER_MAX_A: (True, _number(6, 80, 1, "A")),
    CONF_VOLTS: (True, _number(100, 480, 1, "V")),
}
# Step "tariff": Solcast and the free hours (a Schedule helper; leave empty for a flat or
# time-of-use plan). Prices are entities of the planner; extra price windows are subentries.
TARIFF_FIELDS: dict[str, tuple[bool, Any]] = {
    CONF_FORECAST_TODAY: (True, _entity("sensor")),
    CONF_FORECAST_TOMORROW: (True, _entity("sensor")),
    CONF_FORECAST_POLLED: (False, _entity("sensor")),
    CONF_FREE_HOURS: (False, _entity("schedule")),
}
STEPS = {"user": SITE_FIELDS, "charger": CHARGER_FIELDS, "tariff": TARIFF_FIELDS}


def _schema(fields: Mapping[str, tuple[bool, Any]]) -> vol.Schema:
    return vol.Schema(
        {
            (vol.Required(key) if required else vol.Optional(key)): selector
            for key, (required, selector) in fields.items()
        }
    )


def _existing(hass: HomeAssistant, candidates: Mapping[str, str]) -> dict[str, str]:
    """The suggestions whose entities exist."""
    return {key: eid for key, eid in candidates.items() if hass.states.get(eid) is not None}


def site_suggestions(hass: HomeAssistant) -> dict[str, Any]:
    """Defaults plus the owner's entity IDs that exist in this Home Assistant."""
    return DEFAULT_SITE_OPTIONS | _existing(hass, SITE_ENTITIES)


def _clean(fields: Mapping[str, tuple[bool, Any]], user_input: Mapping[str, Any]) -> dict[str, Any]:
    """The step's answers; an emptied optional entity is dropped."""
    out: dict[str, Any] = {}
    for key in fields:
        if key not in user_input or user_input[key] in (None, ""):
            continue
        out[key] = user_input[key]
    return out


def _site_errors(options: Mapping[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if options.get(CONF_FORECAST_TODAY) and options.get(CONF_FORECAST_TODAY) == options.get(
        CONF_FORECAST_TOMORROW
    ):
        errors[CONF_FORECAST_TOMORROW] = "same_forecast"
    return errors


class SolarEvPlannerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Map the site; cars and price windows are added as subentries."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SolarEvPlannerOptionsFlow:
        return SolarEvPlannerOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_CAR: CarSubentryFlow, SUBENTRY_PRICE: PriceWindowSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Solar, grid and the home battery."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self._async_step("user", "charger", user_input)

    async def async_step_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The Wall Connector."""
        return await self._async_step("charger", "tariff", user_input)

    async def async_step_tariff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Solcast and the free hours."""
        return await self._async_step("tariff", None, user_input)

    async def _async_step(
        self, step_id: str, next_step: str | None, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        fields = STEPS[step_id]
        errors: dict[str, str] = {}
        if user_input is not None:
            self._options.update(_clean(fields, user_input))
            if not (errors := _site_errors(self._options)):
                if next_step is None:
                    return self.async_create_entry(
                        title=ENTRY_TITLE, data={}, options=DEFAULT_SITE_OPTIONS | self._options
                    )
                return await getattr(self, f"async_step_{next_step}")()
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _schema(fields), user_input or site_suggestions(self.hass)
            ),
            errors=errors,
        )


class SolarEvPlannerOptionsFlow(OptionsFlow):
    """Change the site mapping; saving reloads the planner (its memory is kept)."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_step("init", "user", "charger", user_input)

    async def async_step_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step("charger", "charger", "tariff", user_input)

    async def async_step_tariff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_step("tariff", "tariff", None, user_input)

    async def _async_step(
        self,
        step_id: str,
        fields_of: str,
        next_step: str | None,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        fields = STEPS[fields_of]
        errors: dict[str, str] = {}
        if not self._options:
            # Start from the stored options without this flow's optional entities, so an
            # emptied field removes the mapping.
            self._options = dict(self.config_entry.options)
        if user_input is not None:
            for key in fields:
                self._options.pop(key, None)
            self._options.update(_clean(fields, user_input))
            if not (errors := _site_errors(self._options)):
                if next_step is None:
                    return self.async_create_entry(data=DEFAULT_SITE_OPTIONS | self._options)
                return await getattr(self, f"async_step_{next_step}")()
        suggested = user_input or (DEFAULT_SITE_OPTIONS | dict(self.config_entry.options))
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(_schema(fields), suggested),
            errors=errors,
        )


# --------------------------------------------------------------------------- cars


def car_fields(kind: str) -> dict[str, tuple[bool, Any]]:
    """What the planner reads and commands for a car of this kind."""
    fields: dict[str, tuple[bool, Any]] = {
        CONF_NAME: (True, TextSelector()),
        # Tesla Fleet's charge switch and charge current, or Rivian schedule control's solar
        # charging switch and solar current
        CONF_SWITCH: (True, _entity(["switch", "input_boolean"])),
        CONF_CURRENT: (True, _entity(["number", "input_number"])),
        CONF_SOC: (True, _entity("sensor")),
        CONF_LIMIT: (True, _entity(["sensor", "number", "input_number"])),
        # on while the car's cable is connected (this car's own sensor, any charger)
        CONF_PLUGGED: (True, _entity("binary_sensor")),
        # the car's own tracker (home), or a binary sensor that is on at home
        CONF_LOCATION: (False, _entity(["device_tracker", "binary_sensor"])),
    }
    if kind == KIND_RIVIAN:
        fields[CONF_CONTROL_STATE] = (False, _entity("sensor"))
    else:
        # Tesla Fleet's charging sensor: no start is sent while it reads complete and the like
        fields[CONF_CHARGING_STATE] = (False, _entity("sensor"))
        # Tesla Fleet's status (on while online): a send to a car that is not online is
        # counted with a wake by the spending guard
        fields[CONF_STATUS] = (False, _entity("binary_sensor"))
    fields |= {
        CONF_MIN_A: (True, _number(1, 80, 1, "A")),
        CONF_MAX_A: (True, _number(1, 80, 1, "A")),
        CONF_STEP_A: (True, _number(1, 10, 1, "A")),
        CONF_CAPACITY_KWH: (True, _number(5, 300, 1, "kWh")),
    }
    return fields


def car_suggestions(hass: HomeAssistant, kind: str) -> dict[str, Any]:
    """The kind's defaults and the owner's entity IDs for it that exist."""
    found = _existing(hass, CAR_ENTITIES.get(kind, {}))
    for key, others in CAR_ENTITY_ALTERNATIVES.get(kind, {}).items():
        existing = [eid for eid in others if hass.states.get(eid) is not None]
        if key not in found and existing:
            found[key] = existing[0]
    found |= _existing(hass, CAR_ENTITY_PREFERRED.get(kind, {}))
    return CAR_DEFAULTS[kind] | found


def _car_errors(data: Mapping[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if int(data[CONF_MIN_A]) > int(data[CONF_MAX_A]):
        errors[CONF_MAX_A] = "max_below_min"
    if data[CONF_SWITCH].startswith(f"switch.{DOMAIN}") or data[CONF_CURRENT].startswith(
        f"number.{DOMAIN}"
    ):
        errors["base"] = "own_entity"
    return errors


def _car_data(kind: str, user_input: Mapping[str, Any]) -> dict[str, Any]:
    data = _clean(car_fields(kind), user_input)
    data[CONF_NAME] = str(data.get(CONF_NAME) or CAR_DEFAULTS[kind][CONF_NAME]).strip()
    for key in (CONF_MIN_A, CONF_MAX_A, CONF_STEP_A):
        data[key] = int(data[key])
    data[CONF_CAPACITY_KWH] = float(data[CONF_CAPACITY_KWH])
    return data


class CarSubentryFlow(ConfigSubentryFlow):
    """Add or change a car."""

    def __init__(self) -> None:
        self._kind = KIND_RIVIAN

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose the kind of car."""
        if user_input is not None:
            self._kind = user_input[CONF_KIND]
            return await self.async_step_car()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_KIND, default=KIND_RIVIAN): SelectSelector(
                        SelectSelectorConfig(
                            options=list(CAR_KINDS),
                            mode=SelectSelectorMode.LIST,
                            translation_key="car_kind",
                        )
                    )
                }
            ),
        )

    async def async_step_car(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Map the car's entities."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _car_data(self._kind, user_input)
            if not (errors := _car_errors(data)):
                car_id = self._new_car_id()
                return self.async_create_entry(
                    title=data[CONF_NAME],
                    data={CONF_CAR_ID: car_id, CONF_KIND: self._kind} | data,
                    unique_id=car_id,
                )
        return self.async_show_form(
            step_id="car",
            data_schema=self.add_suggested_values_to_schema(
                _schema(car_fields(self._kind)),
                user_input or car_suggestions(self.hass, self._kind),
            ),
            errors=errors,
            description_placeholders={"kind": self._kind},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change the car's entities and numbers (its kind and id stay)."""
        subentry = self._get_reconfigure_subentry()
        kind = subentry.data[CONF_KIND]
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _car_data(kind, user_input)
            if not (errors := _car_errors(data)):
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    title=data[CONF_NAME],
                    data={CONF_CAR_ID: subentry.data[CONF_CAR_ID], CONF_KIND: kind} | data,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _schema(car_fields(kind)), user_input or dict(subentry.data)
            ),
            errors=errors,
            description_placeholders={"kind": kind},
        )

    def _new_car_id(self) -> str:
        """The kind ("rivian", "tesla"), numbered from the second car of a kind on. The
        Rivian schedule control's vehicle override treats "rivian" as its own car."""
        taken = {sub.data.get(CONF_CAR_ID) for sub in self._get_entry().subentries.values()}
        car_id, n = self._kind, 2
        while car_id in taken:
            car_id = f"{self._kind}_{n}"
            n += 1
        return car_id


# --------------------------------------------------------------------------- price windows

PRICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SCHEDULE): _entity("schedule"),
        vol.Required(CONF_LABEL): TextSelector(),
        vol.Required(CONF_PRICE): _number(0, 5, 0.01),
        vol.Optional(CONF_EXPORT_PRICE): _number(0, 5, 0.01),
    }
)


def _price_data(user_input: Mapping[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        CONF_SCHEDULE: user_input[CONF_SCHEDULE],
        CONF_LABEL: str(user_input[CONF_LABEL]).strip(),
        CONF_PRICE: float(user_input[CONF_PRICE]),
    }
    if user_input.get(CONF_EXPORT_PRICE) is not None:
        data[CONF_EXPORT_PRICE] = float(user_input[CONF_EXPORT_PRICE])
    return data


class PriceWindowSubentryFlow(ConfigSubentryFlow):
    """A Schedule helper painted with one import price (for example a peak or an off-peak
    level); the free hours are the main entry's own schedule."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            data = _price_data(user_input)
            return self.async_create_entry(
                title=data[CONF_LABEL], data=data, unique_id=data[CONF_SCHEDULE]
            )
        return self.async_show_form(step_id="user", data_schema=PRICE_SCHEMA)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            data = _price_data(user_input)
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=data[CONF_LABEL],
                data=data,
                unique_id=data[CONF_SCHEDULE],
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(PRICE_SCHEMA, dict(subentry.data)),
        )
