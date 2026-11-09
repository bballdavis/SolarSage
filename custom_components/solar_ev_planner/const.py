"""Constants for the SolarSage integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final

from homeassistant.const import Platform

from .core import CAUTION_LEVELS, RIVIAN_R1S, TESLA

DOMAIN: Final = "solar_ev_planner"
PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]

# The coordinator reads the states every minute. The core re-plans the daytime current every
# quarter hour (and on a plug-in, a new forecast or a mode change); the minute calls run its
# safety rules and rate limits.
READ_INTERVAL: Final = timedelta(minutes=1)
# House-use history from the recorder: 14 days of hourly statistics, refreshed this often
# and at every date change, off the event loop.
HISTORY_DAYS: Final = 14
HISTORY_REFRESH: Final = timedelta(hours=6)
RECENT_HOURS: Final = 24
# The free-hours and price Schedule helpers are re-read this often.
SCHEDULE_REFRESH: Final = timedelta(minutes=15)
# The heartbeat is unavailable once no planning cycle has succeeded for this long.
HEARTBEAT_TIMEOUT: Final = timedelta(minutes=3)
HEARTBEAT_CHECK: Final = timedelta(seconds=30)
# The site's power sensors count as stale when none of them has reported for this long. One
# unchanged value keeps an old timestamp and is never stale by itself.
SITE_QUIET_AFTER: Final = timedelta(minutes=30)
# A car counts as "command in flight" this long after a command (Rivian schedule writes take
# a minute or two); the core's safety rules wait meanwhile.
PENDING_AFTER_SEND: Final = timedelta(minutes=3)
# The executor's own limits, on top of the core's (minimum time between changes, largest
# change): at most one command per car per minute, and an hourly cap that a pause ignores.
SEND_SPACING: Final = timedelta(seconds=60)
MAX_SENDS_PER_HOUR: Final = 12
FAILURE_BACKOFF_MAX: Final = timedelta(minutes=15)
# A Tesla (billed commands, each may wake the car) stops retrying after this many failures in
# a row, until its plug, SoC or limit changes, or this long has passed.
STALL_AFTER_FAILURES: Final = 3
STALL_BACKOFF: Final = timedelta(hours=3)
SERVICE_TIMEOUT: Final = 60

# The Tesla spending guard. Tesla's Fleet API price table (USD, developer.tesla.com
# billing-and-limits, read 2026-11-05): commands $1 per 1,000, data requests $1 per 500, wakes
# $1 per 50, with a $10 discount per account and month. Every request below status 500 is
# billed, a refused or failed command too. Tesla Fleet's state check before vehicle_data is
# "free" by HA's comment but unconfirmed, so it is counted as a data request.
TESLA_PRICE_COMMAND: Final = 0.001
TESLA_PRICE_DATA: Final = 0.002
TESLA_PRICE_WAKE: Final = 0.02
TESLA_CREDIT: Final = 10.0
# Kept free of the credit whatever the settings say: the counts are the planner's own, and
# Tesla's billing month may not start with the calendar month.
TESLA_MARGIN: Final = 1.0
# At this share of the planner's cap only essential commands go out; at the cap minus the
# cost of one more send (TESLA_WORST_SEND) nothing is sent until the next month.
TESLA_ESSENTIAL_AT: Final = 0.7
# A wake (Tesla Fleet wakes an asleep car before each command) is followed by up to three
# state checks, counted as data requests. A refresh is the state check plus vehicle_data.
TESLA_WAKE_CHECKS: Final = 3
TESLA_REFRESH_DATA: Final = 2
TESLA_WORST_SEND: Final = (
    2 * TESLA_PRICE_COMMAND
    + TESLA_PRICE_WAKE
    + (TESLA_WAKE_CHECKS + TESLA_REFRESH_DATA) * TESLA_PRICE_DATA
)
# Before a command, the Tesla is refreshed (with Tesla Fleet's polling off) when its data is
# older than this, so Tesla Fleet neither wakes an awake car nor commands an asleep one.
TESLA_FRESH_BEFORE_SEND: Final = timedelta(minutes=10)
PV_DAYS_KEPT: Final = 21

STORAGE_VERSION: Final = 1
STORE_SAVE_DELAY: Final = 30

ENTRY_TITLE: Final = "SolarSage"

# Config entry options: the site ----------------------------------------------------------
CONF_PV_POWER: Final = "pv_power"
CONF_GRID_POWER: Final = "grid_power"
CONF_BATTERY_POWER: Final = "battery_power"
CONF_WALL_POWER: Final = "wall_power"
# The four powers, in the order house = pv + grid - battery - wall needs them.
POWER_KEYS: Final = (CONF_PV_POWER, CONF_GRID_POWER, CONF_BATTERY_POWER, CONF_WALL_POWER)
UNIT_W: Final = "W"
UNIT_KW: Final = "kW"
POWER_UNITS: Final = (UNIT_W, UNIT_KW)
# kW per unit; a sensor's own unit_of_measurement wins over the configured one.
POWER_SCALE: Final[dict[str, float]] = {"W": 0.001, "kW": 1.0, "MW": 1000.0}
DEFAULT_POWER_UNITS: Final[dict[str, str]] = {
    CONF_PV_POWER: UNIT_W,
    CONF_GRID_POWER: UNIT_W,
    CONF_BATTERY_POWER: UNIT_W,
    CONF_WALL_POWER: UNIT_KW,
}


def unit_key(key: str) -> str:
    """The option holding a power sensor's unit."""
    return f"{key}_unit"


def invert_key(key: str) -> str:
    """The option that flips a power sensor's sign (grid + import, battery + charging)."""
    return f"{key}_invert"


CONF_BATTERY_SOC: Final = "battery_soc"
CONF_BATTERY_CAPACITY: Final = "battery_capacity"  # a sensor (kWh), optional
CONF_BATTERY_CAPACITY_KWH: Final = "battery_capacity_kwh"  # the fixed value otherwise
CONF_BATTERY_MAX_CHARGE_KW: Final = "battery_max_charge_kw"
CONF_BATTERY_MAX_DISCHARGE_KW: Final = "battery_max_discharge_kw"
CONF_BATTERY_RESERVE_PCT: Final = "battery_reserve_pct"
CONF_WALL_CURRENT: Final = "wall_current"
CONF_WALL_CONNECTED: Final = "wall_connected"
CONF_WALL_CONTACTOR: Final = "wall_contactor"
CONF_CHARGER_MAX_A: Final = "charger_max_a"
CONF_VOLTS: Final = "volts"
CONF_FORECAST_TODAY: Final = "forecast_today"
CONF_FORECAST_TOMORROW: Final = "forecast_tomorrow"
CONF_FORECAST_POLLED: Final = "forecast_last_polled"
CONF_FREE_HOURS: Final = "free_hours"

DEFAULT_SITE_OPTIONS: Final[dict[str, Any]] = {
    **{unit_key(k): u for k, u in DEFAULT_POWER_UNITS.items()},
    **{invert_key(k): False for k in POWER_KEYS},
    CONF_BATTERY_CAPACITY_KWH: 31.0,
    CONF_BATTERY_MAX_CHARGE_KW: 9.0,
    CONF_BATTERY_MAX_DISCHARGE_KW: 9.0,
    CONF_BATTERY_RESERVE_PCT: 10.0,
    CONF_CHARGER_MAX_A: 48,
    CONF_VOLTS: 240,
}

# Config subentries ------------------------------------------------------------------------
SUBENTRY_CAR: Final = "car"
SUBENTRY_PRICE: Final = "price_window"

# A car
CONF_CAR_ID: Final = "car_id"
CONF_KIND: Final = "kind"
CONF_NAME: Final = "name"
CONF_SWITCH: Final = "switch"
CONF_CURRENT: Final = "current"
CONF_SOC: Final = "soc"
CONF_LIMIT: Final = "limit"
CONF_PLUGGED: Final = "plugged"
CONF_LOCATION: Final = "location"
CONF_CONTROL_STATE: Final = "control_state"
CONF_CHARGING_STATE: Final = "charging_state"  # a Tesla's charging sensor, optional
CONF_STATUS: Final = "status"  # a Tesla's status (online) binary sensor, optional
CONF_MIN_A: Final = "min_a"
CONF_MAX_A: Final = "max_a"
CONF_STEP_A: Final = "step_a"
CONF_CAPACITY_KWH: Final = "capacity_kwh"
KIND_RIVIAN: Final = "rivian"
KIND_TESLA: Final = "tesla"
CAR_KINDS: Final = (KIND_RIVIAN, KIND_TESLA)
CAR_DEFAULTS: Final[dict[str, dict[str, Any]]] = {
    KIND_RIVIAN: {
        CONF_NAME: "R1S",
        CONF_MIN_A: RIVIAN_R1S.min_a,
        CONF_MAX_A: RIVIAN_R1S.max_a,
        CONF_STEP_A: RIVIAN_R1S.step_a,
        CONF_CAPACITY_KWH: RIVIAN_R1S.capacity_kwh,
    },
    KIND_TESLA: {
        CONF_NAME: "Tesla",
        CONF_MIN_A: TESLA.min_a,
        CONF_MAX_A: TESLA.max_a,
        CONF_STEP_A: TESLA.step_a,
        CONF_CAPACITY_KWH: TESLA.capacity_kwh,
    },
}

# A price window: a Schedule helper painted with one price
CONF_SCHEDULE: Final = "schedule"
CONF_LABEL: Final = "label"
CONF_PRICE: Final = "price"
CONF_EXPORT_PRICE: Final = "export_price"

# Owner settings (entities, persisted in the planner's store) ------------------------------
S_ENABLED: Final = "enabled"
S_SHADOW: Final = "shadow"
S_DAYTIME_SOLAR: Final = "daytime_solar"
S_CAUTION: Final = "caution"
S_FLOOR: Final = "floor_pct"
S_WANTED: Final = "wanted_at_free_start_pct"
S_MIN_CHANGE: Final = "min_change_min"
S_MAX_STEP: Final = "max_step_a"
S_PAID_TOLERANCE: Final = "paid_tolerance_min"
S_PRICE: Final = "price"
S_BUYBACK: Final = "buyback"
S_OVERRIDE: Final = "override"
S_TESLA_BUDGET: Final = "tesla_budget"  # USD a month for the planner's own Tesla calls
S_TESLA_POLLING: Final = "tesla_polling"  # USD a month Tesla Fleet's own polling may cost
S_TESLA_REFRESH: Final = "tesla_refresh_min"  # minutes; 0 = the planner never refreshes
S_NIGHT_START: Final = "night_start"  # NIGHT_FREE_START or "HH:MM"

OVERRIDE_AUTO: Final = "auto"

DEFAULT_SETTINGS: Final[dict[str, Any]] = {
    S_ENABLED: True,
    S_SHADOW: True,
    S_DAYTIME_SOLAR: True,
    S_CAUTION: "conservative",
    S_FLOOR: 30.0,
    S_WANTED: 30.0,
    S_MIN_CHANGE: 15.0,
    S_MAX_STEP: 6,
    S_PAID_TOLERANCE: 5.0,
    S_PRICE: 0.32,
    S_BUYBACK: 0.0,
    S_OVERRIDE: OVERRIDE_AUTO,
    S_TESLA_BUDGET: 5.0,
    # Tesla Fleet polls an awake car every 10 minutes: about 84 vehicle_data a day with the
    # car awake 14 hours, $5.11 a month. Not counted once its polling is turned off.
    S_TESLA_POLLING: 5.2,
    S_TESLA_REFRESH: 60.0,
    S_NIGHT_START: "free_hours_start",
}

# Caution select options (translation-friendly keys) -> the core's labels.
CAUTION_OPTIONS: Final[dict[str, str]] = {
    label.replace(" ", "_").replace("-", "_"): label for label in CAUTION_LEVELS
}

# Night charging start: with the free hours, or a half hour from 18:00 to 05:30. A time that
# falls outside a free period means that period's start.
NIGHT_FREE_START: Final = "free_hours_start"
NIGHT_START_OPTIONS: Final = [
    NIGHT_FREE_START,
    *(f"{(18 + i // 2) % 24:02d}:{30 * (i % 2):02d}" for i in range(24)),
]

# Per-car settings
C_LIMIT: Final = "limit_pct"  # 0 = the car's own limit
C_PLAN_TARGET: Final = "plan_target_pct"
C_PLAN_TIME: Final = "plan_time"
C_PLAN_DAYS: Final = "plan_days"
C_PLAN_ONCE_AT: Final = "plan_once_at"  # internal: the deadline of a one-off plan
C_RESTORE: Final = "restore_a"  # Tesla: current restored at unplug, 0 = off

PLAN_OFF: Final = "off"
PLAN_ONCE: Final = "once"
PLAN_DAYS: Final[dict[str, tuple[int, ...]]] = {
    PLAN_OFF: (),
    PLAN_ONCE: (),
    "weekdays": (0, 1, 2, 3, 4),
    "daily": (0, 1, 2, 3, 4, 5, 6),
    "weekends": (5, 6),
}


def default_car_settings(kind: str, max_a: int) -> dict[str, Any]:
    """Per-car settings before the owner changes them."""
    return {
        C_LIMIT: 0,
        C_PLAN_TARGET: 80,
        C_PLAN_TIME: "07:30",
        C_PLAN_DAYS: PLAN_OFF,
        C_PLAN_ONCE_AT: None,
        # The Tesla keeps the last current it was given; restore its maximum at unplug so a
        # later charge elsewhere does not start at 6 A. Rivian schedule control restores itself.
        C_RESTORE: max_a if kind == KIND_TESLA else 0,
    }


# Modes the mode sensor shows: the core's, plus no_data when the planner could not decide.
MODE_NO_DATA: Final = "no_data"
MODES: Final = ["off", "empty", "stale", "full", "day", "night", MODE_NO_DATA]

# Rivian schedule control's control states in which the planner does not repeat a request.
SCHEDULE_CONTROL_IGNORES: Final = frozenset({"handed_back", "fault", "waiting_for_vehicle"})
SCHEDULE_CONTROL_WRITING: Final = "writing"

# Tesla Fleet's charging sensor states in which a start command cannot start a charge (the
# car is at its limit, unplugged or the charger gives no power): the planner sends no "on".
TESLA_CANNOT_START: Final = frozenset({"complete", "disconnected", "no_power"})
# Tesla Fleet's command refusals that mean the car already is where the command would take it.
TESLA_ALREADY_DONE: Final[dict[str, frozenset[str]]] = {
    "on": frozenset({"complete", "is_charging"}),
    "off": frozenset({"not_charging"}),
}

DEVICE_MANUFACTURER: Final = "SolarSage"
DEVICE_MODEL: Final = "SolarSage"
DEVICE_MODEL_CAR: Final = "Car charging plan"
DEVICE_MODEL_WINDOW: Final = "Price window"

# Redacted from diagnostics at any depth.
REDACT_KEYS: Final = frozenset(
    {
        "latitude",
        "longitude",
        "gps_accuracy",
        "gps",
        "vin",
        "email",
        "token",
        "access_token",
        "refresh_token",
        "password",
    }
)
