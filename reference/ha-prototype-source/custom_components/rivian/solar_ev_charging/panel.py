"""Native Home Assistant panel for solar EV charging."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from homeassistant.components import http
from homeassistant.components.frontend import async_remove_panel
from homeassistant.components.panel_custom import (
    async_register_panel as _register_panel,
)
from homeassistant.core import HomeAssistant

from ..const import VERSION

_LOGGER = logging.getLogger(__name__)
_FRONTEND_DIR = Path(__file__).with_name("frontend")
_STATIC_URL = "/rivian_solar_static"
_PANEL_PATH = "solar-charging"
_STORAGE_KEY = "rivian_solar_panel"


def _state(hass: HomeAssistant) -> dict[str, Any]:
    """Return panel registration state held by Home Assistant."""
    return hass.data.setdefault(
        _STORAGE_KEY,
        {
            "entries": set(),
            "static_registered": False,
            "panel_registered": False,
            "lock": asyncio.Lock(),
        },
    )


async def async_register_panel(
    hass: HomeAssistant, entry_id: str | None = None
) -> None:
    """Register the solar charging panel once, retaining config-entry users."""
    if not _FRONTEND_DIR.is_dir() or not (_FRONTEND_DIR / "solar-panel.js").is_file():
        _LOGGER.warning(
            "Solar charging panel frontend is absent; panel registration skipped"
        )
        return

    state = _state(hass)
    async with state["lock"]:
        entries: set[str] = state["entries"]
        if entry_id:
            entries.add(entry_id)
        try:
            if not state["static_registered"]:
                await hass.http.async_register_static_paths(
                    [http.StaticPathConfig(_STATIC_URL, str(_FRONTEND_DIR), False)]
                )
                state["static_registered"] = True
            if not state["panel_registered"]:
                await _register_panel(
                    hass,
                    frontend_url_path=_PANEL_PATH,
                    webcomponent_name="solar-charging-panel",
                    sidebar_title="Solar charging",
                    sidebar_icon="mdi:solar-power",
                    module_url=f"{_STATIC_URL}/solar-panel.js?v={VERSION}",
                    require_admin=True,
                )
                state["panel_registered"] = True
        except Exception:
            if entry_id:
                entries.discard(entry_id)
            _LOGGER.exception("Could not register the solar charging panel")


async def async_unregister_panel(
    hass: HomeAssistant, entry_id: str | None = None
) -> None:
    """Release one config-entry user of the panel.

    Home Assistant owns removal of registered HTTP paths; keeping the path for the
    lifetime of this HA process avoids a race when entries reload. The ref-count
    is still exposed so callers can safely register/unregister per entry.
    """
    state = hass.data.get(_STORAGE_KEY)
    if not state:
        return
    async with state["lock"]:
        if entry_id:
            state["entries"].discard(entry_id)
        if not state["entries"] and state["panel_registered"]:
            async_remove_panel(hass, _PANEL_PATH)
            state["panel_registered"] = False
