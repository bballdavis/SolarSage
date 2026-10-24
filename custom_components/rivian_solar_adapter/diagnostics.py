"""Diagnostics for the Rivian Solar Charge Adapter."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    CONF_VEHICLE_ID,
)
from .runtime import RivianSolarAdapterConfigEntry

TO_REDACT_ENTRY = {
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
    CONF_USER_SESSION_TOKEN,
    CONF_VEHICLE_ID,
}
# Every schedule in the journal (the owner's originals included) has a location.
TO_REDACT_JOURNAL = {"location", "latitude", "longitude"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RivianSolarAdapterConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    runtime = entry.runtime_data
    controller = runtime.controller
    now = dt_util.now()
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT_ENTRY),
            "options": async_redact_data(dict(entry.options), TO_REDACT_ENTRY),
        },
        "control_state": controller.state(now).value,
        "evcc_status": runtime.evcc_status,
        "observation": asdict(runtime.observation()),
        "reauth_required": runtime.reauth_required,
        "controller": async_redact_data(controller.snapshot(now), TO_REDACT_JOURNAL),
        "journal": async_redact_data(controller.journal.to_dict(), TO_REDACT_JOURNAL),
        "last_provider_read": async_redact_data(runtime.last_provider_read, TO_REDACT_JOURNAL),
    }
