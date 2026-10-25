"""Constants for the Rivian Solar Charge Adapter integration."""

from __future__ import annotations

from datetime import timedelta
import hashlib
from typing import Any, Final

from homeassistant.const import Platform

DOMAIN: Final = "rivian_solar_adapter"
PLATFORMS: Final = [Platform.BUTTON, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]

TICK_INTERVAL: Final = timedelta(seconds=15)
# After Rivian reports the session as unauthenticated, provider-facing ticks are
# retried this often until reauthentication reloads the entry.
AUTH_RETRY_INTERVAL: Final = timedelta(minutes=5)
# How long disabling or removing the integration may spend restoring the owner's schedule.
REMOVE_RESTORE_TIMEOUT: Final = 60
# How long a mapped plug, location or controller entity may be missing before a repair issue.
MISSING_ENTITY_GRACE: Final = timedelta(minutes=5)
# How long unloading waits for a tick in flight before it is cancelled.
STOP_TIMEOUT: Final = 10
REQUEST_TIMEOUT: Final = 30

STORAGE_VERSION: Final = 1
# Config entry version. Minor version 2 renamed the evcc options and the evcc
# status sensor for the solar planner.
ENTRY_VERSION: Final = 1
ENTRY_MINOR_VERSION: Final = 2


def journal_key(vehicle_id: str) -> str:
    """Return the storage key of a vehicle's ownership journal.

    The journal belongs to the vehicle, not to the config entry, so a removed
    and re-added entry inherits an unfinished journal. The vehicle ID is
    hashed so it does not appear in a file name.
    """
    return f"{DOMAIN}.{hashlib.sha256(vehicle_id.encode()).hexdigest()[:16]}"


def legacy_storage_key(entry_id: str) -> str:
    """Return the storage key journals had when they were keyed by config entry."""
    return f"{DOMAIN}.{entry_id}"


# Config entry data
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_OTP: Final = "otp"
CONF_VEHICLE_ID: Final = "vehicle_id"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_USER_SESSION_TOKEN: Final = "user_session_token"

# Config entry options: Home Assistant entities the adapter observes
CONF_PLUGGED_ENTITY: Final = "plugged_entity"
CONF_LOCATION_ENTITY: Final = "location_entity"
CONF_CHARGER_STATE_ENTITY: Final = "charger_state_entity"
CONF_WALL_CONNECTOR_ENTITY: Final = "wall_connector_entity"
# The controller (the solar planner) provides these two: one that is unavailable
# while it is down, and one that is on while it wants this vehicle charging.
CONF_LIVENESS_ENTITY: Final = "controller_liveness_entity"
CONF_CHARGING_ENTITY: Final = "controller_charging_entity"
CONF_OVERRIDE_ENTITY: Final = "vehicle_override_entity"

ENTITY_OPTIONS: Final = (
    CONF_PLUGGED_ENTITY,
    CONF_LOCATION_ENTITY,
    CONF_CHARGER_STATE_ENTITY,
    CONF_WALL_CONNECTOR_ENTITY,
    CONF_LIVENESS_ENTITY,
    CONF_CHARGING_ENTITY,
    CONF_OVERRIDE_ENTITY,
)
# Mapped entities whose absence is reported as a repair issue. A missing plug or
# location entity is unknown; a missing liveness entity means the controller is
# down, which blocks every start and renewal; a missing charging entity is unknown.
MONITORED_ENTITY_OPTIONS: Final = (
    CONF_PLUGGED_ENTITY,
    CONF_LOCATION_ENTITY,
    CONF_LIVENESS_ENTITY,
    CONF_CHARGING_ENTITY,
)
# Option keys before minor version 2, when evcc was the controller.
LEGACY_OPTION_KEYS: Final = {
    "evcc_liveness_entity": CONF_LIVENESS_ENTITY,
    "evcc_enabled_entity": CONF_CHARGING_ENTITY,
}
# Override entity states that leave the charger status to the adapter.
OVERRIDE_PASS_STATES: Final = frozenset({"auto", "rivian"})

# Config entry options: tuning
CONF_LEASE_MINUTES: Final = "lease_minutes"
CONF_RENEW_BEFORE_MINUTES: Final = "renew_before_minutes"
CONF_CLOCK_SKEW_MINUTES: Final = "clock_skew_minutes"
CONF_MIN_WRITE_INTERVAL: Final = "min_write_interval_s"
CONF_INCREASE_INTERVAL: Final = "increase_interval_s"
CONF_INCREASE_MARGIN: Final = "increase_margin_amps"
CONF_CEILING_HOLD: Final = "ceiling_hold_minutes"
CONF_RELEASE_DELAY: Final = "release_delay_s"
CONF_DRY_RUN: Final = "dry_run"

# Dry run is on by default: the owner must deliberately allow schedule writes.
# The increase margin and the ceiling hold were tuned for evcc, which pads its
# requests and follows the surplus; they are off for a controller that asks for
# exact currents and rate-limits itself (evcc used 3 A and 30 minutes).
DEFAULT_OPTIONS: Final[dict[str, Any]] = {
    CONF_LEASE_MINUTES: 20,
    CONF_RENEW_BEFORE_MINUTES: 8,
    CONF_CLOCK_SKEW_MINUTES: 5,
    CONF_MIN_WRITE_INTERVAL: 30,
    CONF_INCREASE_INTERVAL: 120,
    CONF_INCREASE_MARGIN: 0,
    CONF_CEILING_HOLD: 0,
    CONF_RELEASE_DELAY: 60,
    CONF_DRY_RUN: True,
}

# Inclusive ranges and units of the numeric tuning options.
OPTION_RANGES: Final[dict[str, tuple[int, int, str]]] = {
    CONF_LEASE_MINUTES: (10, 60, "min"),
    CONF_RENEW_BEFORE_MINUTES: (1, 59, "min"),
    CONF_CLOCK_SKEW_MINUTES: (0, 15, "min"),
    CONF_MIN_WRITE_INTERVAL: (15, 600, "s"),
    CONF_INCREASE_INTERVAL: (30, 1800, "s"),
    CONF_INCREASE_MARGIN: (0, 10, "A"),
    CONF_CEILING_HOLD: (0, 120, "min"),
    CONF_RELEASE_DELAY: (0, 1800, "s"),
}

# Range of the current the controller may request; the adapter applies even 8-48 A.
REQUEST_MIN_AMPS: Final = 0
REQUEST_MAX_AMPS: Final = 80

# Rivian chargerState values that mean the vehicle is charging.
ACTIVE_CHARGER_STATES: Final = frozenset(
    {"charging_active", "charging_connecting", "smart_charging_active"}
)

# Charger status values (IEC 61851 states A/B/C).
CHARGER_STATUS_DISCONNECTED: Final = "a"
CHARGER_STATUS_CONNECTED: Final = "b"
CHARGER_STATUS_CHARGING: Final = "c"
CHARGER_STATUSES: Final = [
    CHARGER_STATUS_DISCONNECTED,
    CHARGER_STATUS_CONNECTED,
    CHARGER_STATUS_CHARGING,
]
# The charger status sensor's key in its unique ID, and the key before minor version 2.
CHARGER_STATUS_KEY: Final = "charger_status"
LEGACY_CHARGER_STATUS_KEY: Final = "evcc_status"

# Services
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
SERVICE_RELEASE: Final = "release"
SERVICE_CLEAR_FAULT: Final = "clear_fault"
SERVICE_ABANDON_OWNERSHIP: Final = "abandon_ownership"
SERVICE_FORCE_RESTORE: Final = "force_restore"
SERVICE_READ_SCHEDULES: Final = "read_schedules"

DEVICE_MANUFACTURER: Final = "HASS-Solar-Rivian"
DEVICE_MODEL: Final = "Rivian charge adapter"
