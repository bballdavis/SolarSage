"""The executor: turns the planner's command for the car on the charger into service calls.

It runs only while the planner is on and shadow mode is off. A command (``CarCommand.amps``)
of None sends nothing, 0 turns the car's charge switch off, and N > 0 sets its current number
to N and then turns the switch on. Only what differs from the car's standing request is sent:

- Tesla (Tesla Fleet): the charge switch (on while charging) and the charge-current number.
  Every command is billed (and may wake the car), so the executor never repeats a request on
  its own, and sends no start ("on") while the car cannot start: its optional charging sensor
  reads complete, disconnected or no power, its SoC is at its own limit, or it last answered
  a start with "complete" and its plug, SoC and limit have not changed since. A refusal that
  says the car already is where the command would take it ("complete", "is_charging",
  "not_charging") counts as done, not as a failure.
- Rivian (Rivian schedule control): the schedule control's request, read from its control
  state's ``desired_charge`` and ``requested_amps`` attributes when that sensor is mapped,
  else from its switch and number. The schedule control drops a request on unplug, after a
  restart or a planner outage, and the switch then turns off; the executor repeats the
  planner's last command until the schedule control holds it again, except while it ignores
  requests (handed back, fault, waiting for the vehicle). A Rivian is paused only with the
  switch, never with 0 A.

Rate limits on top of the core's (minimum time between changes, largest change): one command
per car per minute, at most ``MAX_SENDS_PER_HOUR`` an hour (a pause ignores the cap), and an
exponential back-off after a failure. A command that could not be sent is retried while the
planner holds; a new command replaces it. After ``STALL_AFTER_FAILURES`` failures in a row a
Tesla gets no more starts or current changes until its plug, SoC or limit changes, or
``STALL_BACKOFF`` has passed; a pause keeps the short back-off. The sends of the last hour
are persisted, so a restart does not reset the cap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import re
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import Context, HomeAssistant, split_entity_id
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_CHARGING_STATE,
    CONF_CONTROL_STATE,
    CONF_CURRENT,
    CONF_KIND,
    CONF_LIMIT,
    CONF_PLUGGED,
    CONF_SOC,
    CONF_SWITCH,
    FAILURE_BACKOFF_MAX,
    KIND_RIVIAN,
    MAX_SENDS_PER_HOUR,
    SCHEDULE_CONTROL_IGNORES,
    SEND_SPACING,
    SERVICE_TIMEOUT,
    STALL_AFTER_FAILURES,
    STALL_BACKOFF,
    TESLA_ALREADY_DONE,
    TESLA_CANNOT_START,
)
from .inputs import available, state_float

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_VALUE = "set_value"
HOUR = timedelta(hours=1)


@dataclass
class CarRuntime:
    """What the planner remembers about one car (persisted)."""

    plugged_since: datetime | None = None  # start of the current session on the charger
    commanded_a: int | None = None  # last command the car accepted this session
    touched: bool = False  # the planner set this car's current since it was last plugged in
    plugged_prev: bool | None = None  # the car's own plug sensor at the previous cycle
    last_sent: datetime | None = None
    retry_a: int | None = None  # a command not sent yet (rate limit or failure)
    failures: int = 0
    retry_at: datetime | None = None
    last_error: str | None = None
    blocked: str | None = None  # why the last command waits
    until: datetime | None = None  # the free hours' end while the car charges through them
    sends: list[datetime] = field(default_factory=list)  # in the last hour
    # A Tesla's [plug, SoC, limit] when it answered a start with "complete" (no start is sent
    # while they stay so), and when it stopped retrying after failures in a row.
    held: list[Any] | None = None
    stalled: list[Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        def iso(t: datetime | None) -> str | None:
            return t.isoformat() if t else None

        return {
            "plugged_since": iso(self.plugged_since),
            "commanded_a": self.commanded_a,
            "touched": self.touched,
            "plugged_prev": self.plugged_prev,
            "last_sent": iso(self.last_sent),
            "retry_a": self.retry_a,
            "failures": self.failures,
            "retry_at": iso(self.retry_at),
            "last_error": self.last_error,
            "until": iso(self.until),
            "sends": [t.isoformat() for t in self.sends],
            "held": self.held,
            "stalled": self.stalled,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CarRuntime:
        def dt(value: Any) -> datetime | None:
            try:
                return datetime.fromisoformat(value) if value else None
            except TypeError, ValueError:
                return None

        commanded = data.get("commanded_a")
        retry = data.get("retry_a")
        sends = [t for t in map(dt, data.get("sends") or []) if t is not None]
        held, stalled = data.get("held"), data.get("stalled")
        return cls(
            plugged_since=dt(data.get("plugged_since")),
            commanded_a=int(commanded) if commanded is not None else None,
            touched=bool(data.get("touched", False)),
            plugged_prev=data.get("plugged_prev"),
            last_sent=dt(data.get("last_sent")),
            retry_a=int(retry) if retry is not None else None,
            failures=int(data.get("failures") or 0),
            retry_at=dt(data.get("retry_at")),
            last_error=data.get("last_error"),
            until=dt(data.get("until")),
            sends=sends,
            held=list(held) if isinstance(held, list) else None,
            stalled=list(stalled) if isinstance(stalled, list) else None,
        )

    def end_session(self) -> None:
        """The car left the charger: the next plug-in is a new session."""
        self.plugged_since = None
        self.commanded_a = None
        self.retry_a = None
        self.blocked = None
        self.until = None
        self.held = None
        self.stalled = None


@dataclass(frozen=True)
class Standing:
    """The request the car (or Rivian schedule control) holds now."""

    charging: bool | None  # True on, False paused, None unknown or no request
    amps: float | None
    schedule_control_state: str | None = None


def standing_request(hass: HomeAssistant, car: Mapping[str, Any]) -> Standing:
    """Read the car's standing request."""
    if car.get(CONF_KIND) == KIND_RIVIAN and car.get(CONF_CONTROL_STATE):
        state = hass.states.get(car[CONF_CONTROL_STATE])
        if available(state):
            assert state is not None
            desired = state.attributes.get("desired_charge")
            amps = state.attributes.get("requested_amps")
            return Standing(
                desired if isinstance(desired, bool) else None,
                float(amps) if isinstance(amps, (int, float)) else None,
                state.state,
            )
    switch = hass.states.get(car[CONF_SWITCH])
    charging: bool | None = None
    if available(switch):
        assert switch is not None
        if switch.state == "on":
            charging = True
        elif car.get(CONF_KIND) != KIND_RIVIAN:
            # A Tesla's switch is off while it does not charge. Rivian schedule control's switch
            # is off both for a pause and for no request at all, so it says nothing then.
            charging = False
    return Standing(charging, state_float(hass.states.get(car[CONF_CURRENT])))


def calls_for(target: int, standing: Standing) -> list[str]:
    """The calls that bring the standing request to ``target`` ("current", "on", "off"),
    in order: the current first, then the switch."""
    if target > 0:
        calls = []
        if standing.amps is None or abs(standing.amps - target) > 0.01:
            calls.append("current")
        if standing.charging is not True:
            calls.append("on")
        return calls
    return [] if standing.charging is False else ["off"]


def condition(hass: HomeAssistant, car: Mapping[str, Any]) -> list[Any]:
    """The car's [plug, SoC, limit], which a Tesla's start guard and its stop after failures
    wait to change."""
    plugged = hass.states.get(car[CONF_PLUGGED])
    return [
        plugged.state if plugged is not None else None,
        state_float(hass.states.get(car[CONF_SOC])),
        state_float(hass.states.get(car[CONF_LIMIT])),
    ]


def changed(old: list[Any], new: list[Any]) -> bool:
    """A value of the condition is known now and differs (going unavailable is no change)."""
    return any(n is not None and o != n for o, n in zip(old, new, strict=False))


_REASONS = re.compile(r"\b(" + "|".join(sorted(set().union(*TESLA_ALREADY_DONE.values()))) + r")\b")


def refusal(err: Exception) -> str | None:
    """The reason Tesla Fleet gave for refusing a command (``command_reason``), if any."""
    placeholders = getattr(err, "translation_placeholders", None) or {}
    reason = placeholders.get("reason")
    if isinstance(reason, str) and reason:
        return reason.strip().lower()
    match = _REASONS.search(str(err).lower())
    return match.group(1) if match else None


class CarExecutor:
    """Sends the planner's commands; one per config entry."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.context = Context()

    def blocked(
        self, rt: CarRuntime, now: datetime, target: int, cond: list[Any] | None = None
    ) -> str | None:
        """Why a command must wait, or None. ``cond`` is a Tesla's condition()."""
        rt.sends = [t for t in rt.sends if now - t < HOUR]
        if (
            rt.stalled is not None
            and cond is not None
            and (target == 0 or changed(rt.stalled, cond))
        ):
            # the car's plug, SoC or limit changed, or a pause (the stop for paid hours, never
            # held back by a stall): try again now
            rt.stalled = None
            rt.retry_at = None
        if rt.retry_at is not None and now < rt.retry_at:
            if rt.stalled is not None:
                return (
                    f"stopped after {rt.failures} failed commands until the car's plug, SoC "
                    f"or limit changes, or {rt.retry_at:%H:%M}"
                )
            return f"waiting until {rt.retry_at:%H:%M} after a failed command"
        if rt.last_sent is not None and now - rt.last_sent < SEND_SPACING:
            return "one command per minute"
        if target > 0 and len(rt.sends) >= MAX_SENDS_PER_HOUR:
            return f"{MAX_SENDS_PER_HOUR} commands in the last hour"
        return None

    def cannot_start(self, car: Mapping[str, Any], rt: CarRuntime, cond: list[Any]) -> str | None:
        """Why a Tesla's start command would do nothing but cost (and wake the car), or None."""
        if rt.held is not None and changed(rt.held, cond):
            rt.held = None
        if eid := car.get(CONF_CHARGING_STATE):
            state = self.hass.states.get(eid)
            if state is not None and state.state in TESLA_CANNOT_START:
                return f"the car reports {state.state}; no start is sent"
        _, soc, limit = cond
        if soc is not None and limit is not None and soc >= limit:
            return "the car is at its charge limit; no start is sent"
        if rt.held is not None:
            return "the car answered complete; no start until its plug, SoC or limit changes"
        return None

    async def async_apply(
        self, car: Mapping[str, Any], rt: CarRuntime, target: int, now: datetime
    ) -> bool:
        """Bring the car to ``target`` A (0 = paused). True when the car holds it now."""
        rt.retry_a = None
        standing = standing_request(self.hass, car)
        calls = calls_for(target, standing)
        if not calls:
            rt.commanded_a = target
            rt.blocked = None
            return True
        billed = car.get(CONF_KIND) != KIND_RIVIAN
        cond = condition(self.hass, car) if billed else None
        if cond is not None and "on" in calls and (why := self.cannot_start(car, rt, cond)):
            # nothing is sent (not even the current): the guard is re-checked every cycle
            rt.retry_a = target
            rt.blocked = why
            return False
        if why := self.blocked(rt, now, target, cond):
            rt.retry_a = target
            rt.blocked = why
            return False
        rt.last_sent = now
        rt.sends.append(now)
        held = False
        for call in calls:
            try:
                await self._async_call(car, call, target)
            except (HomeAssistantError, TimeoutError) as err:
                reason = refusal(err) if billed else None
                if reason is not None and reason in TESLA_ALREADY_DONE.get(call, ()):
                    _LOGGER.debug(
                        "%s answered %s: %s; nothing to do", car.get("name"), call, reason
                    )
                    held = held or reason == "complete"
                    continue
                self._failed(car, rt, target, now, err, cond)
                return False
        rt.failures = 0
        rt.retry_at = None
        rt.stalled = None
        rt.last_error = None
        rt.blocked = None
        rt.commanded_a = target
        if held:
            rt.held = cond
            rt.blocked = "the car answered complete; no start until its plug, SoC or limit changes"
        if "current" in calls:
            rt.touched = True
        return True

    def _failed(
        self,
        car: Mapping[str, Any],
        rt: CarRuntime,
        target: int,
        now: datetime,
        err: Exception,
        cond: list[Any] | None,
    ) -> None:
        rt.failures += 1
        if cond is not None and target > 0 and rt.failures >= STALL_AFTER_FAILURES:
            backoff = STALL_BACKOFF  # billed: stop until the car changes (a pause never stalls)
            rt.stalled = cond
        else:
            backoff = min(FAILURE_BACKOFF_MAX, timedelta(minutes=2 ** (rt.failures - 1)))
        rt.retry_at = now + backoff
        rt.retry_a = target
        rt.last_error = f"{type(err).__name__}: {err}"[:200]
        rt.blocked = f"failed: {rt.last_error}"
        _LOGGER.warning(
            "Command %s A to %s failed (%s); retrying after %s%s",
            target,
            car.get("name"),
            rt.last_error,
            backoff,
            " or once its plug, SoC or limit changes" if rt.stalled is not None else "",
        )

    async def async_reconcile(self, car: Mapping[str, Any], rt: CarRuntime, now: datetime) -> bool:
        """While the planner holds: send a command that is still waiting, or repeat the last
        one to a Rivian whose schedule control dropped it. True when something was sent."""
        if rt.retry_a is not None:
            return await self.async_apply(car, rt, rt.retry_a, now)
        if rt.commanded_a is None or car.get(CONF_KIND) != KIND_RIVIAN:
            return False
        standing = standing_request(self.hass, car)
        if standing.schedule_control_state in SCHEDULE_CONTROL_IGNORES:
            return False
        if standing.schedule_control_state is None and rt.commanded_a == 0:
            return False  # without the control state a pause cannot be told from no request
        if calls_for(rt.commanded_a, standing):
            _LOGGER.debug("Rivian schedule control dropped the request; sending it again")
            return await self.async_apply(car, rt, rt.commanded_a, now)
        return False

    async def async_restore(self, car: Mapping[str, Any], amps: int) -> bool:
        """Set the car's current back to ``amps`` (the Tesla restore at unplug)."""
        if state_float(self.hass.states.get(car[CONF_CURRENT])) == amps:
            return True
        try:
            await self._async_call(car, "current", amps)
        except (HomeAssistantError, TimeoutError) as err:
            _LOGGER.warning("Restoring %s A on %s failed: %s", amps, car.get("name"), err)
            return False
        return True

    async def _async_call(self, car: Mapping[str, Any], call: str, amps: int) -> None:
        if call == "current":
            entity_id = car[CONF_CURRENT]
            state = self.hass.states.get(entity_id)
            top = state.attributes.get("max") if state is not None else None
            value = min(amps, int(top)) if isinstance(top, (int, float)) else amps
            service, data = SERVICE_SET_VALUE, {ATTR_ENTITY_ID: entity_id, "value": value}
        else:
            entity_id = car[CONF_SWITCH]
            service = SERVICE_TURN_ON if call == "on" else SERVICE_TURN_OFF
            data = {ATTR_ENTITY_ID: entity_id}
        domain = split_entity_id(entity_id)[0]
        _LOGGER.debug("Calling %s.%s %s", domain, service, data)
        async with asyncio.timeout(SERVICE_TIMEOUT):
            await self.hass.services.async_call(
                domain, service, data, blocking=True, context=self.context
            )
