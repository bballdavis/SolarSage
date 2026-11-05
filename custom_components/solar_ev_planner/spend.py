"""The Tesla spending guard: the planner counts and prices every Tesla Fleet call it causes,
so that with Tesla Fleet's own polling the account stays inside Tesla's monthly $10 discount
and the owner is never charged.

Counted per calendar month (local time) and kept in the planner's store, so a restart does
not reset them:

- commands: each charge switch or charge current call, billed also when refused or failed;
- wakes: Tesla Fleet wakes an asleep car before a command, so a send to a Tesla whose status
  sensor (``binary_sensor.<car>_status``, mapped on the car) does not read online counts one
  wake, and its follow-up state checks as data requests. Without that sensor every send
  counts a wake;
- data requests: the planner's own refreshes of the Tesla (``homeassistant.update_entity`` on
  its battery level), a state check and vehicle_data each, both counted.

The planner's cap is the smaller of its budget and Tesla's credit less a margin and less Tesla
Fleet's own polling estimate (not counted once that integration's polling is turned off). As
the month's spend rises:

- **normal** below ``TESLA_ESSENTIAL_AT`` of the cap;
- **essential only** from there: a Tesla is started only in the free hours, and by day a
  decrease becomes a stop (the free hours' end, the paid-import and battery-floor safety
  rules), so the car never draws paid power the planner did not want. Daytime starts and
  increases, the timed refreshes and the restore at unplug wait;
- **stopped** once one more send could pass the cap: nothing at all goes to the Tesla, not
  even a stop or a refresh, until the next month.

Refreshes replace Tesla Fleet's polling, which costs more: they run only while that polling
is off, every ``S_TESLA_REFRESH`` minutes while the Tesla is on the Wall Connector, once when
the Wall Connector reports a car connected (is it the Tesla?) or the Tesla gone, and before a
command when the Tesla's data is older than ``TESLA_FRESH_BEFORE_SEND``, so Tesla Fleet
neither wakes an awake car nor sends a command to an asleep one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CAR_ID,
    CONF_KIND,
    CONF_SOC,
    CONF_STATUS,
    KIND_TESLA,
    S_TESLA_BUDGET,
    S_TESLA_POLLING,
    S_TESLA_REFRESH,
    SERVICE_TIMEOUT,
    TESLA_CREDIT,
    TESLA_ESSENTIAL_AT,
    TESLA_FRESH_BEFORE_SEND,
    TESLA_MARGIN,
    TESLA_PRICE_COMMAND,
    TESLA_PRICE_DATA,
    TESLA_PRICE_WAKE,
    TESLA_REFRESH_DATA,
    TESLA_WAKE_CHECKS,
    TESLA_WORST_SEND,
)

_LOGGER = logging.getLogger(__name__)

LEVEL_NORMAL = "normal"
LEVEL_ESSENTIAL = "essential_only"
LEVEL_STOPPED = "stopped"
LEVELS = [LEVEL_NORMAL, LEVEL_ESSENTIAL, LEVEL_STOPPED]


def month_key(now: datetime) -> str:
    return f"{now:%Y-%m}"


def next_month(now: datetime) -> datetime:
    """Midnight on the first of the next month."""
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first + timedelta(days=32)).replace(day=1)


@dataclass
class SpendCounts:
    """One month's billable calls the planner caused."""

    month: str
    commands: int = 0
    wakes: int = 0
    data: int = 0
    refreshes: int = 0

    @property
    def usd(self) -> float:
        return (
            self.commands * TESLA_PRICE_COMMAND
            + self.wakes * TESLA_PRICE_WAKE
            + self.data * TESLA_PRICE_DATA
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "commands": self.commands,
            "wakes": self.wakes,
            "data": self.data,
            "refreshes": self.refreshes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SpendCounts:
        def count(key: str) -> int:
            try:
                return max(0, int(data.get(key) or 0))
            except TypeError, ValueError:
                return 0

        return cls(
            str(data.get("month") or ""),
            count("commands"),
            count("wakes"),
            count("data"),
            count("refreshes"),
        )


class TeslaSpend:
    """The month's count, the levels, and the planner's refreshes of the Tesla."""

    def __init__(
        self,
        hass: HomeAssistant,
        settings: Mapping[str, Any],
        cars: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.hass = hass
        self.settings = settings  # the coordinator's own dict: changes show at once
        self.cars = cars
        self.counts = SpendCounts("")
        self.last_month: SpendCounts | None = None
        self.refreshed: dict[str, datetime] = {}  # car id -> the planner's last refresh
        self.wall_prev: bool | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ storage

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts.to_dict(),
            "last_month": self.last_month.to_dict() if self.last_month else None,
            "refreshed": {car_id: t.isoformat() for car_id, t in self.refreshed.items()},
        }

    def load(self, data: Mapping[str, Any]) -> None:
        self.counts = SpendCounts.from_dict(data.get("counts") or {})
        last = data.get("last_month")
        self.last_month = SpendCounts.from_dict(last) if isinstance(last, Mapping) else None
        self.refreshed = {}
        for car_id, value in (data.get("refreshed") or {}).items():
            try:
                self.refreshed[car_id] = datetime.fromisoformat(value)
            except TypeError, ValueError:
                continue

    def roll(self, now: datetime) -> None:
        """A new month starts from zero; the last one is kept for the dashboard."""
        key = month_key(now)
        if self.counts.month >= key:
            return  # same month, or a clock set back: keep counting (never lose the count)
        if self.counts.month:
            self.last_month = self.counts
        self.counts = SpendCounts(key)

    # ------------------------------------------------------------------ the cap

    @property
    def teslas(self) -> dict[str, Mapping[str, Any]]:
        return {cid: car for cid, car in self.cars.items() if car.get(CONF_KIND) == KIND_TESLA}

    def polling(self, car: Mapping[str, Any]) -> bool | None:
        """Whether Tesla Fleet polls this car (its "Enable polling for updates" system
        option), None when its battery level has no config entry to tell."""
        entry = er.async_get(self.hass).async_get(car[CONF_SOC])
        if entry is None or entry.config_entry_id is None:
            return None
        config_entry = self.hass.config_entries.async_get_entry(entry.config_entry_id)
        if config_entry is None:
            return None
        return not config_entry.pref_disable_polling

    def polling_off(self) -> bool:
        """Tesla Fleet's polling is off for every Tesla."""
        teslas = self.teslas
        return bool(teslas) and all(self.polling(car) is False for car in teslas.values())

    @property
    def polling_estimate(self) -> float:
        if not self.teslas or self.polling_off():
            return 0.0
        return max(0.0, float(self.settings[S_TESLA_POLLING]))

    @property
    def budget(self) -> float:
        return max(0.0, float(self.settings[S_TESLA_BUDGET]))

    @property
    def cap(self) -> float:
        """What the planner's own calls may cost this month."""
        return max(0.0, min(self.budget, TESLA_CREDIT - TESLA_MARGIN - self.polling_estimate))

    def spent(self, now: datetime) -> float:
        self.roll(now)
        return self.counts.usd

    def level(self, now: datetime) -> str:
        spent, cap = self.spent(now), self.cap
        if spent + TESLA_WORST_SEND > cap:
            return LEVEL_STOPPED
        if spent >= TESLA_ESSENTIAL_AT * cap:
            return LEVEL_ESSENTIAL
        return LEVEL_NORMAL

    def note(self, now: datetime) -> str | None:
        """One sentence for the reason, or None at the normal level."""
        level = self.level(now)
        if level == LEVEL_NORMAL:
            return None
        head = f"Tesla API spend ${self.counts.usd:.2f} of the planner's ${self.cap:.2f} this month"
        if level == LEVEL_ESSENTIAL:
            return (
                f"{head}: the Tesla is only started in the free hours and stopped by day; "
                "daytime starts and current changes wait."
            )
        return f"{head}: nothing is sent to the Tesla until {next_month(now):%B} 1."

    # ------------------------------------------------------------------ sends

    def gate(
        self, target: int, charging: bool | None, amps: float | None, now: datetime, free: bool
    ) -> tuple[int, str | None]:
        """What a Tesla may be sent for ``target`` A (0 = a stop instead), and why it waits
        (None: send). ``charging`` and ``amps`` are the car's standing request; ``free``: in
        the free hours."""
        level = self.level(now)
        if level == LEVEL_STOPPED and target == 0 and charging is True:
            # a charging car is awake, so a stop needs no wake and costs one command:
            # paid-hours charging would cost far more than the API call
            return target, None
        if level == LEVEL_STOPPED:
            return target, (
                f"the Tesla API budget is used up (${self.counts.usd:.2f} of "
                f"${self.cap:.2f}); nothing is sent until {next_month(now):%B} 1"
            )
        if level == LEVEL_ESSENTIAL and target > 0 and not free:
            if charging is True and (amps is None or target < amps):
                return 0, None  # a decrease by day: stop rather than draw paid power
            return target, (
                f"the Tesla API budget is nearly used (${self.counts.usd:.2f} of "
                f"${self.cap:.2f}); daytime starts and increases wait"
            )
        return target, None

    def wake_expected(self, car: Mapping[str, Any]) -> bool:
        """Whether Tesla Fleet wakes the car before a command: its status does not read
        online (or is not mapped)."""
        eid = car.get(CONF_STATUS)
        state = self.hass.states.get(eid) if eid else None
        return state is None or state.state != STATE_ON

    def count_wake(self, now: datetime) -> None:
        self.roll(now)
        self.counts.wakes += 1
        self.counts.data += TESLA_WAKE_CHECKS

    def count_command(self, now: datetime) -> None:
        self.roll(now)
        self.counts.commands += 1

    # ------------------------------------------------------------------ refreshes

    def _refreshes(self, car: Mapping[str, Any]) -> bool:
        """The planner refreshes this Tesla: a refresh interval is set and Tesla Fleet's
        own polling is off."""
        return float(self.settings[S_TESLA_REFRESH]) > 0 and self.polling(car) is False

    def stale_before_send(self, car: Mapping[str, Any], now: datetime) -> bool:
        """A command is about to go out and the Tesla's data is old."""
        if not self._refreshes(car):
            return False
        last = self.refreshed.get(car[CONF_CAR_ID])
        return last is None or now - last >= TESLA_FRESH_BEFORE_SEND

    async def async_refresh(self, car: Mapping[str, Any], now: datetime) -> bool:
        """Have Tesla Fleet read the car once. Counted before it goes out (billed even when
        it fails); never once the budget is used up."""
        if self.level(now) == LEVEL_STOPPED:
            return False
        self.counts.refreshes += 1
        self.counts.data += TESLA_REFRESH_DATA
        self.refreshed[car[CONF_CAR_ID]] = now
        try:
            async with asyncio.timeout(SERVICE_TIMEOUT):
                await self.hass.services.async_call(
                    "homeassistant",
                    "update_entity",
                    {ATTR_ENTITY_ID: car[CONF_SOC]},
                    blocking=True,
                )
        except (HomeAssistantError, TimeoutError) as err:
            self.last_error = f"{type(err).__name__}: {err}"[:200]
            _LOGGER.warning("Refreshing %s failed: %s", car.get("name"), self.last_error)
            return False
        self.last_error = None
        return True

    async def async_watch(
        self, now: datetime, wall: bool | None, sessions: Mapping[str, bool]
    ) -> None:
        """The refreshes on the planner's own timer, at the start of a cycle. ``wall``: the
        Wall Connector's vehicle-connected reading; ``sessions``: car id -> the car was on
        the charger at the last cycle."""
        prev = self.wall_prev
        if wall is not None:
            self.wall_prev = wall
        level = self.level(now)
        if level == LEVEL_STOPPED:
            return
        interval = timedelta(minutes=float(self.settings[S_TESLA_REFRESH]))
        for car_id, car in self.teslas.items():
            if not self._refreshes(car):
                continue
            last = self.refreshed.get(car_id)
            if wall is True and prev is not True:
                due = True  # a car was connected (or is, at start-up): is it this one?
            elif wall is False and prev is True and sessions.get(car_id):
                due = True  # this car left: its plug reading changes
            elif wall is True and sessions.get(car_id) and level == LEVEL_NORMAL:
                due = last is None or now - last >= interval
            else:
                due = False
            if due:
                await self.async_refresh(car, now)

    # ------------------------------------------------------------------ the dashboard

    def attributes(self, now: datetime) -> dict[str, Any]:
        polling = {
            car_id: {True: "on", False: "off", None: "unknown"}[self.polling(car)]
            for car_id, car in self.teslas.items()
        }
        spent, cap = self.spent(now), self.cap
        return {
            "month": self.counts.month,
            "level": self.level(now),
            "commands": self.counts.commands,
            "wakes": self.counts.wakes,
            "data_requests": self.counts.data,
            "refreshes": self.counts.refreshes,
            "budget": round(self.budget, 2),
            "polling_estimate": round(self.polling_estimate, 2),
            "tesla_fleet_polling": polling,
            "cap": round(cap, 2),
            "essential_only_from": round(TESLA_ESSENTIAL_AT * cap, 2),
            "stops_at": round(max(0.0, cap - TESLA_WORST_SEND), 2),
            "left": round(max(0.0, cap - spent), 3),
            "credit": TESLA_CREDIT,
            "wakes_counted": {
                car_id: "from status" if car.get(CONF_STATUS) else "every send"
                for car_id, car in self.teslas.items()
            },
            "last_refresh": {cid: t.isoformat() for cid, t in self.refreshed.items()},
            "last_refresh_error": self.last_error,
            "last_month": round(self.last_month.usd, 3) if self.last_month else None,
            "last_month_counts": self.last_month.to_dict() if self.last_month else None,
        }
