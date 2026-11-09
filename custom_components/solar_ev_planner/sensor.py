"""Sensors: what the planner decided and why, the battery outlook, the forecast correction,
each car's planned current and energy outlook, the heartbeat, and (with a Tesla) what the
planner's Tesla Fleet calls cost this month.

The heartbeat is Rivian schedule control's ``controller_liveness_entity``: its state is
``active`` (commanding) or ``shadow`` while the planner is on and its last decision is recent,
and it is unavailable while the planner is off, cannot decide (missing site readings, an
unreadable tariff, an error) or has not decided for ``HEARTBEAT_TIMEOUT``. It re-checks itself
every 30 seconds, so a stuck planner shows as down without any other update.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import CONF_KIND, CONF_NAME, HEARTBEAT_CHECK, KIND_RIVIAN, MODES
from .coordinator import CarView, PlannerConfigEntry, PlannerCoordinator, PlannerData
from .entity import PlannerEntity
from .spend import next_month

PARALLEL_UPDATES = 0
MAX_STATE = 255
NONE = "none"


def _short(text: str) -> str:
    return text if len(text) <= MAX_STATE else text[: MAX_STATE - 1] + "…"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the planner's and each car's sensors."""
    if "vehicle_id" in entry.data:
        from .rivian.sensor import async_setup_entry as setup_rivian  # noqa: PLC0415

        await setup_rivian(hass, entry, async_add_entities)
        return
    coordinator = entry.runtime_data
    async_add_entities(
        [
            ModeSensor(coordinator),
            ReasonSensor(coordinator),
            NextChangeSensor(coordinator),
            BatteryMinimumSensor(coordinator),
            BatteryMinimumTimeSensor(coordinator),
            ForecastCorrectionSensor(coordinator),
            CarOnChargerSensor(coordinator),
            HeartbeatSensor(coordinator),
        ]
    )
    if coordinator.spend.teslas:
        async_add_entities([TeslaSpendSensor(coordinator)])
    for car_id, car in coordinator.cars.items():
        async_add_entities(
            [
                PlannedCurrentSensor(coordinator, car_id),
                OutlookSensor(coordinator, car_id),
                *(OutlookEnergySensor(coordinator, car_id, d) for d in OUTLOOK_ENERGY),
            ],
            config_subentry_id=car["subentry_id"],
        )
        if car[CONF_KIND] == KIND_RIVIAN:
            async_add_entities(
                [ChargingUntilSensor(coordinator, car_id)], config_subentry_id=car["subentry_id"]
            )


class DataSensor(PlannerEntity, SensorEntity):
    """A sensor whose value comes from the last cycle."""

    @property
    def data(self) -> PlannerData | None:
        return self.coordinator.data


# --------------------------------------------------------------------------- the planner


class ModeSensor(DataSensor):
    """off, empty, stale, full, day, night, or no_data when it could not decide."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = MODES

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "mode")

    @property
    def native_value(self) -> str | None:
        return self.data.mode if self.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.data.decision if self.data else None
        return {
            "shadow": self.coordinator.shadow,
            "commanding": self.coordinator.active,
            "car": d.car_id if d else None,
            "safety": d.safety if d else None,
        }


class ReasonSensor(DataSensor):
    """The decision in one sentence (cut to 255 characters; the attribute is complete)."""

    _unrecorded_attributes = frozenset({"full_reason"})

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "reason")

    @property
    def native_value(self) -> str | None:
        return _short(self.data.reason) if self.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"full_reason": self.data.reason if self.data else None}


class NextChangeSensor(DataSensor):
    """When the planner expects to change the current next, and to what."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "next_change")

    @property
    def native_value(self) -> datetime | None:
        d = self.data.decision if self.data else None
        return d.next_change.at if d and d.next_change else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.data.decision if self.data else None
        nxt = d.next_change if d else None
        return {"amps": nxt.amps if nxt else None, "why": nxt.why if nxt else None}


def _lowest(data: PlannerData | None) -> Any:
    d = data.decision if data else None
    if d is None or not d.trajectory:
        return None
    return min(d.trajectory, key=lambda p: p.soc_pct)


class BatteryMinimumSensor(DataSensor):
    """The home battery's predicted lowest level (at the caution level) until tomorrow's free
    hours or sunset; the floor rule keeps this above the floor."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0
    _unrecorded_attributes = frozenset({"trajectory"})

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "battery_minimum")

    @property
    def native_value(self) -> float | None:
        low = _lowest(self.data)
        return round(low.soc_pct, 1) if low else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.data.decision if self.data else None
        if d is None or not d.trajectory:
            return {}
        tr = d.trajectory
        low = _lowest(self.data)
        return {
            "at": low.t.isoformat(),
            "expected_minimum": round(min(p.soc_expected_pct for p in tr), 1),
            "without_car_minimum": round(min(p.soc_no_car_pct for p in tr), 1),
            "floor": float(self.coordinator.core_settings().floor_pct),
            "at_free_hours_start": d.debug.get("soc_free"),
            # half-hourly: the cautious path, the expected day, and without the car
            "trajectory": [
                {
                    "t": p.t.isoformat(),
                    "soc": round(p.soc_pct, 1),
                    "expected": round(p.soc_expected_pct, 1),
                    "no_car": round(p.soc_no_car_pct, 1),
                    "car_kw": round(p.car_kw, 2),
                }
                for p in tr[1::2]
            ],
        }


class BatteryMinimumTimeSensor(DataSensor):
    """When the predicted lowest battery level is reached."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "battery_minimum_time")

    @property
    def native_value(self) -> datetime | None:
        low = _lowest(self.data)
        return low.t if low else None


class ForecastCorrectionSensor(DataSensor):
    """The factor today's production so far puts on the rest of today's Solcast forecast
    (the Bayesian correction; 1.0 early in the morning)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "forecast_correction")

    @property
    def native_value(self) -> float | None:
        return self.data.pv.get("ratio") if self.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.data:
            return {}
        return {k: v for k, v in self.data.pv.items() if k != "ratio"} | {
            "forecast_slots": self.data.forecast_slots
        }


class CarOnChargerSensor(DataSensor):
    """Which car the planner sees on the Wall Connector (none, or a car id), and which car
    should be on it in the coming free hours."""

    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "car_on_charger")
        self._attr_options = [NONE, *coordinator.cars]

    @property
    def native_value(self) -> str | None:
        if not self.data:
            return None
        return self.data.identification.car_id or NONE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.data:
            return {}
        ident, site = self.data.identification, self.data.site
        d = self.data.decision
        tonight = d.debug.get("tonight_car") if d else None
        return {
            "why": ident.reason,
            "candidates": list(ident.candidates),
            "override": ident.override,
            "wall_connector_connected": site.wall_connected,
            "contactor_closed": site.contactor_closed,
            "wall_current": site.wall_current_a,
            "wall_power_kw": site.wall_kw,
            "tonight_car": tonight,
            "tonight_car_name": (
                self.coordinator.cars[tonight][CONF_NAME]
                if tonight in self.coordinator.cars
                else None
            ),
        }


class HeartbeatSensor(DataSensor):
    """The planner's liveness for Rivian schedule control (see the module docstring)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["active", "shadow"]
    _unrecorded_attributes = frozenset({"last_decision"})

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "heartbeat")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_check, HEARTBEAT_CHECK, cancel_on_shutdown=True
            )
        )

    @callback
    def _async_check(self, now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self.coordinator.healthy()

    @property
    def native_value(self) -> str:
        return "active" if self.coordinator.active else "shadow"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self.coordinator.last_success
        return {"last_decision": last.isoformat() if last else None}


class TeslaSpendSensor(PlannerEntity, SensorEntity):
    """What the planner's own Tesla Fleet calls cost this calendar month (USD, Tesla's prices),
    with the counts, the cap and the guard's level (``spend``)."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "USD"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: PlannerCoordinator) -> None:
        super().__init__(coordinator, "tesla_api_spend")

    @property
    def available(self) -> bool:
        """Always available: the count is kept by the planner, also while a cycle fails."""
        return True

    @property
    def native_value(self) -> float:
        return round(self.coordinator.spend.spent(dt_util.now()), 3)

    @property
    def last_reset(self) -> datetime:
        now = dt_util.now()
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = dt_util.now()
        return self.coordinator.spend.attributes(now) | {"resets": next_month(now).isoformat()}


# --------------------------------------------------------------------------- each car


class CarSensor(DataSensor):
    @property
    def view(self) -> CarView | None:
        return self.car_view


class PlannedCurrentSensor(CarSensor):
    """The current the plan has the car at now (in shadow mode, the virtual car's), 0 when
    it is not on the charger."""

    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: PlannerCoordinator, car_id: str) -> None:
        super().__init__(coordinator, "planned_current", car_id)

    @property
    def native_value(self) -> int | None:
        return self.view.planned_a if self.view else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        v = self.view
        if v is None:
            return {}
        standing = v.standing
        return {
            "on_charger": v.on_charger,
            "wanted": v.want_a,
            "command": v.command,
            "reason": v.reason,
            "commanded": v.commanded_a,
            "car_charging": standing.charging if standing else None,
            "car_current": standing.amps if standing else None,
            "schedule_control_state": standing.schedule_control_state if standing else None,
            "waiting": v.blocked,
            "last_error": v.last_error,
            "stale": v.stale,
            "soc": v.soc_pct,
            "limit": v.limit_pct,
        }


class ChargingUntilSensor(PlannerEntity, SensorEntity):
    """Until when the planner charges the Rivian: the end of the free hours while it charges
    the car through them, unknown otherwise. Rivian schedule control's
    ``controller_until_entity``: it then writes one window to that time, once."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: PlannerCoordinator, car_id: str) -> None:
        super().__init__(coordinator, "charging_until", car_id)

    @property
    def available(self) -> bool:
        """Always available: unknown, not unavailable, is how it says "no end time"."""
        return True

    @property
    def native_value(self) -> datetime | None:
        assert self.car_id is not None
        return self.coordinator.charging_until(self.car_id)


class OutlookSensor(CarSensor):
    """Where the car's energy is expected to come from, in words ("covered by sun", "about
    7 kWh in free hours", ...)."""

    def __init__(self, coordinator: PlannerCoordinator, car_id: str) -> None:
        super().__init__(coordinator, "outlook", car_id)

    @property
    def native_value(self) -> str | None:
        o = self.view.outlook if self.view else None
        return _short(o.summary) if o else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        o = self.view.outlook if self.view else None
        if o is None:
            return {}
        return {
            "need_kwh": o.need_kwh,
            "target": o.target_pct,
            "by": o.by.isoformat() if o.by else None,
            "solar_kwh": o.solar_kwh,
            "free_kwh": o.free_kwh,
            "paid_kwh": o.paid_kwh,
            "short_kwh": o.short_kwh,
        }


@dataclass(frozen=True)
class OutlookEnergy:
    key: str
    value: Callable[[Any], float]


OUTLOOK_ENERGY = (
    OutlookEnergy("solar_energy", lambda o: o.solar_kwh),
    OutlookEnergy("free_energy", lambda o: o.free_kwh),
    OutlookEnergy("paid_energy", lambda o: o.paid_kwh),
)


class OutlookEnergySensor(CarSensor):
    """kWh the car is expected to get from the sun (and the battery within the floor rule),
    from the free hours, or from paid hours."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: PlannerCoordinator, car_id: str, spec: OutlookEnergy) -> None:
        super().__init__(coordinator, spec.key, car_id)
        self.spec = spec

    @property
    def native_value(self) -> float | None:
        o = self.view.outlook if self.view else None
        return self.spec.value(o) if o else None
