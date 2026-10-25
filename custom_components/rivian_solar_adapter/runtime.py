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

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_CORE_CONFIG_UPDATE,
    EVENT_HOMEASSISTANT_STOP,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.redact import async_redact_data
from homeassistant.util import dt as dt_util

from .api import AuthError, ScheduleApiError
from .const import (
    ACTIVE_CHARGER_STATES,
    AUTH_RETRY_INTERVAL,
    CONF_ACCESS_TOKEN,
    CONF_CHARGER_STATE_ENTITY,
    CONF_CLOCK_SKEW_MINUTES,
    CONF_DRY_RUN,
    CONF_EMAIL,
    CONF_INCREASE_INTERVAL,
    CONF_LEASE_MINUTES,
    CONF_LIVENESS_ENTITY,
    CONF_LOCATION_ENTITY,
    CONF_MIN_WRITE_INTERVAL,
    CONF_OVERRIDE_ENTITY,
    CONF_PLUGGED_ENTITY,
    CONF_REFRESH_TOKEN,
    CONF_RELEASE_DELAY,
    CONF_RENEW_BEFORE_MINUTES,
    CONF_USER_SESSION_TOKEN,
    CONF_VEHICLE_ID,
    CONF_WALL_CONNECTOR_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
    ENTITY_OPTIONS,
    EVCC_STATUS_CHARGING,
    EVCC_STATUS_CONNECTED,
    EVCC_STATUS_DISCONNECTED,
    MISSING_ENTITY_GRACE,
    MONITORED_ENTITY_OPTIONS,
    OVERRIDE_PASS_STATES,
    STOP_TIMEOUT,
    TICK_INTERVAL,
    journal_key,
)
from .controller import (
    AdapterConfig,
    Fault,
    JournalSaveError,
    Observation,
    Phase,
    ReleaseResult,
    RivianScheduleController,
    summarize_schedules,
)

_LOGGER = logging.getLogger(__name__)

_UNKNOWN_STATES = frozenset({STATE_UNKNOWN, STATE_UNAVAILABLE, ""})

# Removed from diagnostics and entity attributes wherever they appear. Every
# schedule has a location, and fingerprints keep their locations separately.
REDACT_KEYS = frozenset(
    {
        "location",
        "latitude",
        "longitude",
        "locations",
        CONF_EMAIL,
        CONF_ACCESS_TOKEN,
        CONF_REFRESH_TOKEN,
        CONF_USER_SESSION_TOKEN,
        CONF_VEHICLE_ID,
    }
)

type RivianSolarAdapterConfigEntry = ConfigEntry[AdapterRuntime]


def adapter_config_from_entry(hass: HomeAssistant, entry: ConfigEntry) -> AdapterConfig:
    """Build the controller tuning from the entry options and Home Assistant's home."""
    options = DEFAULT_OPTIONS | dict(entry.options)
    return AdapterConfig(
        home_location={
            "latitude": float(hass.config.latitude),
            "longitude": float(hass.config.longitude),
        },
        # The vehicle evaluates windows on its local wall clock.
        time_zone=dt_util.get_time_zone(hass.config.time_zone) or dt_util.DEFAULT_TIME_ZONE,
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


def _binary_state(hass: HomeAssistant, entity_id: str | None) -> bool | None:
    """on is True, off is False; anything else, or no entity, is unknown."""
    state = _known_state(hass, entity_id)
    if state == STATE_ON:
        return True
    if state == STATE_OFF:
        return False
    return None


def restore_failed_notification_id(vehicle_id: str) -> str:
    """Notification about an owner's schedule left unrestored; it follows the vehicle."""
    return f"{journal_key(vehicle_id)}_restore_failed"


def setup_failed_issue_id(entry_id: str) -> str:
    """Repair issue for an entry that owns the schedule but could not be set up."""
    return f"setup_failed_while_owning_{entry_id}"


class AdapterRuntime:
    """Owns the controller of one config entry and drives it from Home Assistant."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        controller: RivianScheduleController,
        *,
        journal_path: str,
        sw_version: str | None = None,
        time_zone: str | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.journal_path = journal_path
        self.sw_version = sw_version
        # Home Assistant's time zone when the controller's config was built.
        self.time_zone: str = time_zone if time_zone is not None else hass.config.time_zone
        self.vehicle_id: str = entry.data[CONF_VEHICLE_ID]
        self.last_provider_read: dict[str, Any] | None = None
        self.last_provider_read_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.reauth_required = False
        self.unexpected_error: str | None = None
        self._auth_retry_at: datetime | None = None
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubscribe: list[CALLBACK_TYPE] = []
        self._tick_task: asyncio.Task[None] | None = None
        self._tick_requested = False
        self._tick_running = False
        self._stopped = False
        self._notified_fault: tuple[str, datetime] | None = None
        self._missing_since: dict[str, datetime] = {}
        self._missing_issues: set[str] = set()

    # Observation ---------------------------------------------------------

    def observation(self) -> Observation:
        """Read the vehicle facts from the mapped entities; unknown stays None."""
        options = self.entry.options
        location_state = _known_state(self.hass, options.get(CONF_LOCATION_ENTITY))
        charging: bool | None = None
        if charger_entity := options.get(CONF_CHARGER_STATE_ENTITY):
            charger_state = _known_state(self.hass, charger_entity)
            if charger_state is not None:
                charging = charger_state in ACTIVE_CHARGER_STATES
        return Observation(
            plugged=_binary_state(self.hass, options.get(CONF_PLUGGED_ENTITY)),
            # "home" is home; not_home and any zone name are away.
            home=None if location_state is None else location_state == "home",
            charging=charging,
            at_charger=_binary_state(self.hass, options.get(CONF_WALL_CONNECTOR_ENTITY)),
            controller_alive=self._controller_alive(),
        )

    def _controller_alive(self) -> bool | None:
        """Whether evcc's liveness entity is available; None if none is configured."""
        if not (entity_id := self.entry.options.get(CONF_LIVENESS_ENTITY)):
            return None
        return _known_state(self.hass, entity_id) is not None

    @property
    def override_active(self) -> bool:
        """Whether the override entity names another vehicle."""
        state = _known_state(self.hass, self.entry.options.get(CONF_OVERRIDE_ENTITY))
        return state is not None and state.casefold() not in OVERRIDE_PASS_STATES

    @property
    def evcc_status(self) -> str:
        """The vehicle status for evcc: a (not here), b (connected) or c (charging).

        Eligibility is the controller's: known plugged in at home, and not
        known absent from the Wall Connector.
        """
        obs = self.observation()
        if self.override_active or not obs.eligible:
            return EVCC_STATUS_DISCONNECTED
        return EVCC_STATUS_CHARGING if obs.charging is True else EVCC_STATUS_CONNECTED

    def attributes(self, now: datetime) -> dict[str, Any]:
        """Controller snapshot plus what the runtime knows; no coordinates."""
        return async_redact_data(
            self.controller.snapshot(now)
            | {
                "observation": asdict(self.observation()),
                "vehicle_override_active": self.override_active,
                "reauth_required": self.reauth_required,
                "unexpected_error": self.unexpected_error,
                "last_provider_read": self.last_provider_read,
                "last_provider_read_at": (
                    self.last_provider_read_at.isoformat() if self.last_provider_read_at else None
                ),
            },
            REDACT_KEYS,
        )

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
        self._unsubscribe.append(
            self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._async_handle_stop)
        )
        self._unsubscribe.append(
            self.hass.bus.async_listen(
                EVENT_CORE_CONFIG_UPDATE, self._async_handle_core_config_update
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
        if self.controller.journal.phase is not Phase.FAULT:
            # Left over from before a reload.
            self._async_dismiss_fault_notifications()
        # The time zone may have changed while the entry was being set up.
        self._async_handle_core_config_update(None)
        self.request_tick()

    async def async_stop(self) -> None:
        """Stop ticking and let a tick in flight finish.

        Ownership is not released here: the journal persists and resumes on
        restart. Disabling and removal restore the owner's schedule separately.
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
        for option in self._missing_issues:
            ir.async_delete_issue(self.hass, DOMAIN, self._missing_issue_id(option))
        self._missing_issues.clear()
        self._listeners.clear()

    @callback
    def _async_handle_stop(self, _event: Event) -> None:
        """No ticks while Home Assistant stops; a lease lapses on its own."""
        self._stopped = True

    @callback
    def _async_handle_core_config_update(self, _event: Event | None) -> None:
        """Reload when Home Assistant's time zone changes.

        The vehicle evaluates windows on its local clock, and the controller's
        time zone is fixed when it is created. After the reload, the next tick
        rewrites the window held on the vehicle for the new zone.
        """
        if self._stopped or self.hass.config.time_zone == self.time_zone:
            return
        _LOGGER.info(
            "Home Assistant's time zone changed from %s to %s; reloading the solar adapter "
            "so its charging windows follow the new local time",
            self.time_zone,
            self.hass.config.time_zone,
        )
        self._stopped = True  # no tick with the old zone until the reload
        self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

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
        """Update the fault notification and tell the entities to write their state."""
        self._async_update_fault_notification()
        for update_callback in list(self._listeners):
            update_callback()

    # Ticks ---------------------------------------------------------------

    @callback
    def request_tick(self) -> None:
        """Schedule a tick without waiting for it.

        Ticks run one at a time. Requests made while a tick runs are coalesced
        into a single follow-up tick.
        """
        if self._stopped or self.hass.is_stopping:
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
        if self.hass.is_stopping:
            return
        now = dt_util.utcnow()
        self._async_check_entities(now)
        try:
            if self._auth_retry_at is not None and now < self._auth_retry_at:
                # Do not keep sending requests with a session Rivian rejected.
                return
            await self.controller.async_tick(now, self.observation())
        except AuthError:
            self._async_handle_auth_error(now)
        except Exception as err:
            # Logged in full once, then quietly until a tick succeeds.
            if self.unexpected_error is None:
                _LOGGER.exception("Unexpected error while driving the Rivian charging schedule")
            else:
                _LOGGER.debug(
                    "Unexpected error while driving the Rivian charging schedule", exc_info=True
                )
            self.unexpected_error = type(err).__name__
        else:
            self._auth_retry_at = None
            self.reauth_required = False
            if self.unexpected_error is not None:
                _LOGGER.info("Driving the Rivian charging schedule works again")
                self.unexpected_error = None
            self.last_tick_at = now
        finally:
            self.async_notify()

    @callback
    def _async_handle_interval(self, _now: datetime) -> None:
        self.request_tick()

    @callback
    def _async_handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        old_state: State | None = event.data["old_state"]
        new_state: State | None = event.data["new_state"]
        if old_state and new_state and old_state.state == new_state.state:
            # Attribute updates (a tracker's coordinates, say) change nothing here.
            return
        # evcc_status follows the mapped entities immediately.
        self.async_notify()
        self.request_tick()

    @callback
    def _async_handle_auth_error(self, now: datetime) -> None:
        """Ask for a new login after Rivian rejected the session.

        Home Assistant starts no second reauth flow while one is open, so this
        asks again after a flow that ended without new tokens.
        """
        if not self.reauth_required:
            _LOGGER.warning("The Rivian session was rejected; starting reauthentication")
        self.reauth_required = True
        self._auth_retry_at = now + AUTH_RETRY_INTERVAL
        self.entry.async_start_reauth(self.hass)

    # Mapped entities -----------------------------------------------------

    def _missing_issue_id(self, option: str) -> str:
        return f"missing_entity_{self.entry.entry_id}_{option}"

    @callback
    def _async_check_entities(self, now: datetime) -> None:
        """Raise a repair issue while a mapped plug, location or liveness entity is missing.

        A missing plug or location entity is treated as unknown. A missing
        liveness entity is treated as evcc being down, which blocks every start
        and renewal.
        """
        for option in MONITORED_ENTITY_OPTIONS:
            if not (entity_id := self.entry.options.get(option)):
                continue
            if self.hass.states.get(entity_id) is not None:
                self._missing_since.pop(option, None)
                if option in self._missing_issues:
                    self._missing_issues.discard(option)
                    ir.async_delete_issue(self.hass, DOMAIN, self._missing_issue_id(option))
                continue
            since = self._missing_since.setdefault(option, now)
            if option in self._missing_issues or now - since < MISSING_ENTITY_GRACE:
                continue
            self._missing_issues.add(option)
            _LOGGER.warning(
                "%s, mapped as the adapter's %s entity, does not exist; %s",
                entity_id,
                option.removesuffix("_entity"),
                "evcc is treated as down, so no charging is started or renewed"
                if option == CONF_LIVENESS_ENTITY
                else "the adapter treats its state as unknown",
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._missing_issue_id(option),
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=f"missing_{option}",
                translation_placeholders={"entity_id": entity_id, "title": self.entry.title},
            )

    # Notifications -------------------------------------------------------

    @property
    def _fault_notification_id(self) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_fault"

    @property
    def _release_notification_id(self) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_release_pending"

    @callback
    def _async_update_fault_notification(self) -> None:
        """Notify on entering a fault; dismiss once the phase leaves it."""
        journal = self.controller.journal
        fault = journal.fault if journal.phase is Phase.FAULT else None
        if fault is None:
            if self._notified_fault is not None:
                self._async_dismiss_fault_notifications()
            return
        if self._notified_fault == (fault.code, fault.at):
            return
        self._notified_fault = (fault.code, fault.at)
        persistent_notification.async_create(
            self.hass,
            _fault_message(self.entry.title, fault),
            title=f"{self.entry.title} solar adapter stopped: {fault.code}",
            notification_id=self._fault_notification_id,
        )

    @callback
    def _async_dismiss_fault_notifications(self) -> None:
        self._notified_fault = None
        persistent_notification.async_dismiss(self.hass, self._fault_notification_id)
        persistent_notification.async_dismiss(self.hass, self._release_notification_id)

    # Requests ------------------------------------------------------------

    async def async_request_charge(self, charge: bool) -> None:
        """Record evcc's start/stop request and tick in the background."""
        try:
            accepted = await self._async_journal_call(self.controller.async_request_charge(charge))
        finally:
            self.async_notify()
        self._async_after_request(accepted)

    async def async_request_amps(self, amps: float) -> None:
        """Record evcc's current request and tick in the background."""
        try:
            accepted = await self._async_journal_call(self.controller.async_request_amps(amps))
        finally:
            self.async_notify()
        self._async_after_request(accepted)

    @callback
    def _async_after_request(self, accepted: bool) -> None:
        if not accepted:
            _LOGGER.debug(
                "Ignoring an evcc request: control was handed back to the owner until the "
                "vehicle is unplugged"
            )
        self.request_tick()

    async def async_set_renewal(self, enabled: bool) -> None:
        """Enable or disable lease renewal (persisted; for the lease-expiry trial)."""
        try:
            await self._async_journal_call(self.controller.async_set_renewal(enabled))
        finally:
            self.async_notify()
        self.request_tick()

    async def async_request_release(self, reason: str) -> ReleaseResult:
        """Restore the owner's schedule and hand control back until the next unplug."""
        try:
            result = await self._async_journal_call(self.controller.async_request_release(reason))
        finally:
            self.async_notify()
        if result is ReleaseResult.AFTER_FAULT:
            persistent_notification.async_create(
                self.hass,
                (
                    f"The release of {self.entry.title} was recorded. The adapter is in a fault "
                    "and writes nothing until it is cleared, so the owner's charging schedules "
                    "are restored once the fault is cleared. To restore them now, whatever "
                    "Rivian holds, use the force restore action."
                ),
                title=f"{self.entry.title} solar adapter: release waits for the fault",
                notification_id=self._release_notification_id,
            )
        self.request_tick()
        return result

    async def async_abandon(self) -> None:
        """While faulted, stop controlling the vehicle without writing anything.

        The owner's saved schedules are archived and listed in a notification.
        """
        if self.controller.journal.phase is not Phase.FAULT:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="abandon_not_faulted"
            )
        try:
            abandoned = await self._async_journal_call(self.controller.async_abandon())
        finally:
            self.async_notify()
        if not abandoned:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="abandon_not_faulted"
            )
        archived = self.controller.journal.archived_original
        if archived is None:
            summary = "The adapter held no copy of the owner's charging schedules."
        else:
            summary = "\n".join(
                [
                    "The adapter's copy of the owner's charging schedules was archived. "
                    "Check that the Rivian app shows them:",
                    *format_schedules(summarize_schedules(archived)),
                ]
            )
        persistent_notification.async_create(
            self.hass,
            f"The solar adapter for {self.entry.title} no longer controls the vehicle and "
            f"wrote nothing to Rivian.\n\n{summary}",
            title=f"{self.entry.title} solar adapter: ownership abandoned",
            notification_id=f"{DOMAIN}_{self.entry.entry_id}_abandoned",
        )
        self.request_tick()

    async def async_force_restore(self) -> None:
        """While faulted, write the owner's saved schedules whatever Rivian holds.

        A copy archived by abandon ownership is only written while Rivian holds a
        window this adapter wrote; otherwise nothing is written.
        """
        journal = self.controller.journal
        if journal.phase is not Phase.FAULT:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="force_restore_not_faulted"
            )
        if journal.original is None and journal.archived_original is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="force_restore_no_snapshot"
            )
        fault_before = journal.fault
        try:
            restored = await self._async_provider_call(
                self.controller.async_force_restore(dt_util.utcnow())
            )
        finally:
            self.async_notify()
        if not restored:
            # A write that could not be verified records a new fault. An unchanged
            # fault means nothing was written: the archived copy was not safe to apply.
            written = self.controller.journal.fault is not fault_before
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=(
                    "force_restore_unverified" if written else "force_restore_archive_unsafe"
                ),
            )
        self.request_tick()

    async def async_clear_fault(self) -> bool:
        """Clear a fault if Rivian holds a schedule this adapter knows."""
        try:
            cleared = await self._async_provider_call(
                self.controller.async_clear_fault(dt_util.utcnow())
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
        self.last_provider_read_at = dt_util.utcnow()
        self.async_notify()
        return result

    async def _async_journal_call[T](self, call: Awaitable[T]) -> T:
        """Await a request that only saves the journal, mapping a failed save."""
        try:
            return await call
        except JournalSaveError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="journal_save_failed",
                translation_placeholders={"path": self.journal_path},
            ) from err

    async def _async_provider_call[T](self, call: Awaitable[T]) -> T:
        """Await an operator action that talks to Rivian, mapping its errors."""
        try:
            result = await call
        except AuthError as err:
            self._async_handle_auth_error(dt_util.utcnow())
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="reauth_required"
            ) from err
        except JournalSaveError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="journal_save_failed",
                translation_placeholders={"path": self.journal_path},
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
        # Rivian accepted the session.
        self._auth_retry_at = None
        self.reauth_required = False
        return result


# What to do first for faults whose general remedies do not fit or need a warning.
_FAULT_REMEDIES: dict[str, str] = {
    "restore_rejected": (
        "Rivian refused the owner's schedules when they were written back as one list. "
        "Some accounts return schedules that Rivian then will not accept back together "
        "(upstream PR #308), so writing the same list again is refused again: do not use "
        "force restore, and do not clear the fault while Rivian still shows the adapter's "
        "window, because the release then sends the same list again. Recreate the owner's "
        "schedules in the Rivian app, then press **Clear fault**; once Rivian shows the "
        "same schedules as the adapter's saved copy, the adapter lets go of the vehicle. "
        "If clearing is refused because the recreated schedules differ, use **abandon "
        "ownership**: it writes nothing and keeps the saved copy archived."
    ),
    "write_rejected": (
        "Rivian repeatedly refused the adapter's schedule writes, so none of them was "
        "applied. Check that the vehicle is online and that its charging schedules can be "
        "edited in the Rivian app, then press **Clear fault**. If the fault comes back, "
        "Rivian may have changed its schedule interface; report it with the adapter's "
        "diagnostics."
    ),
}


def _fault_message(title: str, fault: Fault) -> str:
    remedy = _FAULT_REMEDIES.get(fault.code)
    return "\n".join(
        [
            f"The solar adapter for {title} stopped writing the vehicle's charging schedule "
            f"because of a fault.",
            "",
            f"Fault: {fault.code}",
            fault.message,
            *(["", f"First: {remedy}"] if remedy else []),
            "",
            "What you can do:",
            "- Check the charging schedules in the Rivian app.",
            "- **Clear fault** (button or action) resumes once Rivian holds a schedule the "
            "adapter knows: its own window or the owner's schedules.",
            "- **Restore owner schedule** (release) after the fault is cleared restores the "
            "owner's schedules and hands control back. A release requested during the fault "
            "runs once it is cleared.",
            "- The **force restore** action writes the saved owner schedules now, overwriting "
            "whatever Rivian holds, and checks them by reading them back. A copy archived by "
            "abandon ownership is only written while Rivian holds a window this adapter "
            "wrote, so it never replaces schedules the owner has set up since.",
            "- The **abandon ownership** action stops controlling the vehicle without writing "
            "anything; the saved owner schedules are archived and listed in a notification.",
        ]
    )


def format_schedules(schedules: list[dict[str, Any]]) -> list[str]:
    """One line per schedule summary: days, start, duration, amps, enabled."""
    lines = []
    for index, schedule in enumerate(schedules, start=1):
        start = int(schedule["startTime"])
        duration = int(schedule["duration"])
        hours, minutes = divmod(duration, 60)
        length = f"{hours} h {minutes} min" if hours else f"{minutes} min"
        lines.append(
            f"{index}. {', '.join(schedule['weekDays'])} from {start // 60:02d}:{start % 60:02d} "
            f"for {length} at {schedule['amperage']} A, "
            f"{'enabled' if schedule['enabled'] else 'disabled'}"
        )
    return lines


def describe_schedules(result: dict[str, Any]) -> str:
    """Format a provider read for a notification; it never has coordinates."""
    count = result["count"]
    writer = "this adapter" if result["written_by_adapter"] else "not this adapter"
    return "\n".join(
        [
            f"Rivian reports {count} charging schedule{'' if count == 1 else 's'} "
            f"(fingerprint {result['fingerprint']}, written by {writer}).",
            *format_schedules(result["schedules"]),
        ]
    )
