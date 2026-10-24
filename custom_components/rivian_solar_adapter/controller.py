"""Ownership state machine for solar control of a Rivian charging schedule.

The controller turns two requests, "charging allowed" and "current", into
writes of the vehicle's charging schedule. A Gen 2 vehicle has no usable
start/stop command, so the schedule is the only lever:

- **Lease.** A short window that covers now permits charging at the requested
  amperage. It is renewed while charging is requested. If renewals stop, it
  lapses.
- **Parked window.** A window that ended a few minutes ago makes the vehicle
  wait.

Safety rules, carried over from the prototype's journaled design:

1. **Snapshot first.** The owner's complete schedule list is read and persisted
   before the first write. Ownership is persisted before every side effect.
2. **Guard every write.** Each write is preceded by a fresh read. If the
   provider no longer holds the state this adapter last verified, nothing is
   overwritten and the controller faults.
3. **Readback decides.** A write's result is decided by a fresh read, never by
   the mutation response. A timeout is resolved the same way.
4. **Exact restore.** On release (unplug, away, manual, disable), the original
   list is restored exactly, again only if the provider still holds the
   adapter's last verified state.

This module has no Home Assistant imports.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import logging
import math
from typing import Any, Protocol

from .api import AuthError, RateLimitedError, ScheduleApiError
from .schedule import (
    AMPS_MIN,
    applied_amps,
    contract_hash,
    lease_window,
    normalize_schedules,
    to_provider_input,
)

_LOGGER = logging.getLogger(__name__)

JOURNAL_VERSION = 1
# A provider schedule within this distance of home is treated as the home schedule.
HOME_MATCH_KM = 2.0


class ScheduleProvider(Protocol):
    """Provider operations the controller needs."""

    async def get_schedules(self) -> list[dict[str, Any]]:
        """Return the complete schedule list."""

    async def set_schedules(self, schedules: list[dict[str, Any]]) -> None:
        """Replace the complete schedule list."""


class JournalStore(Protocol):
    """Persistence for the ownership journal (Home Assistant's Store fits)."""

    async def async_load(self) -> dict[str, Any] | None:
        """Load the journal."""

    async def async_save(self, data: dict[str, Any]) -> None:
        """Save the journal."""


class Phase(StrEnum):
    """Persisted ownership phase."""

    IDLE = "idle"
    OWNED = "owned"
    WRITING = "writing"
    FAULT = "fault"


class ControlState(StrEnum):
    """What the adapter is doing; this is not a physical charging state."""

    IDLE = "idle"
    WAITING_FOR_VEHICLE = "waiting_for_vehicle"
    LEASE_ACTIVE = "lease_active"
    LEASE_LAPSED = "lease_lapsed"
    PAUSED = "paused"
    WRITING = "writing"
    FAULT = "fault"
    DRY_RUN = "dry_run"


class Purpose(StrEnum):
    """Why a write was made."""

    LEASE = "lease"
    RESTORE = "restore"


@dataclass(frozen=True)
class Observation:
    """Vehicle facts read from Home Assistant; ``None`` means unknown."""

    plugged: bool | None = None
    home: bool | None = None
    charging: bool | None = None

    @property
    def eligible(self) -> bool:
        """Whether the adapter may take control: plugged in at home."""
        return self.plugged is True and self.home is True

    @property
    def ineligible(self) -> bool:
        """Whether the vehicle is known to be unplugged or away."""
        return self.plugged is False or self.home is False


@dataclass(frozen=True)
class AdapterConfig:
    """Tuning; see the options flow for user-facing descriptions."""

    home_location: dict[str, float]
    lease_minutes: int = 20
    renew_before_minutes: int = 8
    clock_skew_minutes: int = 5
    min_write_interval: timedelta = timedelta(seconds=30)
    increase_interval: timedelta = timedelta(minutes=2)
    release_delay: timedelta = timedelta(minutes=1)
    rate_limit_backoff: timedelta = timedelta(minutes=5)
    repark_after: timedelta = timedelta(days=6)
    max_unapplied_writes: int = 3
    dry_run: bool = True


@dataclass
class Lease:
    """The window most recently verified on the provider."""

    active: bool
    amps: int
    written_at: datetime
    expires_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "amps": self.amps,
            "written_at": self.written_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Lease:
        return cls(
            active=data["active"],
            amps=data["amps"],
            written_at=datetime.fromisoformat(data["written_at"]),
            expires_at=(datetime.fromisoformat(data["expires_at"]) if data["expires_at"] else None),
        )


@dataclass
class Pending:
    """A write that was sent (or is about to be) but not yet verified."""

    hash: str
    previous_hash: str
    purpose: Purpose
    lease: Lease | None
    started_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash": self.hash,
            "previous_hash": self.previous_hash,
            "purpose": self.purpose.value,
            "lease": self.lease.to_dict() if self.lease else None,
            "started_at": self.started_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Pending:
        return cls(
            hash=data["hash"],
            previous_hash=data["previous_hash"],
            purpose=Purpose(data["purpose"]),
            lease=Lease.from_dict(data["lease"]) if data["lease"] else None,
            started_at=datetime.fromisoformat(data["started_at"]),
        )


@dataclass
class Fault:
    """A latched condition that stops all writes until cleared."""

    code: str
    message: str
    at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "at": self.at.isoformat()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fault:
        return cls(data["code"], data["message"], datetime.fromisoformat(data["at"]))


@dataclass
class Journal:
    """Persisted ownership and request state."""

    phase: Phase = Phase.IDLE
    original: list[dict[str, Any]] | None = None
    original_hash: str | None = None
    owned_hash: str | None = None
    pending: Pending | None = None
    last_attempt: Pending | None = None
    lease: Lease | None = None
    fault: Fault | None = None
    command_received: bool = False
    desired_charge: bool | None = None
    desired_amps: int = AMPS_MIN
    release_requested: str | None = None
    unapplied_writes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": JOURNAL_VERSION,
            "phase": self.phase.value,
            "original": self.original,
            "original_hash": self.original_hash,
            "owned_hash": self.owned_hash,
            "pending": self.pending.to_dict() if self.pending else None,
            "last_attempt": self.last_attempt.to_dict() if self.last_attempt else None,
            "lease": self.lease.to_dict() if self.lease else None,
            "fault": self.fault.to_dict() if self.fault else None,
            "command_received": self.command_received,
            "desired_charge": self.desired_charge,
            "desired_amps": self.desired_amps,
            "release_requested": self.release_requested,
            "unapplied_writes": self.unapplied_writes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Journal:
        if data.get("version") != JOURNAL_VERSION:
            raise ValueError(f"Unsupported journal version: {data.get('version')}")
        return cls(
            phase=Phase(data["phase"]),
            original=data["original"],
            original_hash=data["original_hash"],
            owned_hash=data["owned_hash"],
            pending=Pending.from_dict(data["pending"]) if data["pending"] else None,
            last_attempt=(
                Pending.from_dict(data["last_attempt"]) if data["last_attempt"] else None
            ),
            lease=Lease.from_dict(data["lease"]) if data["lease"] else None,
            fault=Fault.from_dict(data["fault"]) if data["fault"] else None,
            command_received=data["command_received"],
            desired_charge=data["desired_charge"],
            desired_amps=data["desired_amps"],
            release_requested=data["release_requested"],
            unapplied_writes=data["unapplied_writes"],
        )


def _distance_km(a: dict[str, float], b: dict[str, float]) -> float:
    lat1, lon1 = math.radians(a["latitude"]), math.radians(a["longitude"])
    lat2, lon2 = math.radians(b["latitude"]), math.radians(b["longitude"])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


class RivianScheduleController:
    """Drive one vehicle's charging schedule from charge and current requests."""

    def __init__(self, api: ScheduleProvider, store: JournalStore, config: AdapterConfig) -> None:
        self._api = api
        self._store = store
        self.config = config
        self._journal = Journal()
        self._lock = asyncio.Lock()
        self._ineligible_since: datetime | None = None
        self._last_write_at: datetime | None = None
        self._backoff_until: datetime | None = None
        self.renewal_enabled = True
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.last_observation = Observation()
        self.provider_schedule_count: int | None = None
        self.dry_run_target: dict[str, Any] | None = None

    @property
    def journal(self) -> Journal:
        """The current journal (read-only use)."""
        return self._journal

    async def async_load(self) -> None:
        """Load the persisted journal, if any."""
        data = await self._store.async_load()
        if data:
            self._journal = Journal.from_dict(data)

    # Requests -------------------------------------------------------------

    # Requests only update the journal; the next tick acts on them. They do not
    # wait for a running tick, so a caller such as evcc is never held up by
    # provider round trips.

    async def async_request_charge(self, charge: bool) -> None:
        """Record whether charging is requested (evcc start/stop)."""
        self._journal.desired_charge = charge
        self._journal.command_received = True
        await self._save()

    async def async_request_amps(self, amps: float) -> None:
        """Record the requested current (evcc max current)."""
        self._journal.desired_amps = applied_amps(amps)
        self._journal.command_received = True
        await self._save()

    async def async_request_release(self, reason: str = "manual") -> None:
        """Ask the next tick to restore the owner's schedule."""
        if self._journal.phase in (Phase.OWNED, Phase.WRITING):
            self._journal.release_requested = reason
            await self._save()

    # Periodic work --------------------------------------------------------

    async def async_tick(self, now: datetime, obs: Observation) -> None:
        """Advance the state machine. AuthError propagates for reauthentication."""
        async with self._lock:
            self.last_observation = obs
            if obs.ineligible:
                self._ineligible_since = self._ineligible_since or now
            elif obs.eligible:
                self._ineligible_since = None
            if self._journal.phase is Phase.FAULT:
                return
            if self._backoff_until and now < self._backoff_until:
                return
            try:
                await self._step(now, obs)
            except RateLimitedError as err:
                self._backoff_until = now + self.config.rate_limit_backoff
                self._record_error(now, err)
            except AuthError as err:
                self._record_error(now, err)
                raise
            except ScheduleApiError as err:
                self._record_error(now, err)

    async def _step(self, now: datetime, obs: Observation) -> None:
        journal = self._journal
        if journal.phase is Phase.WRITING:
            await self._resolve_pending(now)
            if journal.phase is not Phase.OWNED:
                return
        if journal.phase is Phase.OWNED:
            if self.config.dry_run and not journal.release_requested:
                # Dry run never holds ownership; give the owner's schedule back.
                journal.release_requested = "dry run enabled"
            if journal.release_requested or (
                self._ineligible_since is not None
                and now - self._ineligible_since >= self.config.release_delay
            ):
                await self._release(now, journal.release_requested or "unplugged or away")
                return
            await self._drive(now)
            return
        # Idle: take control only after a request while plugged in at home.
        if not journal.command_received or not obs.eligible:
            self.dry_run_target = None
            return
        if self.config.dry_run:
            self.dry_run_target = self._dry_run_target(now, obs)
            return
        await self._take_ownership(now, obs)
        if journal.phase is Phase.OWNED:
            await self._drive(now)

    # Ownership ------------------------------------------------------------

    async def _take_ownership(self, now: datetime, obs: Observation) -> None:
        journal = self._journal
        current = await self._api.get_schedules()
        try:
            original = normalize_schedules(current, strict=False)
        except (TypeError, ValueError) as err:
            self._fault(now, "unsupported_schedule", f"Unexpected provider schedule: {err}")
            await self._save()
            return
        self.provider_schedule_count = len(original)
        journal.original = original
        journal.original_hash = contract_hash(original)
        journal.owned_hash = journal.original_hash
        journal.phase = Phase.OWNED
        journal.lease = None
        journal.pending = None
        journal.last_attempt = None
        journal.unapplied_writes = 0
        journal.release_requested = None
        if journal.desired_charge is None:
            # Only a current was requested: keep a session that is already running.
            journal.desired_charge = obs.charging is True
        await self._save()
        _LOGGER.info("Took ownership of %d charging schedule(s)", len(original))

    async def _drive(self, now: datetime) -> None:
        journal = self._journal
        want_active = journal.desired_charge is True
        amps = journal.desired_amps
        lease = journal.lease
        reason: str | None = None
        if lease is None or lease.active != want_active:
            reason = "start" if want_active else "pause"
        elif want_active:
            if amps < lease.amps:
                reason = "decrease"
            elif amps > lease.amps and self._since_last_write(now) >= self.config.increase_interval:
                reason = "increase"
            elif (
                self.renewal_enabled
                and lease.expires_at is not None
                and lease.expires_at - now <= timedelta(minutes=self.config.renew_before_minutes)
            ):
                reason = "renew"
        elif now - lease.written_at >= self.config.repark_after:
            reason = "repark"
        if reason is None:
            return
        if self._since_last_write(now) < self.config.min_write_interval:
            return
        lease_amps = amps if want_active or lease is None else lease.amps
        window = lease_window(
            now,
            active=want_active,
            lease_minutes=self.config.lease_minutes,
            skew_minutes=self.config.clock_skew_minutes,
            amps=lease_amps,
            location=self._lease_location(),
        )
        new_lease = Lease(
            active=want_active,
            amps=lease_amps,
            written_at=now,
            expires_at=(
                now + timedelta(minutes=self.config.lease_minutes) if want_active else None
            ),
        )
        _LOGGER.debug("Schedule write for %s at %d A", reason, lease_amps)
        await self._write(now, [window], Purpose.LEASE, new_lease, strict=True)

    async def _release(self, now: datetime, reason: str) -> None:
        journal = self._journal
        _LOGGER.info("Restoring the owner's charging schedule (%s)", reason)
        if journal.original is None:
            self._clear_ownership()
            await self._save()
            return
        await self._write(now, journal.original, Purpose.RESTORE, None, strict=False)

    def _clear_ownership(self) -> None:
        journal = self._journal
        journal.phase = Phase.IDLE
        journal.original = None
        journal.original_hash = None
        journal.owned_hash = None
        journal.pending = None
        journal.last_attempt = None
        journal.lease = None
        journal.fault = None
        journal.command_received = False
        journal.desired_charge = None
        journal.release_requested = None
        journal.unapplied_writes = 0

    # Writes ---------------------------------------------------------------

    async def _write(
        self,
        now: datetime,
        target: list[dict[str, Any]],
        purpose: Purpose,
        lease: Lease | None,
        *,
        strict: bool,
    ) -> None:
        journal = self._journal
        normalized = normalize_schedules(target, strict=strict)
        target_hash = contract_hash(normalized)
        current_hash = await self._read_hash(now)
        if current_hash is None:
            return
        if current_hash != journal.owned_hash:
            attempt = journal.last_attempt
            if attempt is not None and current_hash == attempt.hash:
                # An earlier write landed after its readback.
                self._confirm(attempt)
                await self._save()
                if journal.phase is not Phase.OWNED:
                    return
            else:
                self._fault(
                    now,
                    "external_edit",
                    "The charging schedule changed outside this adapter; it was not "
                    "overwritten. Check the Rivian app, then clear the fault.",
                )
                await self._save()
                return
        pending = Pending(target_hash, current_hash, purpose, lease, now)
        if current_hash == target_hash:
            self._confirm(pending)
            await self._save()
            return
        journal.phase = Phase.WRITING
        journal.pending = pending
        journal.last_attempt = pending
        await self._save()
        self._last_write_at = now
        try:
            await self._api.set_schedules(to_provider_input(normalized))
        except AuthError, RateLimitedError:
            # The write stays pending and is resolved by a later read.
            raise
        except ScheduleApiError as err:
            # Rejected or unknown: only a fresh read can tell.
            self._record_error(now, err)
        await self._resolve_pending(now)

    async def _resolve_pending(self, now: datetime) -> None:
        journal = self._journal
        pending = journal.pending
        if pending is None:
            journal.phase = Phase.OWNED if journal.original is not None else Phase.IDLE
            return
        current_hash = await self._read_hash(now)
        if current_hash is None:
            return
        if current_hash == pending.hash:
            self._confirm(pending)
        elif current_hash == pending.previous_hash:
            journal.phase = Phase.OWNED
            journal.pending = None
            journal.unapplied_writes += 1
            _LOGGER.warning(
                "Schedule write not applied (%d of %d)",
                journal.unapplied_writes,
                self.config.max_unapplied_writes,
            )
            if journal.unapplied_writes >= self.config.max_unapplied_writes:
                self._fault(
                    now,
                    "write_not_applied",
                    "Rivian repeatedly accepted no schedule change.",
                )
        else:
            self._fault(
                now,
                "conflict_after_write",
                "The schedule read back after a write matches neither the write nor "
                "the previous state; it was not overwritten.",
            )
        await self._save()

    def _confirm(self, pending: Pending) -> None:
        journal = self._journal
        journal.owned_hash = pending.hash
        journal.pending = None
        journal.last_attempt = None
        journal.unapplied_writes = 0
        if pending.purpose is Purpose.RESTORE:
            self._clear_ownership()
            _LOGGER.info("Owner's charging schedule restored")
        else:
            journal.phase = Phase.OWNED
            journal.lease = pending.lease

    async def _read_hash(self, now: datetime) -> str | None:
        current = await self._api.get_schedules()
        self.provider_schedule_count = len(current) if isinstance(current, list) else None
        try:
            return contract_hash(normalize_schedules(current, strict=False))
        except (TypeError, ValueError) as err:
            self._fault(now, "unsupported_schedule", f"Unexpected provider schedule: {err}")
            await self._save()
            return None

    # Operator actions -----------------------------------------------------

    async def async_clear_fault(self, now: datetime) -> bool:
        """Resume after a fault if the provider holds a state this adapter knows."""
        async with self._lock:
            journal = self._journal
            if journal.phase is not Phase.FAULT:
                return True
            if journal.original is None:
                journal.phase = Phase.IDLE
                journal.fault = None
                await self._save()
                return True
            current = await self._api.get_schedules()
            try:
                current_hash = contract_hash(normalize_schedules(current, strict=False))
            except TypeError, ValueError:
                return False
            attempt = journal.last_attempt
            if current_hash == journal.owned_hash:
                journal.phase = Phase.OWNED
                journal.fault = None
                journal.pending = None
                journal.unapplied_writes = 0
            elif attempt is not None and current_hash == attempt.hash:
                self._confirm(attempt)
                journal.fault = None
            elif current_hash == journal.original_hash:
                # The owner's schedule is already back; nothing left to restore.
                self._clear_ownership()
            else:
                return False
            await self._save()
            return True

    async def async_abandon(self) -> None:
        """Forget ownership without writing; the provider state is accepted as is."""
        async with self._lock:
            self._clear_ownership()
            await self._save()

    async def async_read_provider(self) -> dict[str, Any]:
        """Read the provider list for inventory and trial evidence, without coordinates."""
        async with self._lock:
            current = await self._api.get_schedules()
            schedules = normalize_schedules(current, strict=False)
            self.provider_schedule_count = len(schedules)
            return {
                "count": len(schedules),
                "hash": contract_hash(schedules)[:12],
                "schedules": [
                    {
                        "weekDays": s["weekDays"],
                        "startTime": s["startTime"],
                        "duration": s["duration"],
                        "amperage": s["amperage"],
                        "enabled": s["enabled"],
                    }
                    for s in schedules
                ],
            }

    # Reporting ------------------------------------------------------------

    def state(self, now: datetime) -> ControlState:
        """Summarize the adapter's state."""
        journal = self._journal
        if journal.phase is Phase.FAULT:
            return ControlState.FAULT
        if journal.phase is Phase.WRITING:
            return ControlState.WRITING
        if journal.phase is Phase.OWNED:
            lease = journal.lease
            if lease is None or not lease.active:
                return ControlState.PAUSED
            if lease.expires_at is not None and lease.expires_at <= now:
                return ControlState.LEASE_LAPSED
            return ControlState.LEASE_ACTIVE
        if self.config.dry_run:
            return ControlState.DRY_RUN
        if journal.command_received and not self.last_observation.eligible:
            return ControlState.WAITING_FOR_VEHICLE
        return ControlState.IDLE

    def snapshot(self, now: datetime) -> dict[str, Any]:
        """Diagnostic attributes; no coordinates or provider identifiers."""
        journal = self._journal
        lease = journal.lease
        return {
            "phase": journal.phase.value,
            "dry_run": self.config.dry_run,
            "renewal_enabled": self.renewal_enabled,
            "desired_charge": journal.desired_charge,
            "desired_amps": journal.desired_amps,
            "lease_active": lease.active if lease else None,
            "lease_amps": lease.amps if lease else None,
            "lease_written_at": lease.written_at.isoformat() if lease else None,
            "lease_expires_at": (
                lease.expires_at.isoformat() if lease and lease.expires_at else None
            ),
            "owner_schedule_count": len(journal.original) if journal.original is not None else None,
            "provider_schedule_count": self.provider_schedule_count,
            "owned_hash": journal.owned_hash[:12] if journal.owned_hash else None,
            "pending_purpose": journal.pending.purpose.value if journal.pending else None,
            "unapplied_writes": journal.unapplied_writes,
            "release_requested": journal.release_requested,
            "fault_code": journal.fault.code if journal.fault else None,
            "fault_message": journal.fault.message if journal.fault else None,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "last_write_at": self._last_write_at.isoformat() if self._last_write_at else None,
            "backoff_until": self._backoff_until.isoformat() if self._backoff_until else None,
            "dry_run_target": self.dry_run_target,
        }

    # Helpers --------------------------------------------------------------

    def _dry_run_target(self, now: datetime, obs: Observation) -> dict[str, Any]:
        journal = self._journal
        active = (
            journal.desired_charge if journal.desired_charge is not None else (obs.charging is True)
        )
        window = lease_window(
            now,
            active=active,
            lease_minutes=self.config.lease_minutes,
            skew_minutes=self.config.clock_skew_minutes,
            amps=journal.desired_amps,
            location=self.config.home_location,
        )
        window.pop("location")
        return window

    def _lease_location(self) -> dict[str, float]:
        """Use the owner's home schedule location, else Home Assistant's home."""
        home = self.config.home_location
        candidates = [
            schedule["location"]
            for schedule in self._journal.original or []
            if _distance_km(schedule["location"], home) <= HOME_MATCH_KM
        ]
        return candidates[0] if candidates else home

    def _since_last_write(self, now: datetime) -> timedelta:
        if self._last_write_at is None:
            return timedelta.max
        return now - self._last_write_at

    def _fault(self, now: datetime, code: str, message: str) -> None:
        journal = self._journal
        journal.phase = Phase.FAULT
        journal.pending = None
        journal.fault = Fault(code, message, now)
        _LOGGER.warning("Rivian schedule control fault %s: %s", code, message)

    def _record_error(self, now: datetime, err: Exception) -> None:
        self.last_error = str(err)
        self.last_error_at = now
        _LOGGER.warning("Rivian schedule request failed: %s", err)

    async def _save(self) -> None:
        await self._store.async_save(self._journal.to_dict())
