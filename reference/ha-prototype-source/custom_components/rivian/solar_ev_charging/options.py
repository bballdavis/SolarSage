"""Options schema for solar-surplus EV charging."""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

import voluptuous as vol

from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_DISCHARGE_LIMIT_W,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_BATTERY_TARGET_SOC,
    CONF_BLOCKERS,
    CONF_COMMAND_INTERVAL_SECONDS,
    CONF_DRY_RUN,
    CONF_FEEDBACK_TIMEOUT_SECONDS,
    CONF_FORECAST_CONFIDENCE,
    CONF_FORECAST_MODE,
    CONF_FORECAST_NEXT_HOUR,
    CONF_FORECAST_REMAINING,
    CONF_FORECAST_SOURCE_MAX_AGE_HOURS,
    CONF_FORECAST_SOURCE_UPDATED,
    CONF_FORECAST_VALUE_FIELD,
    CONF_GRID_IMPORT_LIMIT_W,
    CONF_GRID_POWER,
    CONF_HARD_BATTERY_DISCHARGE_W,
    CONF_HARD_GRID_IMPORT_W,
    CONF_HOUSEHOLD_BUDGET,
    CONF_MAX_SOLAR_AMPS,
    CONF_MAX_TRANSIENT_DEFICIT_WH,
    CONF_MIN_OFF_SECONDS,
    CONF_MIN_SESSION_MINUTES,
    CONF_PV_POWER,
    CONF_RESERVE_ENTRY_MARGIN,
    CONF_RESERVE_SOC,
    CONF_RIVIAN_CONNECTED,
    CONF_RIVIAN_CONTROL_CONFIRMED,
    CONF_RIVIAN_HOME,
    CONF_RIVIAN_LIMIT,
    CONF_RIVIAN_SOC,
    CONF_RIVIAN_SWITCH,
    CONF_SAFETY_MARGIN_W,
    CONF_SMOOTHING_SECONDS,
    CONF_SOLAR_ENABLED,
    CONF_START_HOLD_SECONDS,
    CONF_STOP_HOLD_SECONDS,
    CONF_TESLA_AMPS,
    CONF_TESLA_CONNECTED,
    CONF_TESLA_HOME,
    CONF_TESLA_LIMIT,
    CONF_TESLA_SOC,
    CONF_TESLA_SWITCH,
    CONF_VEHICLE_MODE,
    CONF_WALL_CONNECTED,
    CONF_WALL_POWER,
    CONF_WALL_VOLTAGE,
    DEFAULT_BATTERY_DISCHARGE_LIMIT_W,
    DEFAULT_DRY_RUN,
    DEFAULT_FORECAST_SOURCE_MAX_AGE_HOURS,
    DEFAULT_GRID_IMPORT_LIMIT_W,
    DEFAULT_MAX_SOLAR_AMPS,
    DEFAULT_RESERVE_ENTRY_MARGIN,
    DEFAULT_RESERVE_SOC,
    DEFAULT_SAFETY_MARGIN_W,
    DEFAULT_START_HOLD_SECONDS,
    DEFAULT_STOP_HOLD_SECONDS,
    FORECAST_VALUE_FIELDS,
    VEHICLE_MODE_BOTH,
    VEHICLE_RIVIAN,
    VEHICLE_TESLA,
)


def _entity(
    domain: str | list[str], *, multiple: bool = False
) -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domain, multiple=multiple)
    )


def _number(
    minimum: float, maximum: float, *, step: float = 1, unit: str | None = None
) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            mode=selector.NumberSelectorMode.BOX,
            **({"unit_of_measurement": unit} if unit else {}),
        )
    )


def solar_option_fields(defaults: Mapping[str, Any]) -> dict[vol.Marker, Any]:
    """Return solar option fields with current values as defaults."""

    def current(key: str, fallback: Any) -> Any:
        return defaults.get(key, fallback)

    return {
        vol.Optional(
            CONF_SOLAR_ENABLED, default=current(CONF_SOLAR_ENABLED, False)
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_VEHICLE_MODE,
            default=current(CONF_VEHICLE_MODE, VEHICLE_MODE_BOTH),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[VEHICLE_RIVIAN, VEHICLE_TESLA, VEHICLE_MODE_BOTH],
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key="solar_vehicle_mode",
            )
        ),
        vol.Optional(CONF_GRID_POWER): _entity("sensor"),
        vol.Optional(CONF_PV_POWER): _entity("sensor"),
        vol.Optional(CONF_BATTERY_POWER): _entity("sensor"),
        vol.Optional(CONF_BATTERY_SOC): _entity("sensor"),
        vol.Optional(CONF_WALL_CONNECTED): _entity("binary_sensor"),
        vol.Optional(CONF_WALL_POWER): _entity("sensor"),
        vol.Optional(CONF_WALL_VOLTAGE): _entity("sensor"),
        vol.Optional(CONF_TESLA_CONNECTED): _entity("binary_sensor"),
        vol.Optional(CONF_TESLA_HOME): _entity(["binary_sensor", "device_tracker"]),
        vol.Optional(CONF_TESLA_SOC): _entity("sensor"),
        vol.Optional(CONF_TESLA_LIMIT): _entity(["number", "sensor"]),
        vol.Optional(CONF_TESLA_AMPS): _entity("number"),
        vol.Optional(CONF_TESLA_SWITCH): _entity("switch"),
        vol.Optional(CONF_RIVIAN_CONNECTED): _entity("binary_sensor"),
        vol.Optional(CONF_RIVIAN_HOME): _entity(["binary_sensor", "device_tracker"]),
        vol.Optional(CONF_RIVIAN_SWITCH): _entity("switch"),
        vol.Optional(
            CONF_RIVIAN_CONTROL_CONFIRMED,
            default=current(CONF_RIVIAN_CONTROL_CONFIRMED, False),
        ): selector.BooleanSelector(),
        vol.Optional(CONF_RIVIAN_SOC): _entity("sensor"),
        vol.Optional(CONF_RIVIAN_LIMIT): _entity(["number", "sensor"]),
        vol.Optional(
            CONF_BLOCKERS,
            default=current(
                CONF_BLOCKERS,
                [],
            ),
        ): _entity("input_boolean", multiple=True),
        vol.Optional(
            CONF_DRY_RUN, default=current(CONF_DRY_RUN, DEFAULT_DRY_RUN)
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_RESERVE_SOC, default=current(CONF_RESERVE_SOC, DEFAULT_RESERVE_SOC)
        ): _number(0, 100, unit="%"),
        vol.Optional(
            CONF_RESERVE_ENTRY_MARGIN,
            default=current(CONF_RESERVE_ENTRY_MARGIN, DEFAULT_RESERVE_ENTRY_MARGIN),
        ): _number(0, 25, unit="%"),
        vol.Optional(
            CONF_SAFETY_MARGIN_W,
            default=current(CONF_SAFETY_MARGIN_W, DEFAULT_SAFETY_MARGIN_W),
        ): _number(0, 5000, step=50, unit="W"),
        vol.Optional(
            CONF_START_HOLD_SECONDS,
            default=current(CONF_START_HOLD_SECONDS, DEFAULT_START_HOLD_SECONDS),
        ): _number(0, 900, unit="s"),
        vol.Optional(
            CONF_STOP_HOLD_SECONDS,
            default=current(CONF_STOP_HOLD_SECONDS, DEFAULT_STOP_HOLD_SECONDS),
        ): _number(0, 300, unit="s"),
        vol.Optional(
            CONF_BATTERY_DISCHARGE_LIMIT_W,
            default=current(
                CONF_BATTERY_DISCHARGE_LIMIT_W,
                DEFAULT_BATTERY_DISCHARGE_LIMIT_W,
            ),
        ): _number(0, 5000, step=50, unit="W"),
        vol.Optional(
            CONF_GRID_IMPORT_LIMIT_W,
            default=current(CONF_GRID_IMPORT_LIMIT_W, DEFAULT_GRID_IMPORT_LIMIT_W),
        ): _number(0, 5000, step=50, unit="W"),
        vol.Optional(
            CONF_MAX_SOLAR_AMPS,
            default=current(CONF_MAX_SOLAR_AMPS, DEFAULT_MAX_SOLAR_AMPS),
        ): _number(8, 48, unit="A"),
        vol.Optional(
            CONF_SMOOTHING_SECONDS, default=current(CONF_SMOOTHING_SECONDS, 60)
        ): _number(0, 300, unit="s"),
        vol.Optional(
            CONF_MIN_OFF_SECONDS, default=current(CONF_MIN_OFF_SECONDS, 180)
        ): _number(30, 1800, unit="s"),
        vol.Optional(
            CONF_MAX_TRANSIENT_DEFICIT_WH,
            default=current(CONF_MAX_TRANSIENT_DEFICIT_WH, 10),
        ): _number(0, 100, unit="Wh"),
        vol.Optional(
            CONF_HARD_GRID_IMPORT_W, default=current(CONF_HARD_GRID_IMPORT_W, 1500)
        ): _number(0, 10000, step=50, unit="W"),
        vol.Optional(
            CONF_HARD_BATTERY_DISCHARGE_W,
            default=current(CONF_HARD_BATTERY_DISCHARGE_W, 1000),
        ): _number(0, 10000, step=50, unit="W"),
        vol.Optional(
            CONF_FEEDBACK_TIMEOUT_SECONDS,
            default=current(CONF_FEEDBACK_TIMEOUT_SECONDS, 90),
        ): _number(30, 300, unit="s"),
        vol.Optional(
            CONF_COMMAND_INTERVAL_SECONDS,
            default=current(CONF_COMMAND_INTERVAL_SECONDS, 60),
        ): _number(30, 300, unit="s"),
        vol.Optional(
            CONF_FORECAST_MODE, default=current(CONF_FORECAST_MODE, "off")
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["off", "advisory", "protect"],
                translation_key="forecast_mode",
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_FORECAST_REMAINING): _entity("sensor"),
        vol.Optional(CONF_FORECAST_NEXT_HOUR): _entity("sensor"),
        vol.Optional(
            CONF_FORECAST_VALUE_FIELD,
            default=current(CONF_FORECAST_VALUE_FIELD, "state"),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(FORECAST_VALUE_FIELDS),
                translation_key="forecast_value_field",
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_FORECAST_SOURCE_UPDATED): _entity("sensor"),
        vol.Optional(
            CONF_FORECAST_SOURCE_MAX_AGE_HOURS,
            default=current(
                CONF_FORECAST_SOURCE_MAX_AGE_HOURS,
                DEFAULT_FORECAST_SOURCE_MAX_AGE_HOURS,
            ),
        ): _number(0.25, 48, step=0.25, unit="h"),
        vol.Optional(
            CONF_FORECAST_CONFIDENCE, default=current(CONF_FORECAST_CONFIDENCE, 0.75)
        ): _number(0.1, 1.0, step=0.05),
        vol.Optional(
            CONF_BATTERY_CAPACITY, default=current(CONF_BATTERY_CAPACITY, 0)
        ): _number(0, 500, step=0.1, unit="kWh"),
        vol.Optional(
            CONF_BATTERY_TARGET_SOC, default=current(CONF_BATTERY_TARGET_SOC, 80)
        ): _number(0, 100, unit="%"),
        vol.Optional(
            CONF_HOUSEHOLD_BUDGET, default=current(CONF_HOUSEHOLD_BUDGET, 5)
        ): _number(0, 200, step=0.1, unit="kWh"),
        vol.Optional(
            CONF_MIN_SESSION_MINUTES, default=current(CONF_MIN_SESSION_MINUTES, 15)
        ): _number(5, 60, unit="min"),
    }


OPTION_GROUPS = {
    "solar_setup": {
        CONF_SOLAR_ENABLED,
        CONF_VEHICLE_MODE,
        CONF_DRY_RUN,
        CONF_GRID_POWER,
        CONF_PV_POWER,
        CONF_BATTERY_POWER,
        CONF_BATTERY_SOC,
        CONF_WALL_CONNECTED,
        CONF_WALL_POWER,
        CONF_WALL_VOLTAGE,
    },
    "solar_vehicles": {
        CONF_TESLA_CONNECTED,
        CONF_TESLA_HOME,
        CONF_TESLA_SOC,
        CONF_TESLA_LIMIT,
        CONF_TESLA_AMPS,
        CONF_TESLA_SWITCH,
        CONF_RIVIAN_CONNECTED,
        CONF_RIVIAN_HOME,
        CONF_RIVIAN_SOC,
        CONF_RIVIAN_LIMIT,
        CONF_RIVIAN_SWITCH,
        CONF_RIVIAN_CONTROL_CONFIRMED,
    },
    "solar_forecast": {
        CONF_FORECAST_MODE,
        CONF_FORECAST_REMAINING,
        CONF_FORECAST_NEXT_HOUR,
        CONF_FORECAST_VALUE_FIELD,
        CONF_FORECAST_SOURCE_UPDATED,
        CONF_FORECAST_SOURCE_MAX_AGE_HOURS,
        CONF_FORECAST_CONFIDENCE,
        CONF_BATTERY_CAPACITY,
        CONF_BATTERY_TARGET_SOC,
        CONF_HOUSEHOLD_BUDGET,
        CONF_MIN_SESSION_MINUTES,
    },
    "solar_tuning": {
        CONF_RESERVE_SOC,
        CONF_RESERVE_ENTRY_MARGIN,
        CONF_SAFETY_MARGIN_W,
        CONF_START_HOLD_SECONDS,
        CONF_STOP_HOLD_SECONDS,
        CONF_BATTERY_DISCHARGE_LIMIT_W,
        CONF_GRID_IMPORT_LIMIT_W,
        CONF_MAX_SOLAR_AMPS,
        CONF_BLOCKERS,
        CONF_SMOOTHING_SECONDS,
        CONF_MIN_OFF_SECONDS,
        CONF_MAX_TRANSIENT_DEFICIT_WH,
        CONF_HARD_GRID_IMPORT_W,
        CONF_HARD_BATTERY_DISCHARGE_W,
        CONF_FEEDBACK_TIMEOUT_SECONDS,
        CONF_COMMAND_INTERVAL_SECONDS,
    },
}


def solar_group_schema(group: str, defaults: Mapping[str, Any]) -> vol.Schema:
    """Group native HA configuration without changing stored option keys."""
    return vol.Schema(
        {
            key: value
            for key, value in solar_option_fields(defaults).items()
            if key.schema in OPTION_GROUPS[group]
        }
    )


def validate_solar_options(options: Mapping[str, Any]) -> str | None:
    """Validate cross-field limits before they can reach the controller."""
    from .const import (
        SOLAR_BASE_REQUIRED_OPTIONS,
        SOLAR_RIVIAN_REQUIRED_OPTIONS,
        SOLAR_TESLA_REQUIRED_OPTIONS,
    )

    if options.get(CONF_SOLAR_ENABLED):
        required = list(SOLAR_BASE_REQUIRED_OPTIONS)
        mode = options.get(CONF_VEHICLE_MODE, VEHICLE_MODE_BOTH)
        if mode in (VEHICLE_TESLA, VEHICLE_MODE_BOTH):
            required.extend(SOLAR_TESLA_REQUIRED_OPTIONS)
        if mode in (VEHICLE_RIVIAN, VEHICLE_MODE_BOTH):
            required.extend(SOLAR_RIVIAN_REQUIRED_OPTIONS)
        if any(not options.get(key) for key in required):
            return "solar_required"
    if (
        float(options.get(CONF_RESERVE_SOC, 25))
        + float(options.get(CONF_RESERVE_ENTRY_MARGIN, 5))
        > 100
    ):
        return "solar_reserve_range"
    if float(options.get(CONF_HARD_GRID_IMPORT_W, 1500)) < float(
        options.get(CONF_GRID_IMPORT_LIMIT_W, 500)
    ) or float(options.get(CONF_HARD_BATTERY_DISCHARGE_W, 1000)) < float(
        options.get(CONF_BATTERY_DISCHARGE_LIMIT_W, 250)
    ):
        return "solar_hard_limits"
    if options.get(CONF_FORECAST_MODE) == "protect" and (
        not options.get(CONF_FORECAST_REMAINING)
        or not options.get(CONF_FORECAST_NEXT_HOUR)
        or not options.get(CONF_FORECAST_SOURCE_UPDATED)
        or options.get(CONF_FORECAST_VALUE_FIELD, "state") not in FORECAST_VALUE_FIELDS
        or not _positive_finite(
            options.get(
                CONF_FORECAST_SOURCE_MAX_AGE_HOURS,
                DEFAULT_FORECAST_SOURCE_MAX_AGE_HOURS,
            )
        )
        or float(options.get(CONF_BATTERY_CAPACITY, 0)) <= 0
        or float(options.get(CONF_BATTERY_TARGET_SOC, 80))
        < float(options.get(CONF_RESERVE_SOC, 25))
    ):
        return "solar_forecast_required"
    return None


def _positive_finite(value: Any) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric > 0
