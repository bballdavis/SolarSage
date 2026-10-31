"""How the Tesla (Fleet entities) and Rivian schedule control answer commands, in a FakeHass.

A crude stand-in for the real integrations, enough for attended_test.py: the Wall Connector's
current follows the commanded current, the schedule control moves through its control states,
and the lease renews and lapses on a fake clock. Nothing here reaches a real system.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fake_hass import FakeHass
from synthetic import E, schedule_control_attrs


def applied(amps: float) -> int:
    """The schedule control's rounding: even 8-48 A."""
    a = int(min(48, max(8, amps)))
    return a - a % 2


class FakeCars:
    """Install with ``FakeCars(fake, clock)``; ``clock()`` is the test's fake time (s)."""

    def __init__(self, fake: FakeHass, clock: Callable[[], float]) -> None:
        self.fake = fake
        self.clock = clock
        self.renewal = True
        self.lease_t: float | None = None  # fake time of the last lease write
        self.lapsed_t: float | None = None
        self.read_count = 0
        self.owner_fp = "a1b2c3d4e5f6"  # the owner's list as a read shows it
        fake.on_service = self.on_service
        fake.on_poll = self.on_poll

    # ------------------------------------------------------------------ helpers

    def st(self, key: str) -> Any:
        return self.fake.states[E[key]]["state"]

    def set(self, key: str, value: Any, **attrs: Any) -> None:
        old = self.fake.states.get(E[key], {}).get("attributes", {})
        self.fake.set_state(E[key], value, {**old, **attrs} if attrs else None)

    def wall(self, amps: float) -> None:
        self.set("wall_current", amps)
        self.set("wall", round(amps * 0.24, 2))
        self.set("wall_contactor", "on" if amps > 0 else "off")

    def control(self, state: str, **attrs: Any) -> None:
        cur = dict(self.fake.states[E["r_state"]]["attributes"])
        cur.update(attrs)
        self.fake.set_state(E["r_state"], state, cur)

    def a(self, key: str) -> Any:
        return self.fake.states[E["r_state"]]["attributes"].get(key)

    def write_lease(self) -> None:
        amps = applied(self.a("requested_amps") or 8)
        now = datetime.now(UTC)
        self.lease_t = self.clock()
        self.lapsed_t = None
        self.control(
            "lease_active",
            desired_charge=True,
            lease_active=True,
            lease_amps=amps,
            lease_written_at=f"{now.isoformat()}#{self.clock():.0f}",
            lease_expires_at=(now + timedelta(minutes=25)).isoformat(),
            last_write_at=f"{self.clock():.0f}",
        )
        self.wall(amps)

    # ------------------------------------------------------------------ services

    def on_service(self, domain: str, service: str, data: dict, target: dict | None) -> None:
        eid = (target or {}).get("entity_id") or data.get("entity_id")
        if eid == E["t_current"]:
            self.set("t_current", data["value"])
            if self.st("t_switch") == "on":
                self.wall(float(data["value"]))
        elif eid == E["t_switch"]:
            on = service == "turn_on"
            self.set("t_switch", "on" if on else "off")
            self.wall(float(self.st("t_current")) if on else 0)
        elif eid == E["r_current"]:
            self.set("r_current", data["value"])
            self.control(self.st("r_state"), requested_amps=data["value"])
            if self.a("desired_charge"):
                self.write_lease()
        elif eid == E["r_switch"]:
            if service == "turn_on":
                self.set("r_switch", "on")
                self.write_lease()
            else:
                self.set("r_switch", "off")
                self.control("paused", desired_charge=False, lease_active=False)
                self.wall(0)
        elif eid == E["r_renewal"]:
            self.renewal = service == "turn_on"
            self.set("r_renewal", "on" if self.renewal else "off")
            if self.renewal and self.a("desired_charge"):
                self.write_lease()
        elif eid == E["r_restore"]:
            self.set("r_switch", "off")
            self.control("handed_back", desired_charge=None, handed_back=True, lease_active=False)
            self.wall(0)
        elif eid == E["r_read"]:
            self.read_count += 1
            owner = self.st("r_state") in ("idle", "handed_back")  # the owner's list is back
            self.control(
                self.st("r_state"),
                last_provider_read_at=f"read-{self.read_count}",
                provider_schedule_count=2,
                last_provider_read={
                    "count": 2,
                    "fingerprint": self.owner_fp if owner else "0123456789ab",
                    "written_by_schedule_control": self.read_count > 1,
                },
            )

    def on_poll(self) -> None:
        """Time passes: renewals while charging, a lapse when renewal is off."""
        if self.lease_t is None or self.st("r_state") not in ("lease_active", "lease_lapsed"):
            return
        age = self.clock() - self.lease_t
        if self.renewal and age >= 12 * 60:
            self.write_lease()
        elif not self.renewal and self.st("r_state") == "lease_active" and age >= 25 * 60:
            self.lapsed_t = self.clock()
            self.control("lease_lapsed", lease_active=False)
            self.wall(0)
        elif self.lapsed_t is not None and self.clock() - self.lapsed_t >= 60:
            self.control("paused")
            self.lapsed_t = None


def live_schedule_control(fake: FakeHass) -> None:
    """The schedule control out of dry run, idle, with the Rivian on the charger, not charging."""
    fake.set_state(E["r_state"], "idle", schedule_control_attrs(dry_run=False))
    fake.set_state(E["wall_current"], 0)
    fake.set_state(E["wall_contactor"], "off")


def tesla_on_charger(fake: FakeHass) -> None:
    """The Tesla plugged in at home instead of the Rivian."""
    fake.set_state(E["r_plugged"], "off")
    fake.set_state(E["t_plugged"], "on")
    fake.set_state(E["t_location"], "home")
    fake.set_state(E["wall_connected"], "on")
    fake.set_state(E["wall_current"], 0)
    fake.set_state(E["wall_contactor"], "off")
