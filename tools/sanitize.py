"""Strip what must never leave the owner's machine from Home Assistant data.

Snapshots and logs keep states, numbers and timestamps. They drop coordinates, VINs, tokens,
emails, user ids, pictures and state contexts, and reduce a device tracker's zone to
``home`` / ``not_home`` (a zone name can say where the car is). ``scrub`` works on any
JSON-like value; ``scrub_state`` on one HA state dict.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

REDACTED = "**redacted**"
# keys dropped wherever they appear (compared lower-case, without - and _)
DROP_KEYS = {
    "latitude",
    "longitude",
    "lat",
    "lon",
    "lng",
    "gpsaccuracy",
    "altitude",
    "elevation",
    "location",
    "homelocation",
    "coordinates",
    "address",
    "vin",
    "vehicleid",
    "accesstoken",
    "refreshtoken",
    "usersessiontoken",
    "token",
    "password",
    "email",
    "userid",
    "parentid",
    "context",
    "entitypicture",
    "locationname",
    "internalurl",
    "externalurl",
}
VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
JWT = re.compile(r"\beyJ[\w-]{6,}\.[\w-]{6,}\.[\w-]{6,}\b")
COORD_PAIR = re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}")
TRACKER_STATES = ("home", "not_home", "unavailable", "unknown")


def _norm(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


def scrub_text(text: str) -> str:
    for pattern in (JWT, EMAIL, COORD_PAIR):
        text = pattern.sub(REDACTED, text)
    # a VIN has letters and digits; plain 17-digit numbers (timestamps) stay
    return VIN.sub(
        lambda m: REDACTED if not m.group().isdigit() and not m.group().isalpha() else m.group(),
        text,
    )


def scrub(value: Any) -> Any:
    """A copy of ``value`` without sensitive keys or values."""
    if isinstance(value, Mapping):
        return {k: scrub(v) for k, v in value.items() if _norm(str(k)) not in DROP_KEYS}
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def tracker_state(state: Any) -> Any:
    """A device tracker's zone reduced to home / not_home."""
    if isinstance(state, str) and state not in TRACKER_STATES:
        return "not_home"
    return state


def scrub_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """One HA state dict, sanitized; a device tracker keeps no attributes."""
    out = scrub(state)
    eid = str(state.get("entity_id", ""))
    if eid.startswith(("device_tracker.", "person.", "zone.")):
        out["state"] = tracker_state(out.get("state"))
        out["attributes"] = {
            k: v
            for k, v in (out.get("attributes") or {}).items()
            if k in ("friendly_name", "source_type")
        }
    return out
