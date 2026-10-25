"""The owner's Home Assistant entities, as config-flow suggestions (every one stays
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
number, then switch on if it was off. Rivian: the adapter's switch and number (each change is
a cloud schedule write, ~1-2 min). Tesla (Fleet): charge switch and charge current (billed).
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
    "free_hours": "schedule.free_hours",  # the owner's Schedule helper (name it as you like)
}

CAR_ENTITIES: dict[str, dict[str, str]] = {
    "rivian": {
        "soc": "sensor.r1s_battery_state_of_charge",
        "limit": "sensor.r1s_battery_state_of_charge_limit",
        "plugged": "binary_sensor.r1s_charger_connection",
        "location": "device_tracker.r1s_location",
        "switch": "switch.r1s_solar_adapter_solar_charging",
        "current": "number.r1s_solar_adapter_solar_current",
        "control_state": "sensor.r1s_solar_adapter_control_state",
    },
    "tesla": {  # HA core Tesla Fleet names for the car "Roxxo"
        "soc": "sensor.roxxo_battery_level",
        "limit": "number.roxxo_charge_limit",
        "plugged": "binary_sensor.roxxo_charge_cable",
        "location": "device_tracker.roxxo_location",
        "switch": "switch.roxxo_charge",
        "current": "number.roxxo_charge_current",
    },
}

# today's tesla_custom names, for an owner who has not moved to Tesla Fleet yet
TESLA_CUSTOM_ENTITIES: dict[str, str] = {
    "switch": "switch.roxxo_charger",
    "current": "number.roxxo_charging_amps",
    "limit": "number.roxxo_charge_limit",
}
