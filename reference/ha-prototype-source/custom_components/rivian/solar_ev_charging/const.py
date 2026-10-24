"""Constants for Solar EV Charging."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "solar_ev_charging"
CONF_SOLAR_ENABLED = "solar_ev_charging_enabled"
CONF_VEHICLE_MODE = "solar_vehicle_mode"

CONF_GRID_POWER = "grid_power_entity"
CONF_BATTERY_POWER = "battery_power_entity"
CONF_BATTERY_SOC = "battery_soc_entity"
CONF_PV_POWER = "pv_power_entity"
CONF_WALL_CONNECTED = "wall_connected_entity"
CONF_WALL_POWER = "wall_power_entity"
CONF_WALL_VOLTAGE = "wall_voltage_entity"
CONF_TESLA_CONNECTED = "tesla_connected_entity"
CONF_TESLA_CHARGING = "tesla_charging_entity"
CONF_TESLA_SOC = "tesla_soc_entity"
CONF_TESLA_LIMIT = "tesla_limit_entity"
CONF_TESLA_AMPS = "tesla_amps_entity"
CONF_TESLA_SWITCH = "tesla_switch_entity"
CONF_RIVIAN_CONNECTED = "rivian_connected_entity"
CONF_RIVIAN_CHARGING = "rivian_charging_entity"
CONF_RIVIAN_SOC = "rivian_soc_entity"
CONF_RIVIAN_LIMIT = "rivian_limit_entity"
CONF_NORMAL_SCHEDULE = "normal_schedule_entity"
CONF_BLOCKERS = "blocker_entities"

CONF_DRY_RUN = "dry_run"
CONF_RESERVE_SOC = "reserve_soc"
CONF_RESERVE_ENTRY_MARGIN = "reserve_entry_margin"
CONF_SAFETY_MARGIN_W = "safety_margin_w"
CONF_START_HOLD_SECONDS = "start_hold_seconds"
CONF_STOP_HOLD_SECONDS = "stop_hold_seconds"
CONF_BATTERY_DISCHARGE_LIMIT_W = "battery_discharge_limit_w"
CONF_GRID_IMPORT_LIMIT_W = "grid_import_limit_w"
CONF_NORMAL_TESLA_AMPS = "normal_tesla_amps"
CONF_MAX_SOLAR_AMPS = "max_solar_amps"
CONF_RIVIAN_SWITCH = "rivian_switch_entity"
CONF_RIVIAN_CONTROL_CONFIRMED = "rivian_control_confirmed"
CONF_RIVIAN_HOME = "rivian_home_entity"
CONF_TESLA_HOME = "tesla_home_entity"
CONF_SMOOTHING_SECONDS = "smoothing_seconds"
CONF_MIN_OFF_SECONDS = "min_off_seconds"
CONF_MAX_TRANSIENT_DEFICIT_WH = "max_transient_deficit_wh"
CONF_HARD_GRID_IMPORT_W = "hard_grid_import_w"
CONF_HARD_BATTERY_DISCHARGE_W = "hard_battery_discharge_w"
CONF_FEEDBACK_TIMEOUT_SECONDS = "feedback_timeout_seconds"
CONF_COMMAND_INTERVAL_SECONDS = "command_interval_seconds"
CONF_FORECAST_MODE = "forecast_mode"
CONF_FORECAST_REMAINING = "forecast_remaining_entity"
CONF_FORECAST_NEXT_HOUR = "forecast_next_hour_entity"
CONF_FORECAST_VALUE_FIELD = "forecast_value_field"
CONF_FORECAST_SOURCE_UPDATED = "forecast_source_updated_entity"
CONF_FORECAST_SOURCE_MAX_AGE_HOURS = "forecast_source_max_age_hours"
CONF_FORECAST_CONFIDENCE = "forecast_confidence"
CONF_BATTERY_CAPACITY = "battery_capacity_kwh"
CONF_BATTERY_TARGET_SOC = "battery_target_soc"
CONF_HOUSEHOLD_BUDGET = "household_budget_kwh"
CONF_MIN_SESSION_MINUTES = "minimum_session_minutes"

DEFAULT_DRY_RUN = True
DEFAULT_RESERVE_SOC = 25.0
DEFAULT_RESERVE_ENTRY_MARGIN = 5.0
DEFAULT_SAFETY_MARGIN_W = 500.0
DEFAULT_START_HOLD_SECONDS = 120
DEFAULT_STOP_HOLD_SECONDS = 30
DEFAULT_BATTERY_DISCHARGE_LIMIT_W = 250.0
DEFAULT_GRID_IMPORT_LIMIT_W = 500.0
DEFAULT_NORMAL_TESLA_AMPS = 48
DEFAULT_MAX_SOLAR_AMPS = 48
DEFAULT_FORECAST_SOURCE_MAX_AGE_HOURS = 6.0

FORECAST_VALUE_FIELDS = ("state", "estimate", "estimate10", "estimate90")

UPDATE_INTERVAL = timedelta(seconds=10)
RIVIAN_LEASE_MINUTES = 10
RIVIAN_RENEW_SECONDS = 5 * 60
STORE_VERSION = 1

STATE_NORMAL = "normal"
STATE_WAITING = "waiting_for_surplus"
STATE_SOLAR = "solar_active"
STATE_MANUAL = "manual_override"
STATE_BLOCKED = "blocked"
STATE_FAULT = "fault"

VEHICLE_TESLA = "tesla"
VEHICLE_RIVIAN = "rivian"
VEHICLE_MODE_BOTH = "both"

SOLAR_BASE_REQUIRED_OPTIONS = (
    CONF_GRID_POWER,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_WALL_CONNECTED,
    CONF_WALL_POWER,
    CONF_WALL_VOLTAGE,
)

SOLAR_TESLA_REQUIRED_OPTIONS = (
    CONF_TESLA_CONNECTED,
    CONF_TESLA_SOC,
    CONF_TESLA_LIMIT,
    CONF_TESLA_AMPS,
    CONF_TESLA_SWITCH,
)

SOLAR_RIVIAN_REQUIRED_OPTIONS = (
    CONF_RIVIAN_CONNECTED,
    CONF_RIVIAN_SOC,
    CONF_RIVIAN_LIMIT,
)
