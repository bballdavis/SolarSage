"""Glue between Home Assistant and the schedule controller.

The runtime reads the vehicle facts the controller needs from Home Assistant
entities, ticks the controller, and tells the adapter's entities to update.

Requests from evcc (the switch and number entities) only record the request in
the journal and schedule a tick; they never wait for a Rivian round trip.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import asdict
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import AuthError, ScheduleApiError
from .const import (
    ACTIVE_CHARGER_STATES,
    AUTH_RETRY_INTERVAL,
    CONF_CHARGER_STATE_ENTITY,
    CONF_CLOCK_SKEW_MINUTES,
    CONF_DRY_RUN,
    CONF_INCREASE_INTERVAL,
    CONF_LEASE_MINUTES,
    CONF_LOCATION_ENTITY,
    CONF_MIN_WRITE_INTERVAL,
    CONF_PLUGGED_ENTITY,
    CONF_RELEASE_DELAY,
    CONF_RENEW_BEFORE_MINUTES,
    CONF_VEHICLE_ID,
    CONF_WALL_CONNECTOR_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
    ENTITY_OPTIONS,
    EVCC_STATUS_CHARGING,
    EVCC_STATUS_CONNECTED,
    EVCC_STATUS_DISCONNECTED,
    STOP_TIMEOUT,
    TICK_INTERVAL,
)
from .controller import AdapterConfig, Observation, RivianScheduleController

_LOGGER = logging.getLogger(__name__)

_UNKNOWN_STATES = frozenset({STATE_UNKNOWN, STATE_UNAVAILABLE, ""})

type RivianSolarAdapterConfigEntry = ConfigEntry[AdapterRuntime]


def adapter_config_from_entry(hass: HomeAssistant, entry: ConfigEntry) -> AdapterConfig:
    """Build the controller tuning from the entry options and Home Assistant's home."""
    options = DEFAULT_OPTIONS | dict(entry.options)
    return AdapterConfig(
        home_location={
            "latitude": float(hass.config.latitude),
            "longitude": float(hass.config.longitude),
        },
        lease_minutes=int(options[CONF_LEASE_MINUTES]),
        renew_before_minutes=int(options[CONF_RENEW_BEFORE_MINUTES]),
        clock_skew_minutes=int(options[CONF_CLOCK_SKEW_MINUTES]),
        min_write_interval=timedelta(seconds=int(options[CONF_MIN_WRITE_INTERVAL])),
        increase_interval=timedelta(seconds=int(options[CONF_INCREASE_INTERVAL])),
        release_delay=timedelta(seconds=int(options[CONF_RELEASE_DELAY])),
        dry_run=bool(options[CONF_DRY_RUN]),
    )


def _known_state(hass: HomeAssistant, entity_id: str | None) -> str | None:
    """Return an entity's state, or None when it is missing or unknown."""
    if not entity_id or (state := hass.states.get(entity_id)) is None:
        return None
    return None if state.state in _UNKNOWN_STATES else state.state


class AdapterRuntime:
    """Owns the controller of one config entry and drives it from Home Assistant."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        controller: RivianScheduleController,
        *,
        sw_version: str | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.sw_version = sw_version
        self.vehicle_id: str = entry.data[CONF_VEHICLE_ID]
        self.last_provider_read: dict[str, Any] | None = None
        self.last_provider_read_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.reauth_required = False
        self._auth_retry_at: datetime | None = None
        self._reauth_started = False
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubscribe: list[CALLBACK_TYPE] = []
        self._tick_task: asyncio.Task[None] | None = None
        self._tick_requested = False
        self._tick_running = False
        self._stopped = False

    # Observation ---------------------------------------------------------

    def observation(self) -> Observation:
        """Read the vehicle facts from the mapped entities; unknown stays None."""
        options = self.entry.options
        plugged_state = _known_state(self.hass, options.get(CONF_PLUGGED_ENTITY))
        location_state = _known_state(self.hass, options.get(CONF_LOCATION_ENTITY))
        charging: bool | None = None
        if charger_entity := options.get(CONF_CHARGER_STATE_ENTITY):
            charger_state = _known_state(self.hass, charger_entity)
            if charger_state is not None:
                charging = charger_state in ACTIVE_CHARGER_STATES
        return Observation(
            plugged=None if plugged_state is None else plugged_state == STATE_ON,
            # "home" is home; not_home and any zone name are away.
            home=None if location_state is None else location_state == "home",
            charging=charging,
        )

    @property
    def evcc_status(self) -> str:
        """The vehicle status for evcc: a (not here), b (connected) or c (charging)."""
        obs = self.observation()
        if not obs.eligible:
            return EVCC_STATUS_DISCONNECTED
        if (wall_connector := self.entry.options.get(CONF_WALL_CONNECTOR_ENTITY)) and (
            _known_state(self.hass, wall_connector) != STATE_ON
        ):
            return EVCC_STATUS_DISCONNECTED
        return EVCC_STATUS_CHARGING if obs.charging is True else EVCC_STATUS_CONNECTED

    def attributes(self, now: datetime) -> dict[str, Any]:
        """Controller snapshot plus what the runtime knows; no coordinates."""
        return self.controller.snapshot(now) | {
            "observation": asdict(self.observation()),
            "reauth_required": self.reauth_required,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "last_provider_read": self.last_provider_read,
            "last_provider_read_at": (
                self.last_provider_read_at.isoformat() if self.last_provider_read_at else None
            ),
        }

    # Lifecycle -----------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start ticking: periodically, on mapped entity changes and on requests."""
        self._unsubscribe.append(
            async_track_time_interval(
                self.hass,
                self._async_handle_interval,
                TICK_INTERVAL,
                name=f"{DOMAIN} tick",
                cancel_on_shutdown=True,
            )
        )
        if entity_ids := [
            entity_id for key in ENTITY_OPTIONS if (entity_id := self.entry.options.get(key))
        ]:
            self._unsubscribe.append(
                async_track_state_change_event(
                    self.hass, entity_ids, self._async_handle_state_change
                )
            )
        self.request_tick()

    async def async_stop(self) -> None:
        """Stop ticking and let a tick in flight finish.

        Ownership is not released: the journal persists and resumes on restart.
        """
        self._stopped = True
        while self._unsubscribe:
            self._unsubscribe.pop()()
        if (task := self._tick_task) is not None and not task.done():
            # A tick cut off mid-write is safe (the next start resolves it by a
            # read), but letting it finish leaves a tidier journal.
            _, pending = await asyncio.wait({task}, timeout=STOP_TIMEOUT)
            if pending:
                task.cancel()
                await asyncio.wait({task})
        self._listeners.clear()

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call ``update_callback`` after every tick and request."""
        self._listeners.append(update_callback)

        @callback
        def remove_listener() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove_listener

    @callback
    def async_notify(self) -> None:
        """Tell the entities to write their state."""
        for update_callback in list(self._listeners):
            update_callback()

    # Ticks ---------------------------------------------------------------

    @callback
    def request_tick(self) -> None:
        """Schedule a tick without waiting for it.

        Ticks run one at a time. Requests made while a tick runs are coalesced
        into a single follow-up tick.
        """
        if self._stopped:
            return
        self._tick_requested = True
        if self._tick_running:
            return
        self._tick_running = True
        self._tick_task = self.entry.async_create_background_task(
            self.hass, self._async_run_ticks(), f"{DOMAIN} tick {self.entry.entry_id}"
        )

    async def _async_run_ticks(self) -> None:
        try:
            while self._tick_requested and not self._stopped:
                self._tick_requested = False
                await self._async_tick_once()
        finally:
            self._tick_running = False

    async def _async_tick_once(self) -> None:
        now = dt_util.now()
        try:
            if self._auth_retry_at is not None and now < self._auth_retry_at:
                # Do not keep sending requests with a session Rivian rejected.
                return
            await self.controller.async_tick(now, self.observation())
        except AuthError:
            self._async_handle_auth_error(now)
        except Exception:
            _LOGGER.exception("Unexpected error while driving the Rivian charging schedule")
        else:
            self._auth_retry_at = None
            self.last_tick_at = now
        finally:
            self.async_notify()

    @callback
    def _async_handle_interval(self, _now: datetime) -> None:
        self.request_tick()

    @callback
    def _async_handle_state_change(self, _event: Event[EventStateChangedData]) -> None:
        # evcc_status follows the mapped entities immediately.
        self.async_notify()
        self.request_tick()

    @callback
    def _async_handle_auth_error(self, now: datetime) -> None:
        self.reauth_required = True
        self._auth_retry_at = now + AUTH_RETRY_INTERVAL
        if not self._reauth_started:
            self._reauth_started = True
            _LOGGER.warning("The Rivian session was rejected; starting reauthentication")
            self.entry.async_start_reauth(self.hass)

    # Requests ------------------------------------------------------------

    async def async_request_charge(self, charge: bool) -> None:
        """Record evcc's start/stop request and tick in the background."""
        await self.controller.async_request_charge(charge)
        self.async_notify()
        self.request_tick()

    async def async_request_amps(self, amps: float) -> None:
        """Record evcc's current request and tick in the background."""
        await self.controller.async_request_amps(amps)
        self.async_notify()
        self.request_tick()

    @callback
    def async_set_renewal(self, enabled: bool) -> None:
        """Enable or disable lease renewal (for the attended lease-expiry trial)."""
        self.controller.renewal_enabled = enabled
        self.async_notify()
        self.request_tick()

    async def async_request_release(self, reason: str) -> None:
        """Ask the next tick to restore the owner's schedule."""
        await self.controller.async_request_release(reason)
        self.async_notify()
        self.request_tick()

    async def async_abandon(self) -> None:
        """Forget ownership without writing anything to Rivian."""
        await self.controller.async_abandon()
        self.async_notify()
        self.request_tick()

    async def async_clear_fault(self) -> bool:
        """Clear a fault if Rivian holds a schedule this adapter knows."""
        try:
            cleared = await self._async_provider_call(
                self.controller.async_clear_fault(dt_util.now())
            )
        finally:
            self.async_notify()
        if cleared:
            self.request_tick()
        return cleared

    async def async_read_schedules(self) -> dict[str, Any]:
        """Read Rivian's schedule list (without coordinates) and keep the result."""
        result = await self._async_provider_call(self.controller.async_read_provider())
        self.last_provider_read = result
        self.last_provider_read_at = dt_util.now()
        self.async_notify()
        return result

    async def _async_provider_call[T](self, call: Awaitable[T]) -> T:
        """Await an operator action that talks to Rivian, mapping its errors."""
        try:
            return await call
        except AuthError as err:
            self._async_handle_auth_error(dt_util.now())
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="reauth_required"
            ) from err
        except ScheduleApiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="provider_error",
                translation_placeholders={"error": str(err)},
            ) from err
        except (TypeError, ValueError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unsupported_schedule",
                translation_placeholders={"error": str(err)},
            ) from err


def describe_schedules(result: dict[str, Any]) -> str:
    """Format a provider read for a notification: days, start, duration, amps, enabled."""
    count = result["count"]
    lines = [
        f"Rivian reports {count} charging schedule{'' if count == 1 else 's'} "
        f"(hash {result['hash']})."
    ]
    for index, schedule in enumerate(result["schedules"], start=1):
        start = int(schedule["startTime"])
        duration = int(schedule["duration"])
        hours, minutes = divmod(duration, 60)
        length = f"{hours} h {minutes} min" if hours else f"{minutes} min"
        lines.append(
            f"{index}. {', '.join(schedule['weekDays'])} from {start // 60:02d}:{start % 60:02d} "
            f"for {length} at {schedule['amperage']} A, "
            f"{'enabled' if schedule['enabled'] else 'disabled'}"
        )
    return "\n".join(lines)
