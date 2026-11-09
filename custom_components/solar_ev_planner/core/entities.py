"""Example Home Assistant entities, as config-flow suggestions (every one stays
configurable). Strings only: this package never imports Home Assistant.

How each input of a ``Snapshot`` is read:

  soc_pct              battery_soc (%)
  battery.capacity_kwh battery_capacity (kWh, about 31)
  measured             pv_power (W), grid_power (W, negative = export), battery_power (W,
                       positive = charging), wall_power (kW); 5-min means
  house load           pv + grid - battery - wall (house_load_kw); no house sensor exists
  load                 build_load_profile(samples_from_statistics(...)) over the last 14 days
                       of recorder statistics of those four sensors
  forecast             parse_detailed_forecast(forecast_today attrs, forecast_tomorrow attrs)
  forecast_issued_at   forecast_last_polled
  tariff               Tariff(price, buyback, (PriceWindow(WeeklySchedule.from_ha(<free
                       schedule config>), 0.0, "free"), ...))
  cars                 which car is on the Wall Connector: wall_connected on, and the car whose
                       own cable / connection sensor is on and that is home (location); SoC and
                       limit from the car's sensors; commanded_a = the planner's last command
                       (or the car's current number); the Wall Connector's vehicle current
                       confirms a change. Freshness: an unchanged value keeps an old
                       last_reported timestamp, so a value is stale only when unavailable or
                       when its integration has gone quiet, never from its own timestamp.

Commands (CarCommand.amps): None = no service call; 0 = switch off; > 0 = set the current
number, then switch on if it was off. Rivian: Rivian schedule control's switch and number
(each change is a cloud schedule write, ~1-2 min). Tesla (Fleet): charge switch and charge
current (billed); its charging sensor, optional, keeps the planner from sending a start the
car cannot act on (complete, disconnected, no_power); its status sensor, optional, tells the
spending guard whether a command needs a wake.
"""

from __future__ import annotations

SITE_ENTITIES: dict[str, str] = {
    "pv_power": "sensor.sol_ark_inverter_pv_power",  # W
    "grid_power": "sensor.sol_ark_inverter_grid_power",  # W, negative = export
    "battery_power": "sensor.sol_ark_inverter_battery_power",  # W, positive = charging
    "battery_soc": "sensor.sol_ark_inverter_battery_state_of_charge",  # %
    "battery_capacity": "sensor.sol_ark_battery_capacity_estimate",  # kWh
    "wall_power": "sensor.tesla_wall_connector_total_power",  # kW
    "wall_current": "sensor.garage_tesla_wall_connector_vehicle_current",  # A
    "wall_connected": "binary_sensor.tesla_wall_connector_vehicle_connected",
    "wall_contactor": "binary_sensor.tesla_wall_connector_contactor_closed",
    "forecast_today": "sensor.solcast_pv_forecast_forecast_today",
    "forecast_tomorrow": "sensor.solcast_pv_forecast_forecast_tomorrow",
    "forecast_remaining_today": "sensor.solcast_pv_forecast_forecast_remaining_today",
    "forecast_last_polled": "sensor.solcast_pv_forecast_api_last_polled",
    "free_hours": "schedule.free_hours",  # example Schedule helper (name it as you like)
}

CAR_ENTITIES: dict[str, dict[str, str]] = {
    "rivian": {
        "soc": "sensor.r1s_battery_state_of_charge",
        "limit": "sensor.r1s_battery_state_of_charge_limit",
        "plugged": "binary_sensor.r1s_charger_connection",
        "location": "device_tracker.r1s_location",  # or device_tracker.r1s (below)
        "switch": "switch.r1s_schedule_control_solar_charging",
        "current": "number.r1s_schedule_control_solar_current",
        "control_state": "sensor.r1s_schedule_control_control_state",
    },
    "tesla": {  # HA core Tesla Fleet names for the car "Tesla"
        "soc": "sensor.tesla_battery_level",
        "limit": "number.tesla_charge_limit",
        "plugged": "binary_sensor.tesla_charge_cable",
        "location": "device_tracker.tesla_location",
        "switch": "switch.tesla_charge",
        "current": "number.tesla_charge_current",
        "charging_state": "sensor.tesla_charging",  # enum: charging, complete, disconnected, ...
        "status": "binary_sensor.tesla_status",  # on while the car is online (awake)
    },
}

# Other IDs the same entity has, tried in order when the first one does not exist. Upstream
# Rivian installs made before 2026-10-12 name the tracker r1s_location; fresh 2.0.0 installs
# name it after the car.
CAR_ENTITY_ALTERNATIVES: dict[str, dict[str, tuple[str, ...]]] = {
    "rivian": {"location": ("device_tracker.r1s",)},
}

# Readings preferred over the ones above when they exist: TeslaMate's, bridged over MQTT
# (docs/PLANNER.md, "TeslaMate"). TeslaMate streams them for free, so Tesla Fleet's polling
# can stay off; the commands still go through Tesla Fleet.
CAR_ENTITY_PREFERRED: dict[str, dict[str, str]] = {
    "tesla": {
        "soc": "sensor.teslamate_tesla_battery_level",
        "limit": "sensor.teslamate_tesla_charge_limit",
        "plugged": "binary_sensor.teslamate_tesla_plugged_in",
        "location": "device_tracker.teslamate_tesla_location",
        "charging_state": "sensor.teslamate_tesla_charging",  # Tesla Fleet's states, lowercased
        "status": "binary_sensor.teslamate_tesla_status",  # on while online, charging, driving
    },
}

# The custom tesla_custom integration's names. Never suggested: on the 2020 Model Y it cannot
# command without its proxy add-on, and it takes Tesla Fleet's IDs (the limit is the same), so
# Tesla Fleet's own entities would get a _2 suffix. tools/check.py names leftovers to remove.
TESLA_CUSTOM_ENTITIES: dict[str, str] = {
    "switch": "switch.tesla_charger",
    "current": "number.tesla_charging_amps",
    "limit": "number.tesla_charge_limit",
}
