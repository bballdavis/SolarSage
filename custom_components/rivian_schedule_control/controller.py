"""Ownership state machine for solar control of a Rivian charging schedule.

The state machine turns two requests from the charging controller (the solar
planner), "charging allowed" and "current", into writes of the vehicle's
charging schedule. A Gen 2 vehicle has no usable start/stop command, so the
schedule is the only lever:

- **Lease.** A short window covering now permits charging at the requested
  amperage. It is renewed only while the car is known to be plugged in at
  home and the controller is alive; otherwise it lapses on its own.
- **Parked window.** A window that ended a few minutes ago makes the vehicle
  wait.

Safety rules:

1. **Snapshot first.** The owner's complete schedule list is read and persisted
   before the first write. Write intent is persisted before every side effect,
   and a failed save sends nothing.
2. **Guard every write.** Each write is preceded by a fresh read. If the provider
   holds neither the state this adapter last verified nor one of its own
   unverified writes, nothing is overwritten and the controller faults.
3. **Readback decides.** A write's result is decided by fresh reads, never by
   the mutation response. A readback showing the old state is only "not
   applied" once it has settled.
4. **Exact restore.** On release (unplug, away, manual, dry run, disable or
   removal), the original list is restored exactly, subject to the same guard.
5. **Never adopt our own window.** A list this adapter wrote is never
   snapshotted as the owner's original.

All interval arithmetic is in UTC. Local time is used only to build windows,
because the vehicle evaluates them on its local wall clock.

This module has no Home Assistant imports.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum
import logging
import math
from typing import Any, Protocol

from .api import AuthError, RateLimitedError, ScheduleApiError, WriteRejectedError
from .schedule import (
    AMPS_MAX,
    AMPS_MIN,
    Fingerprint,
    applied_amps,
    fingerprint,
    lease_window,
    normalize_schedules,
    to_provider_input,
)

_LOGGER = logging.getLogger(__name__)

JOURNAL_VERSION = 2
# A provider schedule within this distance of home is treated as the home schedule.
HOME_MATCH_KM = 2.0
# A lapsed lease is parked this long after its window has ended on the vehicle.
LAPSE_PARK_MARGIN = timedelta(minutes=1)
# The 48 A ceiling hold arms only for a decrease from 48 A to at least this much:
# that is the chase it prevents, not a cloud's deep dip.
CEILING_BAND_MIN_AMPS = AMPS_MAX - 4
# Without a charging reading, an unconfirmed request is dropped only once the
# controller has looked up this long, in case that reading is just late.
CHARGING_STATE_WAIT = timedelta(seconds=30)


class ScheduleProvider(Protocol):
    """Provider operations the controller needs."""

    async def get_schedules(self) -> list[dict[str, Any]]:
        """Return the complete schedule list."""

    async def set_schedules(self, schedules: list[dict[str, Any]]) -> None:
        """Replace the complete schedule list."""


class JournalStore(Protocol):
    """Durable journal persistence. ``async_save`` must raise if it cannot write."""

    async def async_load(self) -> dict[str, Any] | None:
        """Load the journal."""

    async def async_save(self, data: dict[str, Any]) -> None:
        """Save the journal, raising on failure."""


class JournalSaveError(ScheduleApiError):
    """The journal could not be saved; nothing was sent to Rivian."""


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
    HANDED_BACK = "handed_back"
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


class ReleaseResult(StrEnum):
    """What a release request did."""

    RELEASING = "releasing"
    AFTER_FAULT = "after_fault"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Observation:
    """Facts read from Home Assistant; ``None`` means unknown or not configured."""

    plugged: bool | None = None
    home: bool | None = None
    charging: bool | None = None
    at_charger: bool | None = None
    controller_alive: bool | None = None
    # Whether the controller wants this vehicle charging (its charging entity);
    # None if unknown or not configured.
    controller_charging: bool | None = None

    @property
    def eligible(self) -> bool:
        """Known plugged in at home (and at the Wall Connector, if that is known)."""
        return self.plugged is True and self.home is True and self.at_charger is not False

    @property
    def session_ended(self) -> bool:
        """Known unplugged, or the Wall Connector knows no vehicle is connected."""
        return self.plugged is False or self.at_charger is False

    @property
    def known_ineligible(self) -> bool:
        """Known unplugged or away."""
        return self.session_ended or self.home is False


@dataclass(frozen=True)
class AdapterConfig:
    """Tuning; see the options flow for user-facing descriptions."""

    home_location: dict[str, float]
    time_zone: tzinfo = UTC
    lease_minutes: int = 20
    renew_before_minutes: int = 8
    clock_skew_minutes: int = 5
    min_write_interval: timedelta = timedelta(seconds=30)
    transition_interval: timedelta = timedelta(seconds=5)
    increase_interval: timedelta = timedelta(minutes=2)
    # Increases are applied this far below the request; 0 applies it exactly.
    increase_margin_amps: float = 0.0
    # After a decrease from the 48 A ceiling, increases keep the margin there too
    # for this long; 0 is off. Without a margin it has no effect.
    ceiling_hold: timedelta = timedelta(0)
    # How long the controller must look down before a request made earlier is
    # held; the longer grace applies until it has been seen up since startup.
    liveness_grace: timedelta = timedelta(minutes=3)
    liveness_startup_grace: timedelta = timedelta(minutes=10)
    release_delay: timedelta = timedelta(minutes=1)
    away_release_delay: timedelta = timedelta(minutes=10)
    settle_delay: timedelta = timedelta(seconds=30)
    rate_limit_backoff: timedelta = timedelta(minutes=5)
    error_backoff_min: timedelta = timedelta(seconds=15)
    error_backoff_max: timedelta = timedelta(minutes=15)
    repark_after: timedelta = timedelta(days=6)
    max_unapplied_writes: int = 3
    attempt_history: int = 8
    adapter_history: int = 32
    dry_run: bool = True


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Lease:
    """The window most recently verified on the provider."""

    active: bool
    amps: int
    written_at: datetime
    expires_at: datetime | None
    utc_offset_minutes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "amps": self.amps,
            "written_at": self.written_at.isoformat(),
            "expires_at": _iso(self.expires_at),
            "utc_offset_minutes": self.utc_offset_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Lease:
        return cls(
            active=data["active"],
            amps=data["amps"],
            written_at=datetime.fromisoformat(data["written_at"]),
            expires_at=_from_iso(data["expires_at"]),
            utc_offset_minutes=data["utc_offset_minutes"],
        )


@dataclass
class Attempt:
    """A write: what it should produce and what the provider held before it."""

    target: Fingerprint
    previous: Fingerprint | None
    purpose: Purpose
    lease: Lease | None
    created_at: datetime
    # unsent, sent (accepted or no answer yet), ambiguous (request failed), or
    # rejected (Rivian definitely did not apply it)
    outcome: str = "unsent"

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "target": self.target.to_dict(),
            "previous": self.previous.to_dict() if self.previous else None,
            "purpose": self.purpose.value,
            "lease": self.lease.to_dict() if self.lease else None,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Attempt:
        return cls(
            target=Fingerprint.from_dict(data["target"]),
            previous=Fingerprint.from_dict(data["previous"]) if data["previous"] else None,
            purpose=Purpose(data["purpose"]),
            lease=Lease.from_dict(data["lease"]) if data["lease"] else None,
            created_at=datetime.fromisoformat(data["created_at"]),
            outcome=data.get("outcome", "sent"),
        )


@dataclass
class Fault:
    """A latched condition that stops all automatic writes until cleared."""

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
    original_fp: Fingerprint | None = None
    owned_fp: Fingerprint | None = None
    pending: Attempt | None = None
    # Writes sent but never verified, newest last; recognized as our own.
    attempts: list[Attempt] = field(default_factory=list)
    # Every list this adapter ever wrote (not restores); kept across releases.
    written_states: list[Fingerprint] = field(default_factory=list)
    lease: Lease | None = None
    fault: Fault | None = None
    command_received: bool = False
    desired_charge: bool | None = None
    requested_amps: float = float(AMPS_MIN)
    request_seq: int = 0
    release_requested: str | None = None
    release_hand_back: bool = False
    release_seq: int | None = None
    handed_back: bool = False
    renewal_enabled: bool = True
    unapplied_writes: int = 0
    archived_original: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": JOURNAL_VERSION,
            "phase": self.phase.value,
            "original": self.original,
            "original_fp": self.original_fp.to_dict() if self.original_fp else None,
            "owned_fp": self.owned_fp.to_dict() if self.owned_fp else None,
            "pending": self.pending.to_dict() if self.pending else None,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "written_states": [state.to_dict() for state in self.written_states],
            "lease": self.lease.to_dict() if self.lease else None,
            "fault": self.fault.to_dict() if self.fault else None,
            "command_received": self.command_received,
            "desired_charge": self.desired_charge,
            "requested_amps": self.requested_amps,
            "request_seq": self.request_seq,
            "release_requested": self.release_requested,
            "release_hand_back": self.release_hand_back,
            "release_seq": self.release_seq,
            "handed_back": self.handed_back,
            "renewal_enabled": self.renewal_enabled,
            "unapplied_writes": self.unapplied_writes,
            "archived_original": self.archived_original,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Journal:
        if data.get("version") != JOURNAL_VERSION:
            raise ValueError(f"Unsupported journal version: {data.get('version')}")

        def fp(value: dict[str, Any] | None) -> Fingerprint | None:
            return Fingerprint.from_dict(value) if value else None

        attempts = [Attempt.from_dict(item) for item in data["attempts"]]
        pending = Attempt.from_dict(data["pending"]) if data["pending"] else None
        if pending is not None:
            # The pending write is also in the attempt list; keep them one object.
            pending = next(
                (
                    a
                    for a in attempts
                    if a.created_at == pending.created_at
                    and a.purpose is pending.purpose
                    and a.target.key == pending.target.key
                ),
                pending,
            )
        return cls(
            phase=Phase(data["phase"]),
            original=data["original"],
            original_fp=fp(data["original_fp"]),
            owned_fp=fp(data["owned_fp"]),
            pending=pending,
            attempts=attempts,
            written_states=[Fingerprint.from_dict(item) for item in data["written_states"]],
            lease=Lease.from_dict(data["lease"]) if data["lease"] else None,
            fault=Fault.from_dict(data["fault"]) if data["fault"] else None,
            command_received=data["command_received"],
            desired_charge=data["desired_charge"],
            requested_amps=float(data["requested_amps"]),
            request_seq=data["request_seq"],
            release_requested=data["release_requested"],
            release_hand_back=data["release_hand_back"],
            release_seq=data["release_seq"],
            handed_back=data["handed_back"],
            renewal_enabled=data["renewal_enabled"],
            unapplied_writes=data["unapplied_writes"],
            archived_original=data["archived_original"],
        )


def _distance_km(a: dict[str, float], b: dict[str, float]) -> float:
    lat1, lon1 = math.radians(a["latitude"]), math.radians(a["longitude"])
    lat2, lon2 = math.radians(b["latitude"]), math.radians(b["longitude"])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _floor_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def summarize_schedules(schedules: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Schedule fields for people to read; never coordinates."""
    return [
        {
            "weekDays": schedule["weekDays"],
            "startTime": schedule["startTime"],
            "duration": schedule["duration"],
            "amperage": schedule["amperage"],
            "enabled": schedule["enabled"],
        }
        for schedule in schedules or []
    ]


class RivianScheduleController:
    """Drive one vehicle's charging schedule from charge and current requests."""

    def __init__(self, api: ScheduleProvider, store: JournalStore, config: AdapterConfig) -> None:
        self._api = api
        self._store = store
        self.config = config
        self._journal = Journal()
        self._lock = asyncio.Lock()
        self._ended_since: datetime | None = None
        self._away_since: datetime | None = None
        self._last_write_at: datetime | None = None
        self._last_amps_change_at: datetime | None = None
        self._backoff_until: datetime | None = None
        self._failures = 0
        self._alive_seen = False
        self._down_since: datetime | None = None
        self._up_since: datetime | None = None
        # The controller's request is unconfirmed after this process starts
        # ("startup") and after the controller looks down ("outage"), until it
        # calls again or its charging state settles it. None: confirmed.
        self._epoch: str | None = None
        self._epoch_calls = 0
        self._epoch_held = False
        self._controller_calls = 0
        self._request_unsaved = False
        self._ceiling_hold_until: datetime | None = None
        self._release_reason: str | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.last_observation = Observation()
        self.provider_schedule_count: int | None = None
        self.dry_run_target: dict[str, Any] | None = None

    @property
    def journal(self) -> Journal:
        """The current journal (read-only use)."""
        return self._journal

    @property
    def _releasing(self) -> bool:
        """A release is under way; requests made now must survive it."""
        journal = self._journal
        return journal.release_requested is not None or journal.release_seq is not None

    @property
    def renewal_enabled(self) -> bool:
        """Whether active leases are renewed (off only for the lease-expiry trial)."""
        return self._journal.renewal_enabled

    async def async_load(self) -> bool:
        """Load the persisted journal; return whether one was found."""
        data = await self._store.async_load()
        if not data:
            return False
        self._journal = Journal.from_dict(data)
        # A request from before the restart may be from a controller that has
        # since restarted.
        self._epoch, self._epoch_calls, self._epoch_held = "startup", self._controller_calls, False
        return True

    # Requests -------------------------------------------------------------
    # Requests only update the journal; the next tick acts on them. They do not
    # wait for a running tick, so the controller is never held up by provider
    # round trips. Unchanged requests are not saved again. Every call, changed
    # or not, shows that the controller is running.

    @property
    def _unchanged_ok(self) -> bool:
        """An unchanged request may be acknowledged without saving it again."""
        return not self._releasing and not self._request_unsaved

    async def async_request_charge(self, charge: bool) -> bool:
        """Record the controller's start/stop. Returns False if ignored (handed back)."""
        self._controller_calls += 1
        journal = self._journal
        if journal.handed_back:
            return False
        if journal.command_received and journal.desired_charge is charge and self._unchanged_ok:
            return True
        journal.desired_charge = charge
        journal.command_received = True
        journal.request_seq += 1
        await self._save_request()
        return True

    async def async_request_amps(self, amps: float) -> bool:
        """Record the controller's current. Returns False if ignored (handed back).

        A current request carries the intent to charge, so it counts as a start
        unless charging was explicitly turned off. (evcc set the current before
        it enabled charging.)
        """
        self._controller_calls += 1
        journal = self._journal
        if journal.handed_back:
            return False
        amps = float(amps)
        if (
            journal.command_received
            and journal.desired_charge is not None
            and journal.requested_amps == amps
            and self._unchanged_ok
        ):
            return True
        journal.requested_amps = amps
        if journal.desired_charge is None:
            journal.desired_charge = True
        journal.command_received = True
        journal.request_seq += 1
        await self._save_request()
        return True

    async def async_request_release(self, reason: str, *, hand_back: bool = True) -> ReleaseResult:
        """Restore the owner's schedule and, by default, hand control back.

        Handing back ignores the controller's requests until the next unplug,
        so the controller cannot immediately take the schedule over again.
        """
        journal = self._journal
        journal.request_seq += 1
        if journal.phase is Phase.IDLE:
            journal.command_received = False
            journal.desired_charge = None
            journal.handed_back = hand_back and self.last_observation.plugged is not False
            await self._save_request()
            return ReleaseResult.CANCELLED
        journal.release_requested = reason
        journal.release_hand_back = hand_back
        await self._save_request()
        return (
            ReleaseResult.AFTER_FAULT if journal.phase is Phase.FAULT else ReleaseResult.RELEASING
        )

    async def async_set_renewal(self, enabled: bool) -> None:
        """Enable or disable lease renewal (persisted)."""
        if self._journal.renewal_enabled is not enabled:
            self._journal.renewal_enabled = enabled
            await self._save_request()

    # Periodic work --------------------------------------------------------

    async def async_tick(self, now: datetime, obs: Observation, *, force: bool = False) -> None:
        """Advance the state machine. AuthError propagates for reauthentication.

        ``force`` ignores an error backoff; use it for an explicit, bounded
        restore (integration disabled or removed), not for routine ticks.
        """
        async with self._lock:
            now = now.astimezone(UTC)
            self.last_observation = obs
            self._ended_since = (self._ended_since or now) if obs.session_ended else None
            self._away_since = (self._away_since or now) if obs.home is False else None
            self._track_liveness(now, obs)
            if self._journal.phase is Phase.FAULT:
                return
            if self._backoff_until and now < self._backoff_until and not force:
                return
            try:
                await self._step(now, obs)
            except RateLimitedError as err:
                self._backoff_until = now + (err.backoff or self.config.rate_limit_backoff)
                self._record_error(now, err)
            except AuthError as err:
                self._record_error(now, err)
                raise
            except ScheduleApiError as err:
                self._failures += 1
                delay = min(
                    self.config.error_backoff_min * 2 ** (self._failures - 1),
                    self.config.error_backoff_max,
                )
                self._backoff_until = now + delay
                self._record_error(now, err)
            else:
                self._failures = 0
                self._backoff_until = None

    async def _step(self, now: datetime, obs: Observation) -> None:
        journal = self._journal
        if obs.plugged is False and journal.handed_back:
            journal.handed_back = False
            await self._save()
        if journal.phase is Phase.WRITING:
            await self._resolve_pending(now)
            if journal.phase is not Phase.OWNED:
                return
        # After resolving a write, so the lease is what Rivian holds.
        await self._settle_request(now, obs)
        if journal.phase is Phase.OWNED:
            if self.config.dry_run and not journal.release_requested:
                # Dry run never holds ownership; give the owner's schedule back.
                journal.release_requested = "dry run enabled"
                journal.release_hand_back = False
            if obs.session_ended and journal.command_received:
                # The session is over for the controller too: forget its request
                # and park now.
                journal.command_received = False
                journal.desired_charge = None
                journal.request_seq += 1
                await self._save()
            reason = journal.release_requested
            if self._ended_since and now - self._ended_since >= self.config.release_delay:
                reason = reason or "unplugged"
            if self._away_since and now - self._away_since >= self.config.away_release_delay:
                reason = reason or "away"
            if reason is not None:
                await self._release(now, reason)
                return
            await self._drive(now, obs)
            return
        # Idle.
        if journal.command_received and obs.session_ended:
            # A request for an unplugged car does not carry over to a later plug-in.
            # (Away while still plugged in is kept: the controller may not repeat it.)
            journal.command_received = False
            journal.desired_charge = None
            await self._save()
        if (
            not journal.command_received
            or journal.handed_back
            or not obs.eligible
            or obs.controller_alive is False
            or self._request_unconfirmed
        ):
            # While the controller looks down, or its request is not yet
            # confirmed, the owner's schedule is left alone.
            self.dry_run_target = None
            return
        if self.config.dry_run:
            self.dry_run_target = self._dry_run_target(now)
            return
        await self._take_ownership(now, obs)
        if journal.phase is Phase.OWNED and not journal.release_requested:
            await self._drive(now, obs)

    # Ownership ------------------------------------------------------------

    async def _take_ownership(self, now: datetime, obs: Observation) -> None:
        journal = self._journal
        seq = journal.request_seq
        current = await self._api.get_schedules()
        if journal.request_seq != seq and not journal.command_received:
            return  # cancelled by a release while reading
        try:
            original = normalize_schedules(current, strict=False)
        except (TypeError, ValueError) as err:
            self._fault(now, "unsupported_schedule", f"Unexpected provider schedule: {err}")
            await self._save()
            return
        self.provider_schedule_count = len(original)
        original_fp = fingerprint(original)
        if any(original_fp.matches(state) for state in journal.written_states) or (
            (journal.written_states or journal.archived_original)
            and self._looks_like_adapter_window(original)
        ):
            self._fault(
                now,
                "own_window_present",
                "Rivian holds a schedule this adapter wrote, not the owner's. Recreate the "
                "owner's schedule in the Rivian app, or use force restore if a snapshot is "
                "archived.",
            )
            await self._save()
            return
        journal.original = original
        journal.original_fp = original_fp
        journal.owned_fp = original_fp
        journal.phase = Phase.OWNED
        journal.lease = None
        journal.pending = None
        journal.attempts = []
        journal.unapplied_writes = 0
        journal.release_requested = None
        journal.release_seq = None
        if journal.desired_charge is None:
            journal.desired_charge = obs.charging is True
        await self._save()
        _LOGGER.info("Took ownership of %d charging schedule(s)", len(original))

    async def _drive(self, now: datetime, obs: Observation) -> None:
        journal = self._journal
        if journal.release_requested:
            return  # the next tick restores; write no lease in between
        lease = journal.lease
        local_now = now.astimezone(self.config.time_zone)
        offset = int(local_now.utcoffset().total_seconds() // 60)
        may_extend = self._may_extend(obs)
        requested = journal.requested_amps
        amps_down = applied_amps(requested)
        # An optional margin below the request for increases, off by default.
        # evcc added up to 2 A for measured-current lag and truncated, so
        # following it exactly made the two loops chase each other (it used
        # 3 A). At the ceiling there is no lag to absorb, unless surplus just
        # short of it made the request decrease from there (the ceiling hold).
        at_ceiling = requested >= AMPS_MAX and (
            self._ceiling_hold_until is None or now >= self._ceiling_hold_until
        )
        amps_up = (
            AMPS_MAX if at_ceiling else applied_amps(requested - self.config.increase_margin_amps)
        )
        live = self._lease_live(now)
        reason: str | None = None
        active = False
        amps = AMPS_MIN
        extend = False
        # A request held while the controller looks down allows no window over
        # now, and an unconfirmed one may keep a live lease running but not
        # start one.
        wants = journal.desired_charge is True and not self._request_held
        if wants and (live or (may_extend and not self._request_unconfirmed)):
            active = True
            if not live:
                reason, amps, extend = "start", amps_down, True
            elif amps_down < lease.amps:
                reason, amps, extend = "decrease", amps_down, may_extend
            elif (
                may_extend
                and amps_up > lease.amps
                and self._since(self._last_amps_change_at, now) >= self.config.increase_interval
            ):
                reason, amps, extend = "increase", amps_up, True
            elif may_extend and lease.expires_at - now <= timedelta(
                minutes=self.config.renew_before_minutes
            ):
                reason, amps, extend = "renew", lease.amps, True
            elif lease.utc_offset_minutes != offset:
                reason, amps, extend = "dst", lease.amps, may_extend
        elif lease is None or lease.active:
            # Charging is not requested, or a lease can no longer be extended:
            # no window may cover now. A lapsed lease would recur in a week.
            lapsed = lease is not None and lease.active and not live
            if (
                lapsed
                and wants
                and lease.expires_at is not None
                and now
                < lease.expires_at
                + timedelta(minutes=self.config.clock_skew_minutes)
                + LAPSE_PARK_MARGIN
            ):
                # Let the window end on the vehicle on its own (the cloud-free
                # stop), then park it so it does not recur.
                return
            reason = "lapse" if lapsed else "pause"
        elif lease.utc_offset_minutes != offset:
            reason = "dst"
        elif now - lease.written_at >= self.config.repark_after:
            reason = "repark"
        if reason is None:
            return
        spacing = (
            self.config.transition_interval
            if reason in ("start", "pause", "lapse", "dst")
            else self.config.min_write_interval
        )
        if self._since(self._last_write_at, now) < spacing:
            return
        if active and extend:
            lease_minutes = self.config.lease_minutes
            expires_at = _floor_minute(now) + timedelta(minutes=lease_minutes)
        elif active:
            # Keep the current end: only an extending write may move it.
            remaining = (lease.expires_at - _floor_minute(now)).total_seconds() / 60
            lease_minutes = math.ceil(remaining)
            if lease_minutes <= 0:
                return
            expires_at = lease.expires_at
        else:
            lease_minutes = self.config.lease_minutes
            expires_at = None
        window = lease_window(
            local_now,
            active=active,
            lease_minutes=lease_minutes,
            skew_minutes=self.config.clock_skew_minutes,
            amps=amps,
            location=self._lease_location(),
        )
        new_lease = Lease(active, amps, now, expires_at, offset)
        if active and (lease is None or not lease.active or amps != lease.amps):
            self._last_amps_change_at = now
        if (
            reason == "decrease"
            and self.config.ceiling_hold > timedelta(0)
            and lease.amps == AMPS_MAX
            and amps >= CEILING_BAND_MIN_AMPS
        ):
            self._ceiling_hold_until = now + self.config.ceiling_hold
        _LOGGER.debug("Schedule write for %s at %d A", reason, amps)
        await self._write(now, [window], Purpose.LEASE, new_lease, strict=True)

    async def _release(self, now: datetime, reason: str) -> None:
        journal = self._journal
        _LOGGER.info("Restoring the owner's charging schedule (%s)", reason)
        self._release_reason = reason
        if journal.release_seq is None:
            journal.release_seq = journal.request_seq
        if journal.original is None:
            self._clear_ownership()
            await self._save()
            return
        await self._write(now, journal.original, Purpose.RESTORE, None, strict=False)

    def _clear_ownership(self) -> None:
        """Return to idle, keeping history, archive, renewal and newer requests.

        The controller's request is also kept through an away release while the
        car is still plugged in (often a location glitch): it may not repeat it.
        """
        journal = self._journal
        hand_back = journal.release_hand_back and journal.release_requested is not None
        newer_request = (
            journal.release_seq is not None and journal.request_seq != journal.release_seq
        ) or (
            self._release_reason == "away"
            and journal.release_requested is None
            and self.last_observation.plugged is True
        )
        self._release_reason = None
        journal.phase = Phase.IDLE
        journal.original = None
        journal.original_fp = None
        journal.owned_fp = None
        journal.pending = None
        journal.attempts = []
        journal.lease = None
        journal.fault = None
        journal.unapplied_writes = 0
        journal.release_requested = None
        journal.release_hand_back = False
        journal.release_seq = None
        if not newer_request:
            journal.command_received = False
            journal.desired_charge = None
        journal.handed_back = hand_back and self.last_observation.plugged is not False

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
        attempt = Attempt(fingerprint(normalized), journal.owned_fp, purpose, lease, now)
        # Intent first: if the process dies after sending, the journal knows.
        journal.phase = Phase.WRITING
        journal.pending = attempt
        journal.attempts = [*journal.attempts, attempt][-self.config.attempt_history :]
        if purpose is Purpose.LEASE and not any(
            attempt.target.matches(state) for state in journal.written_states
        ):
            journal.written_states = [*journal.written_states, attempt.target][
                -self.config.adapter_history :
            ]
        try:
            await self._save()
        except JournalSaveError:
            self._withdraw(attempt)  # nothing was sent
            raise
        try:
            current = await self._read_fp(now)
        except ScheduleApiError:
            self._withdraw(attempt)  # nothing was sent
            await self._save()
            raise
        if current is None:
            return
        outstanding = [a for a in self._outstanding() if a is not attempt]
        if purpose is Purpose.RESTORE and current.matches(attempt.target) and not outstanding:
            self._confirm(attempt)
            await self._save()
            return
        if not current.matches(journal.owned_fp):
            landed = next((a for a in reversed(outstanding) if current.matches(a.target)), None)
            if landed is None:
                self._fault(
                    now,
                    "external_edit",
                    "The charging schedule changed outside this adapter; it was not "
                    "overwritten. Check the Rivian app, then clear the fault.",
                )
                await self._save()
                return
            # An earlier write landed late; it is ours, so carry on from it.
            self._adopt(landed)
            if journal.phase is not Phase.OWNED:
                await self._save()
                return
            attempt.previous = current
            journal.phase = Phase.WRITING
            journal.pending = attempt
        if current.matches(attempt.target) and (purpose is Purpose.LEASE or not outstanding):
            # Already in place. A restore with writes still outstanding is sent
            # anyway, so it is the newest write Rivian receives.
            self._confirm(attempt)
            await self._save()
            return
        # Send immediately after the guard read, with nothing awaited in between.
        self._last_write_at = now
        attempt.outcome = "sent"
        error: ScheduleApiError | None = None
        try:
            await self._api.set_schedules(to_provider_input(normalized))
        except AuthError, RateLimitedError:
            # Refused before it was processed: withdraw without counting it.
            attempt.outcome = "rejected"
            self._withdraw(attempt)
            await self._save()
            raise
        except WriteRejectedError as err:
            attempt.outcome = "invalid" if err.invalid_input else "rejected"
            error = err
        except ScheduleApiError as err:
            attempt.outcome = "ambiguous"
            error = err
        if error is not None:
            self._record_error(now, error)
        # Record that it was sent, so a restart before it settles still knows.
        await self._save()
        await self._resolve_pending(now)
        if error is not None and journal.phase is not Phase.FAULT:
            raise error  # back off before the next write

    async def _resolve_pending(self, now: datetime) -> None:
        journal = self._journal
        pending = journal.pending
        if pending is None:
            journal.phase = Phase.OWNED if journal.original is not None else Phase.IDLE
            await self._save()
            return
        current = await self._read_fp(now)
        if current is None:
            return
        others = [a for a in self._outstanding() if a is not pending]
        landed = next((a for a in reversed(others) if current.matches(a.target)), None)
        # A restore sent only because earlier writes are outstanding: Rivian
        # already showed the owner's list, so a matching read proves nothing.
        unproven_restore = (
            pending.purpose is Purpose.RESTORE
            and pending.previous is not None
            and pending.previous.matches(pending.target)
            and bool(others)
        )
        if pending.outcome == "unsent":
            # Saved as intent, then the process stopped before the outcome was
            # saved: it may have been sent. Keep it so a late landing is ours,
            # also when an earlier write is adopted now.
            pending.outcome = "ambiguous"
            if current.matches(pending.target) and not unproven_restore:
                self._confirm(pending)
            elif landed is not None:
                self._adopt(landed)
            else:
                self._withdraw(pending, keep=True)
        elif current.matches(pending.target) and not unproven_restore:
            self._confirm(pending)
        elif current.matches(pending.target):
            if pending.outcome == "invalid":
                self._restore_rejected(now)
            elif pending.outcome != "sent":
                self._withdraw(pending)  # the restore itself failed: send it again
            elif now - pending.created_at < self.config.settle_delay:
                return  # read again later, in case an earlier write lands
            else:
                # Keep a copy: an earlier write could still land after this.
                journal.archived_original = journal.original
                self._confirm(pending)
        elif pending.previous is not None and current.matches(pending.previous):
            if pending.outcome in ("rejected", "invalid"):
                if pending.purpose is Purpose.RESTORE and pending.outcome == "invalid":
                    self._restore_rejected(now)
                else:
                    self._count_unapplied(now, pending, "write_rejected")
            elif now - pending.created_at < self.config.settle_delay:
                return  # stay pending; read again on a later tick
            else:
                self._count_unapplied(now, pending, "write_not_applied")
        elif landed is not None:
            self._adopt(landed)
        elif pending.purpose is Purpose.RESTORE and current.matches(journal.original_fp):
            self._confirm(pending)
        else:
            self._fault(
                now,
                "conflict_after_write",
                "The schedule read back after a write matches neither the write nor the "
                "previous state; it was not overwritten.",
            )
        await self._save()

    def _count_unapplied(self, now: datetime, attempt: Attempt, code: str) -> None:
        journal = self._journal
        self._withdraw(attempt, keep=attempt.outcome in ("sent", "ambiguous"))
        journal.unapplied_writes += 1
        _LOGGER.warning(
            "Schedule write not applied (%d of %d)",
            journal.unapplied_writes,
            self.config.max_unapplied_writes,
        )
        if journal.unapplied_writes >= self.config.max_unapplied_writes:
            self._fault(
                now,
                code,
                "Rivian repeatedly rejected the schedule write."
                if code == "write_rejected"
                else "Rivian repeatedly applied no schedule change.",
            )

    def _restore_rejected(self, now: datetime) -> None:
        self._fault(
            now,
            "restore_rejected",
            "Rivian refused the owner's schedule list when it was written back. Some "
            "accounts return entries that Rivian will not accept back as one list "
            "(upstream PR #308). The adapter's window is still on the car: recreate the "
            "owner's schedule in the Rivian app, then clear the fault.",
        )

    def _withdraw(self, attempt: Attempt, *, keep: bool = False) -> None:
        """Drop a pending write that did not take effect.

        ``keep`` leaves it in the attempt list so it is still recognized if it
        lands late (only for writes that reached Rivian without a definite no).
        """
        journal = self._journal
        if journal.pending is attempt:
            journal.pending = None
        if not keep:
            journal.attempts = [a for a in journal.attempts if a is not attempt]
        if journal.phase is Phase.WRITING:
            journal.phase = Phase.OWNED if journal.original is not None else Phase.IDLE

    def _outstanding(self) -> list[Attempt]:
        """Writes that reached Rivian and could still land."""
        return [a for a in self._journal.attempts if a.outcome in ("sent", "ambiguous")]

    def _confirm(self, attempt: Attempt) -> None:
        journal = self._journal
        journal.pending = None
        journal.unapplied_writes = 0
        journal.attempts = [a for a in journal.attempts if a is not attempt]
        if attempt.purpose is Purpose.RESTORE:
            self._clear_ownership()
            _LOGGER.info("Owner's charging schedule restored")
        else:
            journal.owned_fp = attempt.target
            journal.phase = Phase.OWNED
            journal.lease = attempt.lease

    def _adopt(self, attempt: Attempt) -> None:
        """Treat a late-landing earlier write as the verified state."""
        journal = self._journal
        _LOGGER.info("An earlier schedule write landed late; adopting it")
        if attempt.purpose is Purpose.RESTORE:
            self._clear_ownership()
            return
        journal.owned_fp = attempt.target
        journal.lease = attempt.lease
        journal.phase = Phase.OWNED
        journal.pending = None
        journal.attempts = [a for a in journal.attempts if a is not attempt]

    async def _read_fp(self, now: datetime) -> Fingerprint | None:
        current = await self._api.get_schedules()
        self.provider_schedule_count = len(current) if isinstance(current, list) else None
        try:
            return fingerprint(normalize_schedules(current, strict=False))
        except (TypeError, ValueError) as err:
            self._fault(now, "unsupported_schedule", f"Unexpected provider schedule: {err}")
            await self._save()
            return None

    # Operator actions -----------------------------------------------------

    async def async_clear_fault(self, now: datetime) -> bool:
        """Resume after a fault if the provider holds a state this adapter knows."""
        async with self._lock:
            now = now.astimezone(UTC)
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
                current_fp = fingerprint(normalize_schedules(current, strict=False))
            except TypeError, ValueError:
                return False
            landed = next(
                (a for a in reversed(journal.attempts) if current_fp.matches(a.target)), None
            )
            if current_fp.matches(journal.owned_fp):
                journal.phase = Phase.OWNED
                journal.pending = None
            elif landed is not None:
                self._adopt(landed)
            elif current_fp.matches(journal.original_fp):
                # The owner's schedule is already back; nothing left to restore.
                self._clear_ownership()
            else:
                return False
            journal.fault = None
            journal.unapplied_writes = 0
            self._backoff_until = None
            await self._save()
            return True

    async def async_force_restore(self, now: datetime) -> bool:
        """While faulted, write the owner's snapshot whatever Rivian holds now.

        An explicit operator action for when the provider state is unknown.
        An archived snapshot (from an earlier abandon) is used only while Rivian
        holds a window this adapter wrote, so a stale archive never replaces a
        list the owner has since set up. Returns True once a readback shows the
        owner's list.
        """
        async with self._lock:
            now = now.astimezone(UTC)
            journal = self._journal
            if journal.phase is not Phase.FAULT:
                return False
            target = journal.original
            if target is None:
                target = journal.archived_original
                if target is None:
                    return False
                current = await self._api.get_schedules()
                try:
                    current_list = normalize_schedules(current, strict=False)
                except TypeError, ValueError:
                    return False
                current_fp = fingerprint(current_list)
                if not (
                    any(current_fp.matches(state) for state in journal.written_states)
                    or self._looks_like_adapter_window(current_list)
                ):
                    return False
            normalized = normalize_schedules(target, strict=False)
            target_fp = fingerprint(normalized)
            attempt = Attempt(target_fp, None, Purpose.RESTORE, None, now, outcome="sent")
            journal.pending = attempt
            journal.attempts = [*journal.attempts, attempt][-self.config.attempt_history :]
            await self._save()
            self._last_write_at = now
            try:
                await self._api.set_schedules(to_provider_input(normalized))
            except AuthError, RateLimitedError:
                self._withdraw(attempt)
                await self._save()
                raise
            except ScheduleApiError as err:
                self._record_error(now, err)
            try:
                current = await self._api.get_schedules()
                verified = fingerprint(normalize_schedules(current, strict=False)).matches(
                    target_fp
                )
            except TypeError, ValueError:
                verified = False
            except ScheduleApiError:
                journal.pending = None
                await self._save()
                raise
            if not verified:
                journal.pending = None
                journal.fault = Fault(
                    "force_restore_unverified",
                    "The forced restore could not be verified; check the Rivian app.",
                    now,
                )
                await self._save()
                return False
            journal.archived_original = None
            journal.release_requested = journal.release_requested or "force restore"
            journal.release_hand_back = True
            self._clear_ownership()
            await self._save()
            return True

    async def async_abandon(self) -> bool:
        """While faulted, forget ownership without writing.

        The owner's snapshot is archived, not deleted, so it can still be shown
        or force-restored. Returns False outside a fault.
        """
        async with self._lock:
            journal = self._journal
            if journal.phase is not Phase.FAULT:
                return False
            if journal.original is not None:
                journal.archived_original = journal.original
            journal.release_seq = journal.request_seq
            self._clear_ownership()
            await self._save()
            return True

    async def async_read_provider(self) -> dict[str, Any]:
        """Read the provider list for inventory and trial evidence, without coordinates."""
        async with self._lock:
            current = await self._api.get_schedules()
            schedules = normalize_schedules(current, strict=False)
            current_fp = fingerprint(schedules)
            self.provider_schedule_count = len(schedules)
            return {
                "count": len(schedules),
                "fingerprint": current_fp.key[:12],
                "written_by_schedule_control": any(
                    current_fp.matches(state) for state in self._journal.written_states
                ),
                "schedules": summarize_schedules(schedules),
            }

    # Reporting ------------------------------------------------------------

    def state(self, now: datetime) -> ControlState:
        """Summarize the adapter's state."""
        now = now.astimezone(UTC)
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
        if journal.handed_back:
            return ControlState.HANDED_BACK
        if self.config.dry_run:
            return ControlState.DRY_RUN
        if journal.command_received and not self.last_observation.eligible:
            return ControlState.WAITING_FOR_VEHICLE
        return ControlState.IDLE

    def snapshot(self, now: datetime) -> dict[str, Any]:
        """Diagnostic attributes; no coordinates, hashes of coordinates, or identifiers."""
        journal = self._journal
        lease = journal.lease
        return {
            "phase": journal.phase.value,
            "dry_run": self.config.dry_run,
            "renewal_enabled": journal.renewal_enabled,
            "handed_back": journal.handed_back,
            "desired_charge": journal.desired_charge,
            "requested_amps": journal.requested_amps,
            "lease_active": lease.active if lease else None,
            "lease_amps": lease.amps if lease else None,
            "lease_written_at": _iso(lease.written_at) if lease else None,
            "lease_expires_at": _iso(lease.expires_at) if lease else None,
            "owner_schedule_count": len(journal.original) if journal.original is not None else None,
            "provider_schedule_count": self.provider_schedule_count,
            "owned_fingerprint": journal.owned_fp.key[:12] if journal.owned_fp else None,
            "pending_purpose": journal.pending.purpose.value if journal.pending else None,
            "unverified_writes": len(journal.attempts),
            "unapplied_writes": journal.unapplied_writes,
            "release_requested": journal.release_requested,
            "archived_owner_schedule_count": (
                len(journal.archived_original) if journal.archived_original is not None else None
            ),
            "fault_code": journal.fault.code if journal.fault else None,
            "fault_message": journal.fault.message if journal.fault else None,
            "last_error": self.last_error,
            "last_error_at": _iso(self.last_error_at),
            "last_write_at": _iso(self._last_write_at),
            "backoff_until": _iso(self._backoff_until),
            "observation": asdict(self.last_observation),
            "start_blocked_by": self._start_blocked_by(),
            "request_unconfirmed": self._request_unconfirmed,
            "request_held": self._request_held and journal.command_received,
            "dry_run_target": self.dry_run_target,
        }

    # Helpers --------------------------------------------------------------

    def _dry_run_target(self, now: datetime) -> dict[str, Any]:
        journal = self._journal
        window = lease_window(
            now.astimezone(self.config.time_zone),
            active=journal.desired_charge is True,
            lease_minutes=self.config.lease_minutes,
            skew_minutes=self.config.clock_skew_minutes,
            amps=applied_amps(journal.requested_amps) if journal.desired_charge else AMPS_MIN,
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

    @staticmethod
    def _since(moment: datetime | None, now: datetime) -> timedelta:
        return timedelta.max if moment is None else now - moment

    def _may_extend(self, obs: Observation) -> bool:
        """Whether a lease may be started, renewed or increased now."""
        return obs.eligible and obs.controller_alive is not False and self._journal.renewal_enabled

    def _start_blocked_by(self) -> list[str]:
        """Why charging cannot be started or extended (for diagnostics).

        The controller being down is always reported; the rest only while
        charging is requested.
        """
        obs = self.last_observation
        reasons = []
        if self._journal.desired_charge is True:
            if obs.plugged is None or obs.home is None:
                reasons.append("vehicle_state_unknown")
            elif not obs.eligible:
                reasons.append("vehicle_not_at_charger")
        if obs.controller_alive is False:
            reasons.append("controller_unavailable")
        if self._journal.desired_charge is True and not self._journal.renewal_enabled:
            reasons.append("renewal_off")
        return reasons

    def _track_liveness(self, now: datetime, obs: Observation) -> None:
        """Time how long the controller has looked down; its request is unconfirmed from then."""
        if obs.controller_alive is True:
            self._alive_seen = True
        if obs.controller_alive is not False:
            self._down_since = None
            self._up_since = self._up_since or now
            return
        self._up_since = None
        if self._down_since is None:
            self._down_since = now
            if self._epoch is None:
                self._epoch, self._epoch_calls = "outage", self._controller_calls
                self._epoch_held = False

    @property
    def _request_unconfirmed(self) -> bool:
        """The controller's request is from before a restart or an outage, not yet settled."""
        return self._epoch is not None and self._journal.command_received

    @property
    def _request_held(self) -> bool:
        """The controller looked down long enough that its unconfirmed request is held."""
        return self._epoch is not None and self._epoch_held

    async def _settle_request(self, now: datetime, obs: Observation) -> None:
        """Confirm or drop a request the controller made before a restart or an outage.

        The controller may have restarted meanwhile; it then asks again if it
        still wants charging, so its old request is stale. Any call since shows
        it is running. While it looks down for the grace period (one
        unavailable reading is not enough), the request is held: no window
        covers now. Once it is up, its charging state decides. Without one, a
        request is kept only if the controller never looked down for the grace
        period and, after a restart, the lease is still live.
        """
        journal = self._journal
        if self._epoch is None:
            return
        if not journal.command_received or self._controller_calls != self._epoch_calls:
            self._epoch = None
            return
        if obs.controller_alive is False:
            grace = (
                self.config.liveness_grace
                if self._alive_seen
                else max(self.config.liveness_grace, self.config.liveness_startup_grace)
            )
            if (
                not self._epoch_held
                and self._down_since is not None
                and now - self._down_since >= grace
            ):
                self._epoch_held = True
                _LOGGER.info("The controller looks down; its earlier charging request is on hold")
            return
        if obs.controller_charging is True or (
            obs.controller_charging is None
            and not self._epoch_held
            and (self._epoch == "outage" or self._lease_live(now))
        ):
            self._epoch = None
            return
        if (
            obs.controller_charging is None
            and self._up_since is not None
            and now - self._up_since < CHARGING_STATE_WAIT
        ):
            return
        _LOGGER.info(
            "Dropping the controller's charging request from before it was last seen running"
        )
        self._epoch = None
        journal.command_received = False
        journal.desired_charge = None
        journal.request_seq += 1
        await self._save()

    def _lease_live(self, now: datetime) -> bool:
        lease = self._journal.lease
        return (
            lease is not None
            and lease.active
            and lease.expires_at is not None
            and lease.expires_at > now
        )

    def _looks_like_adapter_window(self, schedules: list[dict[str, Any]]) -> bool:
        """A single short single-day window at home, as this adapter writes."""
        if len(schedules) != 1:
            return False
        schedule = schedules[0]
        lease, skew = self.config.lease_minutes, self.config.clock_skew_minutes
        return (
            schedule["enabled"] is True
            and len(schedule["weekDays"]) == 1
            and schedule["duration"] in (lease, lease + 2 * skew)
            and _distance_km(schedule["location"], self.config.home_location) <= HOME_MATCH_KM
        )

    def _fault(self, now: datetime, code: str, message: str) -> None:
        journal = self._journal
        journal.phase = Phase.FAULT
        journal.pending = None
        journal.fault = Fault(code, message, now)
        self._release_reason = None  # a release resumes only by an operator action
        _LOGGER.warning("Rivian schedule control fault %s: %s", code, message)

    def _record_error(self, now: datetime, err: Exception) -> None:
        self.last_error = str(err)
        self.last_error_at = now
        _LOGGER.warning("Rivian schedule request failed: %s", err)

    async def _save_request(self) -> None:
        """Save a request, raising if that fails.

        The request is not undone: saves queued behind this one already hold
        it, and the next successful save stores it. Until then an unchanged
        request is saved again rather than acknowledged unsaved.
        """
        try:
            await self._save()
        except JournalSaveError:
            self._request_unsaved = True
            raise

    async def _save(self) -> None:
        try:
            await self._store.async_save(self._journal.to_dict())
        except Exception as err:
            raise JournalSaveError(
                f"Could not save the adapter journal ({type(err).__name__}); nothing was sent"
            ) from err
        self._request_unsaved = False
