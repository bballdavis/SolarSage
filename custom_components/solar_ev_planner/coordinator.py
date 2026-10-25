"""The planner's coordinator: reads the states every minute, decides, and commands.

Each cycle:

1. reads the Sol-Ark, the Wall Connector, Solcast, the tariff schedules and the cars;
2. keeps 5-minute means of the four powers (house = pv + grid - battery - wall connector);
3. decides which car is on the Wall Connector (``inputs.identify_car``);
4. builds the core's ``Snapshot`` and calls ``Planner.decide`` in an executor job; the core
   re-plans the daytime current every quarter hour and runs its safety rules every call;
5. unless shadow mode is on or the planner is off, sends the command for the car on the
   charger (``executor``), then saves what it commanded and the planner's memory.

The load profile is rebuilt from the recorder at start-up, at every date change and every
few hours, off the event loop. Nothing is commanded while the site's readings are missing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    C_LIMIT,
    C_PLAN_DAYS,
    C_PLAN_ONCE_AT,
    C_PLAN_TARGET,
    C_PLAN_TIME,
    C_RESTORE,
    CAUTION_OPTIONS,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_RESERVE_PCT,
    CONF_BATTERY_SOC,
    CONF_CAPACITY_KWH,
    CONF_CAR_ID,
    CONF_CHARGER_MAX_A,
    CONF_CURRENT,
    CONF_EXPORT_PRICE,
    CONF_FORECAST_POLLED,
    CONF_FORECAST_TODAY,
    CONF_FORECAST_TOMORROW,
    CONF_FREE_HOURS,
    CONF_KIND,
    CONF_LABEL,
    CONF_LIMIT,
    CONF_LOCATION,
    CONF_MAX_A,
    CONF_MIN_A,
    CONF_NAME,
    CONF_PLUGGED,
    CONF_PRICE,
    CONF_SCHEDULE,
    CONF_SOC,
    CONF_STEP_A,
    CONF_SWITCH,
    CONF_VOLTS,
    CONF_WALL_CONNECTED,
    CONF_WALL_CONTACTOR,
    CONF_WALL_CURRENT,
    DEFAULT_POWER_UNITS,
    DEFAULT_SETTINGS,
    DEFAULT_SITE_OPTIONS,
    DOMAIN,
    HEARTBEAT_TIMEOUT,
    HISTORY_REFRESH,
    KIND_TESLA,
    MODE_NO_DATA,
    OVERRIDE_AUTO,
    PENDING_AFTER_SEND,
    PLAN_DAYS,
    PLAN_OFF,
    PLAN_ONCE,
    POWER_KEYS,
    PV_DAYS_KEPT,
    READ_INTERVAL,
    S_BUYBACK,
    S_CAUTION,
    S_DAYTIME_SOLAR,
    S_ENABLED,
    S_FLOOR,
    S_MAX_STEP,
    S_MIN_CHANGE,
    S_OVERRIDE,
    S_PAID_TOLERANCE,
    S_PRICE,
    S_SHADOW,
    S_WANTED,
    SCHEDULE_CONTROL_WRITING,
    SCHEDULE_REFRESH,
    SITE_QUIET_AFTER,
    STORAGE_VERSION,
    STORE_SAVE_DELAY,
    SUBENTRY_CAR,
    SUBENTRY_PRICE,
    default_car_settings,
    invert_key,
    unit_key,
)
from .core import (
    BatterySpec,
    CarOutlook,
    CarPlan,
    CarSpec,
    CarState,
    Decision,
    ForecastSlot,
    Measured,
    Planner,
    PvDay,
    RepeatingPlan,
    Settings,
    Snapshot,
    forecast_energy,
    parse_detailed_forecast,
)
from .core.pv import PvModel
from .executor import CarExecutor, CarRuntime, Standing, standing_request
from .history import History, PowerBuffer, async_load_history, empty_history
from .inputs import (
    CarSignals,
    Identification,
    WindowSpec,
    available,
    build_tariff,
    identify_car,
    integration_quiet,
    last_report,
    normalize_schedule,
    power_kw,
    state_float,
    state_home,
    state_on,
)

_LOGGER = logging.getLogger(__name__)

HISTORY_RETRY = timedelta(minutes=15)
# The core says the cars are left alone. Here the heartbeat goes down with the planner, so
# Rivian schedule control stops renewing and parks its window.
OFF_REASON = (
    "The planner is off and sends nothing. The Tesla keeps its last current; Rivian schedule "
    "control sees the heartbeat down and parks its charging window within a few minutes."
)
# Start-up waits this long (seconds) for the recorder; then the planner starts with a flat
# house profile and keeps trying in the background.
HISTORY_SETUP_WAIT = 30

type PlannerConfigEntry = ConfigEntry[PlannerCoordinator]


@dataclass
class SiteView:
    """The site's readings at this cycle (kW, %, A)."""

    pv_kw: float | None = None
    grid_kw: float | None = None
    battery_kw: float | None = None
    wall_kw: float | None = None
    soc_pct: float | None = None
    capacity_kwh: float | None = None
    wall_current_a: float | None = None
    wall_connected: bool | None = None
    contactor_closed: bool | None = None
    problem: str | None = None  # why the planner cannot decide

    @property
    def house_kw(self) -> float | None:
        parts = (self.pv_kw, self.grid_kw, self.battery_kw, self.wall_kw)
        if any(p is None for p in parts):
            return None
        pv, grid, battery, wall = parts  # type: ignore[misc]
        return max(0.0, pv + grid - battery - wall)


@dataclass
class CarView:
    """One car as the dashboard shows it."""

    car_id: str
    name: str
    kind: str
    soc_pct: float | None
    limit_pct: float | None
    plugged: bool | None  # the car's own plug sensor
    home: bool | None
    on_charger: bool
    stale: bool
    planned_a: int = 0  # the current the plan has the car at (shadow mode: the virtual car)
    want_a: int | None = None  # the planned current before the rate limits
    command: int | None = None  # what this cycle commands (None = nothing)
    reason: str = ""
    outlook: CarOutlook | None = None
    charging_wanted: bool | None = None
    commanded_a: int | None = None
    standing: Standing | None = None
    blocked: str | None = None
    last_error: str | None = None


@dataclass
class PlannerData:
    """The result of one cycle."""

    now: datetime
    mode: str
    reason: str
    decision: Decision | None
    site: SiteView
    identification: Identification
    cars: dict[str, CarView] = field(default_factory=dict)
    pv: dict[str, Any] = field(default_factory=dict)
    active: bool = False  # commands are sent
    forecast_slots: int = 0


def next_time(now: datetime, hhmm: str) -> datetime:
    """The next local time HH:MM after now."""
    h, m = (int(x) for x in hhmm.split(":")[:2])
    t = now.replace(hour=h % 24, minute=m, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


class PlannerCoordinator(DataUpdateCoordinator[PlannerData]):
    """Reads, decides and commands every minute; keeps the planner's memory and settings."""

    config_entry: PlannerConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PlannerConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=READ_INTERVAL
        )
        self.options: dict[str, Any] = DEFAULT_SITE_OPTIONS | dict(entry.options)
        self.cars: dict[str, dict[str, Any]] = {}
        self.windows: dict[str, dict[str, Any]] = {}
        for subentry_id, sub in entry.subentries.items():
            if sub.subentry_type == SUBENTRY_CAR:
                self.cars[sub.data[CONF_CAR_ID]] = dict(sub.data) | {"subentry_id": subentry_id}
            elif sub.subentry_type == SUBENTRY_PRICE:
                self.windows[subentry_id] = dict(sub.data)
        self.planner = Planner()
        self.executor = CarExecutor(hass)
        self.runtime: dict[str, CarRuntime] = {car_id: CarRuntime() for car_id in self.cars}
        self.settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.car_settings: dict[str, dict[str, Any]] = {
            car_id: default_car_settings(car[CONF_KIND], int(car[CONF_MAX_A]))
            for car_id, car in self.cars.items()
        }
        self.window_prices: dict[str, float] = {
            sid: float(w[CONF_PRICE]) for sid, w in self.windows.items()
        }
        self.buffer = PowerBuffer()
        self.history: History = empty_history(dt_util.now().date())
        self.history_error: str | None = None
        self.schedules: dict[str, dict[str, Any]] = {}
        self.schedules_at: datetime | None = None
        self.pv_days: list[PvDay] = []
        self.day: dict[str, Any] = {}
        self.forecast: list[ForecastSlot] = []
        self.last_success: datetime | None = None
        self.ok = False
        self.last_error: str | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._lock = asyncio.Lock()
        self._history_task: asyncio.Task[None] | None = None
        self._history_tried: datetime | None = None

    # ------------------------------------------------------------------ setup and storage

    async def _async_setup(self) -> None:
        """Restore the memory and settings, read the schedules and the history."""
        stored = await self._store.async_load() or {}
        self._load(stored)
        now = dt_util.now()
        await self._async_refresh_schedules(now)
        try:
            async with asyncio.timeout(HISTORY_SETUP_WAIT):
                await self._async_refresh_history(now)
        except Exception as err:  # noqa: BLE001 - the planner starts with a flat profile
            self.history_error = f"{type(err).__name__}: {err}"
            _LOGGER.warning("House history is not available yet: %s", self.history_error)

    def _load(self, stored: Mapping[str, Any]) -> None:
        self.settings.update(
            {k: v for k, v in (stored.get("settings") or {}).items() if k in DEFAULT_SETTINGS}
        )
        for car_id, values in (stored.get("cars") or {}).items():
            if car_id in self.car_settings:
                self.car_settings[car_id].update(
                    {k: v for k, v in values.items() if k in self.car_settings[car_id]}
                )
        for sid, price in (stored.get("window_prices") or {}).items():
            if sid in self.window_prices:
                self.window_prices[sid] = float(price)
        for car_id, data in (stored.get("runtime") or {}).items():
            if car_id in self.runtime:
                self.runtime[car_id] = CarRuntime.from_dict(data)
        if planner_state := stored.get("planner"):
            try:
                self.planner.load_state(planner_state)
            except Exception:  # noqa: BLE001 - a fresh memory is always safe
                _LOGGER.warning("Could not restore the planner's memory; starting fresh")
        self.pv_days = []
        for d in stored.get("pv_days") or []:
            try:
                self.pv_days.append(
                    PvDay(
                        date.fromisoformat(d["day"]),
                        float(d["actual_kwh"]),
                        float(d["forecast_kwh"]),
                    )
                )
            except KeyError, TypeError, ValueError:
                continue
        self.day = dict(stored.get("day") or {})
        self.schedules = {k: dict(v) for k, v in (stored.get("schedules") or {}).items()}
        if self.override not in (OVERRIDE_AUTO, *self.cars):
            self.settings[S_OVERRIDE] = OVERRIDE_AUTO

    def _store_data(self) -> dict[str, Any]:
        return {
            "settings": self.settings,
            "cars": self.car_settings,
            "window_prices": self.window_prices,
            "runtime": {car_id: rt.to_dict() for car_id, rt in self.runtime.items()},
            "planner": self.planner.state_dict(),
            "pv_days": [
                {
                    "day": d.day.isoformat(),
                    "actual_kwh": d.actual_kwh,
                    "forecast_kwh": d.forecast_kwh,
                }
                for d in self.pv_days
            ],
            "day": self.day,
            "schedules": self.schedules,
        }

    def save(self) -> None:
        """Save soon (the store batches writes)."""
        self._store.async_delay_save(self._store_data, STORE_SAVE_DELAY)

    async def async_shutdown(self) -> None:
        """Stop and save at once."""
        await super().async_shutdown()
        if self._history_task is not None:
            self._history_task.cancel()
        await self._store.async_save(self._store_data())

    # ------------------------------------------------------------------ settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings[S_ENABLED])

    @property
    def shadow(self) -> bool:
        return bool(self.settings[S_SHADOW])

    @property
    def active(self) -> bool:
        """Whether commands are sent: the planner is on and shadow mode is off."""
        return self.enabled and not self.shadow

    @property
    def override(self) -> str:
        return str(self.settings.get(S_OVERRIDE, OVERRIDE_AUTO))

    async def async_set_setting(self, key: str, value: Any) -> None:
        """Change an owner setting, save it and plan again."""
        if self.settings.get(key) == value:
            return
        async with self._lock:
            self.settings[key] = value
            if key in (S_ENABLED, S_SHADOW):
                # Commands start or stop: a fresh session, so the first decision is sent at
                # once and the rate limiter does not count the shadow plan's changes.
                self.planner.load_state({})
        self.save()
        self.async_update_listeners()
        await self.async_request_refresh()

    async def async_set_car_setting(self, car_id: str, key: str, value: Any) -> None:
        """Change a per-car setting."""
        settings = self.car_settings[car_id]
        if settings.get(key) == value:
            return
        settings[key] = value
        if key in (C_PLAN_DAYS, C_PLAN_TIME):
            settings[C_PLAN_ONCE_AT] = None  # a one-off plan is for the next such time
        self.save()
        self.async_update_listeners()
        await self.async_request_refresh()

    async def async_set_window_price(self, subentry_id: str, value: float) -> None:
        """Change a price window's import price."""
        self.window_prices[subentry_id] = value
        self.save()
        self.async_update_listeners()
        await self.async_request_refresh()

    def core_settings(self) -> Settings:
        """The owner settings as the core takes them."""
        s = self.settings
        caution = CAUTION_OPTIONS.get(str(s[S_CAUTION]), "conservative")
        return Settings(
            enabled=bool(s[S_ENABLED]),
            shadow=bool(s[S_SHADOW]),
            daytime_solar=bool(s[S_DAYTIME_SOLAR]),
            caution=caution,
            floor_pct=float(s[S_FLOOR]),
            wanted_at_free_start_pct=float(s[S_WANTED]),
            min_change_min=float(s[S_MIN_CHANGE]),
            max_step_a=int(s[S_MAX_STEP]),
            paid_tolerance_min=float(s[S_PAID_TOLERANCE]),
        )

    # ------------------------------------------------------------------ health

    def healthy(self, now: datetime | None = None) -> bool:
        """The heartbeat: on, deciding, and the last decision is recent."""
        now = now or dt_util.now()
        return (
            self.enabled
            and self.ok
            and self.last_success is not None
            and now - self.last_success < HEARTBEAT_TIMEOUT
        )

    def charging_wanted(self, car_id: str) -> bool | None:
        """Whether the planner wants this car charging; None while it sends nothing (shadow
        mode, off) or has not commanded the car in this session."""
        rt = self.runtime.get(car_id)
        if not self.active or rt is None or rt.plugged_since is None or rt.commanded_a is None:
            return None
        return rt.commanded_a > 0

    # ------------------------------------------------------------------ refreshes

    async def _async_refresh_history(self, now: datetime) -> None:
        self._history_tried = now
        self.history = await async_load_history(self.hass, self.options, now)
        self.buffer.load(self.history)
        self.history_error = None
        if self.history.missing:
            _LOGGER.info("No recorder statistics yet for %s", ", ".join(self.history.missing))

    async def _async_history_job(self, now: datetime) -> None:
        try:
            await self._async_refresh_history(now)
        except Exception as err:  # noqa: BLE001 - keep the last profile
            self.history_error = f"{type(err).__name__}: {err}"
            _LOGGER.warning("Could not read the house history: %s", self.history_error)
        finally:
            self._history_task = None

    def _schedule_history(self, now: datetime) -> None:
        """Rebuild the profile at a date change and every few hours, in the background; a
        failed read is retried after a quarter of an hour."""
        fetched, tried = self.history.fetched_at, self._history_tried
        due = fetched is None or fetched.date() != now.date() or now - fetched >= HISTORY_REFRESH
        retry = tried is None or tried.date() != now.date() or now - tried >= HISTORY_RETRY
        if due and retry and self._history_task is None:
            self._history_tried = now
            self._history_task = self.config_entry.async_create_background_task(
                self.hass, self._async_history_job(now), f"{DOMAIN} history"
            )

    def _schedule_ids(self) -> list[str]:
        ids = [self.options[CONF_FREE_HOURS]] if self.options.get(CONF_FREE_HOURS) else []
        ids += [w[CONF_SCHEDULE] for w in self.windows.values()]
        return ids

    async def _async_refresh_schedules(self, now: datetime) -> None:
        """Read the Schedule helpers' weekly configs; keep the last good one of each."""
        self.schedules_at = now
        for entity_id in self._schedule_ids():
            if self.hass.states.get(entity_id) is None:
                continue
            try:
                response = await self.hass.services.async_call(
                    "schedule",
                    "get_schedule",
                    {"entity_id": entity_id},
                    blocking=True,
                    return_response=True,
                )
            except HomeAssistantError as err:
                _LOGGER.debug("Could not read the schedule %s: %s", entity_id, err)
                continue
            config = (response or {}).get(entity_id)
            if isinstance(config, Mapping):
                self.schedules[entity_id] = normalize_schedule(config)

    # ------------------------------------------------------------------ the cycle

    async def _async_update_data(self) -> PlannerData:
        now = dt_util.now()
        try:
            async with self._lock:
                return await self._async_cycle(now)
        except Exception as err:
            self.ok = False
            self.last_error = f"{type(err).__name__}: {err}"
            _LOGGER.exception("The planning cycle failed")
            raise UpdateFailed(self.last_error) from err

    def _state(self, entity_id: str | None) -> State | None:
        return self.hass.states.get(entity_id) if entity_id else None

    def _home(self, car: Mapping[str, Any]) -> bool | None:
        """Whether the car is home; True when no location entity is mapped."""
        if not car.get(CONF_LOCATION):
            return True
        return state_home(self._state(car[CONF_LOCATION]))

    async def _async_cycle(self, now: datetime) -> PlannerData:
        self.planner.settings = self.core_settings()
        site = self._read_site(now)
        self._roll_day(now)
        self._schedule_history(now)
        if self.schedules_at is None or now - self.schedules_at >= SCHEDULE_REFRESH:
            await self._async_refresh_schedules(now)
        slots, issued = self._read_forecast(now)
        pv_today = self.buffer.pv_today_kwh(now, lambda a, b: forecast_energy(slots, a, b))
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.day = {
            "date": now.date().isoformat(),
            "pv_kwh": round(pv_today, 3),
            "fc_kwh": round(forecast_energy(slots, midnight, midnight + timedelta(days=1)), 3),
        }
        ident, cars = self._read_cars(now)
        await self._async_restore_at_unplug()
        tariff, tariff_problem = self._tariff()
        pv = self._pv_info(slots, now, pv_today, issued)
        problem = site.problem or tariff_problem
        decision: Decision | None = None
        if problem is None:
            assert site.soc_pct is not None and site.capacity_kwh is not None
            powers = self.buffer.mean(now, 5)
            measured = None
            if powers is not None:
                measured = Measured(*powers, soc_pct=site.soc_pct)
            recent = self.buffer.mean(now, 15)
            o = self.options
            snap = Snapshot(
                now=now,
                soc_pct=site.soc_pct,
                battery=BatterySpec(
                    capacity_kwh=site.capacity_kwh,
                    max_charge_kw=float(o[CONF_BATTERY_MAX_CHARGE_KW]),
                    max_discharge_kw=float(o[CONF_BATTERY_MAX_DISCHARGE_KW]),
                    reserve_pct=float(o[CONF_BATTERY_RESERVE_PCT]),
                ),
                tariff=tariff,
                forecast=slots,
                load=self.history.profile,
                house_recent=self.buffer.house_recent(now),
                pv_today_kwh=pv_today,
                pv_recent_kw=recent[0] if recent else 0.0,
                measured=measured,
                cars=tuple(cars.values()),
                forecast_issued_at=issued,
                pv_days=tuple(self.pv_days),
                volts=float(o[CONF_VOLTS]),
                charger_max_a=int(o[CONF_CHARGER_MAX_A]),
            )
            decision = await self.hass.async_add_executor_job(self.planner.decide, snap)
            if self.active:
                await self._async_execute(decision, now)
        self.ok = decision is not None
        if decision is not None:
            self.last_success = now
            self.last_error = None
        self.save()
        return PlannerData(
            now=now,
            mode=decision.mode if decision else MODE_NO_DATA,
            reason=(
                (OFF_REASON if decision.mode == "off" else decision.reason)
                if decision
                else f"Not planning: {problem}"
            ),
            decision=decision,
            site=site,
            identification=ident,
            cars=self._views(cars, decision, ident),
            pv=pv,
            active=self.active,
            forecast_slots=len(slots),
        )

    # ------------------------------------------------------------------ reading

    def _read_site(self, now: datetime) -> SiteView:
        o = self.options
        states = {key: self._state(o.get(key)) for key in POWER_KEYS}
        kw = {
            key: power_kw(
                states[key],
                o.get(unit_key(key), DEFAULT_POWER_UNITS[key]),
                bool(o.get(invert_key(key))),
            )
            for key in POWER_KEYS
        }
        soc_state = self._state(o.get(CONF_BATTERY_SOC))
        capacity = None
        if o.get(CONF_BATTERY_CAPACITY):
            capacity = state_float(self._state(o[CONF_BATTERY_CAPACITY]))
        if capacity is None or capacity <= 1.0:
            capacity = float(o[CONF_BATTERY_CAPACITY_KWH])
        site = SiteView(
            pv_kw=kw[POWER_KEYS[0]],
            grid_kw=kw[POWER_KEYS[1]],
            battery_kw=kw[POWER_KEYS[2]],
            wall_kw=kw[POWER_KEYS[3]],
            soc_pct=state_float(soc_state),
            capacity_kwh=capacity,
            wall_current_a=state_float(self._state(o.get(CONF_WALL_CURRENT))),
            wall_connected=state_on(self._state(o.get(CONF_WALL_CONNECTED))),
            contactor_closed=state_on(self._state(o.get(CONF_WALL_CONTACTOR))),
        )
        missing = [key for key in POWER_KEYS if kw[key] is None]
        if site.soc_pct is None:
            missing.append(CONF_BATTERY_SOC)
        watched = [*states.values(), soc_state]
        reported = last_report(watched)
        if missing:
            site.problem = f"no reading from {', '.join(missing)}."
        elif any(integration_quiet(self.hass, o.get(k)) for k in (*POWER_KEYS, CONF_BATTERY_SOC)):
            site.problem = "the integration behind the site's sensors is not running."
        elif reported is not None and now - reported > SITE_QUIET_AFTER:
            site.problem = (
                f"the site's sensors have not reported since {dt_util.as_local(reported):%H:%M}."
            )
        else:
            self.buffer.add(now, (site.pv_kw, site.grid_kw, site.battery_kw, site.wall_kw))  # type: ignore[arg-type]
        return site

    def _read_forecast(self, now: datetime) -> tuple[list[ForecastSlot], datetime | None]:
        """Today's and tomorrow's Solcast half hours (the last good ones while Solcast is
        unavailable) and when Solcast was last polled."""
        attrs = []
        for key in (CONF_FORECAST_TODAY, CONF_FORECAST_TOMORROW):
            state = self._state(self.options.get(key))
            if state is not None and state.attributes.get("detailedForecast"):
                attrs.append(state.attributes)
        slots: list[ForecastSlot] = []
        if attrs:
            try:
                slots = parse_detailed_forecast(
                    *attrs, tz=dt_util.get_default_time_zone(), naive=False
                )
            except TypeError, ValueError, KeyError:
                _LOGGER.warning("Could not read the Solcast forecast")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if slots and slots[0].start <= midnight + timedelta(hours=1):
            self.forecast = [s for s in slots if s.start >= midnight]
        elif self.forecast and self.forecast[0].start.date() != now.date():
            self.forecast = []  # yesterday's forecast does not describe today
        issued = None
        polled = self._state(self.options.get(CONF_FORECAST_POLLED))
        if available(polled):
            assert polled is not None
            parsed = dt_util.parse_datetime(polled.state)
            if parsed is not None:
                issued = dt_util.as_local(parsed)
        return list(self.forecast), issued

    def _read_cars(self, now: datetime) -> tuple[Identification, dict[str, CarState]]:
        signals = []
        for car_id, car in self.cars.items():
            home = (
                True if not car.get(CONF_LOCATION) else state_home(self._state(car[CONF_LOCATION]))
            )
            signals.append(CarSignals(car_id, state_on(self._state(car[CONF_PLUGGED])), home))
        override = self.override if self.override in self.cars else OVERRIDE_AUTO
        wall = state_on(self._state(self.options.get(CONF_WALL_CONNECTED)))
        ident = identify_car(signals, wall, override)
        states: dict[str, CarState] = {}
        for signal in signals:
            car_id, car = signal.car_id, self.cars[signal.car_id]
            rt = self.runtime[car_id]
            on = ident.car_id == car_id
            if on and rt.plugged_since is None:
                rt.end_session()
                rt.plugged_since = now
            elif not on and rt.plugged_since is not None:
                # A car whose own readings are unknown (its integration is starting, or a
                # cloud hiccup) keeps its session: it is not commanded meanwhile, but what it
                # was commanded, and the charging-wanted sensor, stand until it is known.
                unknown = signal.plugged is None or signal.home is None
                if not (unknown and wall is not False and ident.car_id is None):
                    rt.end_session()
            states[car_id] = self._car_state(car_id, car, on, now)
        return ident, states

    def _car_state(self, car_id: str, car: Mapping[str, Any], on: bool, now: datetime) -> CarState:
        rt = self.runtime[car_id]
        cs = self.car_settings[car_id]
        soc = state_float(self._state(car[CONF_SOC]))
        limit = state_float(self._state(car[CONF_LIMIT]))
        if limit is not None and float(cs[C_LIMIT]) > 0:
            limit = min(limit, float(cs[C_LIMIT]))
        current = self._state(car[CONF_CURRENT])
        switch = self._state(car[CONF_SWITCH])
        max_a = int(car[CONF_MAX_A])
        top = current.attributes.get("max") if current is not None else None
        if isinstance(top, (int, float)) and top >= int(car[CONF_MIN_A]):
            max_a = min(max_a, int(top))
        spec = CarSpec(
            car[CONF_NAME],
            float(car[CONF_CAPACITY_KWH]),
            int(car[CONF_MIN_A]),
            max_a,
            int(car[CONF_STEP_A]),
            billed_commands=car[CONF_KIND] == KIND_TESLA,
        )
        stale = (
            soc is None
            or limit is None
            or not available(switch)
            or not available(current)
            or integration_quiet(self.hass, car[CONF_SOC])
            or integration_quiet(self.hass, car[CONF_SWITCH])
        )
        pending = rt.last_sent is not None and now - rt.last_sent < PENDING_AFTER_SEND
        if on and not pending:
            pending = (
                standing_request(self.hass, car).schedule_control_state == SCHEDULE_CONTROL_WRITING
            )
        known = soc if soc is not None else (limit if limit is not None else 0.0)
        return CarState(
            car_id,
            spec,
            known,
            limit if limit is not None else known,
            on,
            rt.plugged_since if on else None,
            rt.commanded_a or 0,
            pending,
            stale,
            self._plans(car_id, now),
        )

    def _plans(self, car_id: str, now: datetime) -> tuple[CarPlan, ...]:
        cs = self.car_settings[car_id]
        days, target = cs[C_PLAN_DAYS], float(cs[C_PLAN_TARGET])
        if days not in PLAN_DAYS or days == PLAN_OFF or target <= 0:
            return ()
        if days == PLAN_ONCE:
            at = None
            if cs.get(C_PLAN_ONCE_AT):
                try:
                    at = datetime.fromisoformat(cs[C_PLAN_ONCE_AT])
                except TypeError, ValueError:
                    at = None
            if at is None:
                at = next_time(now, str(cs[C_PLAN_TIME]))
                cs[C_PLAN_ONCE_AT] = at.isoformat()
            if at <= now:  # done: the one-off plan turns itself off
                cs[C_PLAN_DAYS] = PLAN_OFF
                cs[C_PLAN_ONCE_AT] = None
                return ()
            return (CarPlan(target, at),)
        return RepeatingPlan(target, str(cs[C_PLAN_TIME]), PLAN_DAYS[days]).deadlines(now)

    def _tariff(self) -> tuple[Any, str | None]:
        problem = None
        free_config = None
        if free := self.options.get(CONF_FREE_HOURS):
            free_config = self.schedules.get(free)
            if free_config is None:
                problem = f"the free-hours schedule {free} could not be read."
        windows = []
        for sid, w in self.windows.items():
            config = self.schedules.get(w[CONF_SCHEDULE])
            if config is None:
                problem = problem or f"the price schedule {w[CONF_SCHEDULE]} could not be read."
                continue
            export = w.get(CONF_EXPORT_PRICE)
            windows.append(
                WindowSpec(
                    config,
                    self.window_prices.get(sid, float(w[CONF_PRICE])),
                    str(w.get(CONF_LABEL) or ""),
                    float(export) if export is not None else None,
                )
            )
        tariff = build_tariff(
            float(self.settings[S_PRICE]), float(self.settings[S_BUYBACK]), free_config, windows
        )
        return tariff, problem

    def _pv_info(
        self, slots: list[ForecastSlot], now: datetime, pv_today: float, issued: datetime | None
    ) -> dict[str, Any]:
        """The forecast correction from today's production (for the dashboard)."""
        if not slots:
            return {}
        model = PvModel(slots, now, pv_today, self.pv_days, issued, self.planner.params.pv)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        expected = 0.0
        t = now
        while t < midnight:
            step = min(timedelta(minutes=15), midnight - t)
            expected += model.mean_sigma(t + step / 2)[0] * step.total_seconds() / 3600.0
            t += step
        return {
            "ratio": round(model.ratio, 3),
            "evidence_weight": round(model.k, 2),
            "seen": round(model.seen, 2),
            "multi_day_bias": round(model.b0, 3),
            "forecast_age_h": round(model.age_h, 1),
            "pv_today_kwh": round(pv_today, 2),
            "remaining_p10_kwh": round(forecast_energy(slots, now, midnight, "p10"), 2),
            "remaining_p50_kwh": round(forecast_energy(slots, now, midnight, "p50"), 2),
            "remaining_p90_kwh": round(forecast_energy(slots, now, midnight, "p90"), 2),
            "remaining_expected_kwh": round(expected, 2),
        }

    def _roll_day(self, now: datetime) -> None:
        """At the first cycle of a new day, keep yesterday's solar against its forecast."""
        last = self.day.get("date")
        if not last or last == now.date().isoformat():
            return
        try:
            day = date.fromisoformat(last)
        except ValueError:
            return
        fc = float(self.day.get("fc_kwh") or 0.0)
        if fc > 0 and all(d.day != day for d in self.pv_days):
            self.pv_days.append(PvDay(day, float(self.day.get("pv_kwh") or 0.0), fc))
            self.pv_days = self.pv_days[-PV_DAYS_KEPT:]

    # ------------------------------------------------------------------ commanding

    async def _async_execute(self, decision: Decision, now: datetime) -> None:
        """Send the command for the car on the charger (never to any other car)."""
        car_id = decision.car_id
        if car_id is None or car_id not in self.cars:
            return
        command = decision.command_for(car_id)
        car, rt = self.cars[car_id], self.runtime[car_id]
        if command is not None and command.amps is not None:
            await self.executor.async_apply(car, rt, command.amps, now)
        elif decision.mode in ("day", "night"):
            await self.executor.async_reconcile(car, rt, now)

    async def _async_restore_at_unplug(self) -> None:
        """The Tesla keeps the last current it was given; when it unplugs at home after the
        planner set it, set the owner's current back (a setting; 0 turns it off)."""
        for car_id, car in self.cars.items():
            rt = self.runtime[car_id]
            plugged = state_on(self._state(car[CONF_PLUGGED]))
            if car[CONF_KIND] == KIND_TESLA:
                home = (
                    True
                    if not car.get(CONF_LOCATION)
                    else state_home(self._state(car[CONF_LOCATION]))
                )
                restore = int(self.car_settings[car_id][C_RESTORE])
                if (
                    rt.plugged_prev is True
                    and plugged is False
                    and home is True
                    and rt.touched
                    and self.active
                    and restore > 0
                    and await self.executor.async_restore(car, restore)
                ):
                    rt.touched = False
            if plugged is not None:
                rt.plugged_prev = plugged

    # ------------------------------------------------------------------ views

    def _views(
        self, cars: Mapping[str, CarState], decision: Decision | None, ident: Identification
    ) -> dict[str, CarView]:
        views = {}
        for car_id, state in cars.items():
            car, rt = self.cars[car_id], self.runtime[car_id]
            command = decision.command_for(car_id) if decision else None
            on = ident.car_id == car_id
            planned = 0
            if on:
                if command is not None and command.amps is not None:
                    planned = command.amps
                elif self.shadow:
                    planned = self.planner.shadow_a or 0
                else:
                    planned = rt.commanded_a or 0
            outlook = None
            if decision is not None:
                outlook = next((o for o in decision.outlooks if o.car_id == car_id), None)
            views[car_id] = CarView(
                car_id=car_id,
                name=str(car[CONF_NAME]),
                kind=str(car[CONF_KIND]),
                soc_pct=state_float(self._state(car[CONF_SOC])),
                limit_pct=state.limit_pct if not state.stale else None,
                plugged=state_on(self._state(car[CONF_PLUGGED])),
                home=self._home(car),
                on_charger=on,
                stale=state.stale,
                planned_a=int(planned),
                want_a=decision.debug.get("want_a") if decision and on else None,
                command=command.amps if command else None,
                reason=command.reason if command else "",
                outlook=outlook,
                charging_wanted=self.charging_wanted(car_id),
                commanded_a=rt.commanded_a,
                standing=standing_request(self.hass, car) if on else None,
                blocked=rt.blocked,
                last_error=rt.last_error,
            )
        return views
