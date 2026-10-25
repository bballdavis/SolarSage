"""Binary sensors: whether the planner wants each car charging.

Map a Rivian's sensor to Rivian schedule control's ``controller_charging_entity`` option. It is on
while the planner's last command to the car on the charger was a current, off while it was a
pause, and unknown while the planner sends nothing: shadow mode, planner off, or no command
yet in this plug-in session. It follows the commands the car accepted, so it stays in step
with the schedule control's switch.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PlannerConfigEntry, PlannerCoordinator
from .entity import PlannerEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one charging-wanted sensor per car."""
    coordinator = entry.runtime_data
    for car_id, car in coordinator.cars.items():
        async_add_entities(
            [ChargingWantedSensor(coordinator, car_id)], config_subentry_id=car["subentry_id"]
        )


class ChargingWantedSensor(PlannerEntity, BinarySensorEntity):
    """On: the planner wants the car charging; off: paused; unknown: no decision."""

    def __init__(self, coordinator: PlannerCoordinator, car_id: str) -> None:
        super().__init__(coordinator, "charging_wanted", car_id)

    @property
    def available(self) -> bool:
        """Always available: unknown, not unavailable, is how it says "no decision"."""
        return True

    @property
    def is_on(self) -> bool | None:
        assert self.car_id is not None
        return self.coordinator.charging_wanted(self.car_id)
