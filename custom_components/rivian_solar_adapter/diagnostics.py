"""Diagnostics for the Rivian Solar Charge Adapter."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data
from homeassistant.util import dt as dt_util

from .runtime import REDACT_KEYS, RivianSolarAdapterConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RivianSolarAdapterConfigEntry
) -> dict[str, Any]:
    """Return diagnostics without credentials, the vehicle ID or any coordinates.

    The journal's schedules (the owner's originals and the archived ones) and
    its fingerprints carry locations; every such key is redacted at any depth.
    """
    runtime = entry.runtime_data
    controller = runtime.controller
    now = dt_util.utcnow()
    return async_redact_data(
        {
            "entry": {
                "title": entry.title,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "control_state": controller.state(now).value,
            "evcc_status": runtime.evcc_status,
            "observation": asdict(runtime.observation()),
            "vehicle_override_active": runtime.override_active,
            "reauth_required": runtime.reauth_required,
            "unexpected_error": runtime.unexpected_error,
            "last_tick_at": runtime.last_tick_at.isoformat() if runtime.last_tick_at else None,
            "controller": controller.snapshot(now),
            "journal": controller.journal.to_dict(),
            "last_provider_read": runtime.last_provider_read,
        },
        REDACT_KEYS,
    )
