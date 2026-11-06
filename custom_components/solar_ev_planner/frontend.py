"""Serve the planner's dashboard card from the integration, so it needs no Lovelace resource.

The card lives in ``www/``; Home Assistant serves it at ``/solar_ev_planner/`` and loads it on
every dashboard (``add_extra_js_url``). The version in the URL changes with the card, so
browsers fetch the new one after an update.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_BASE = f"/{DOMAIN}"
CARD_FILE = "solar-ev-planner-card.js"
CARD_VERSION = "0.1.0"
_REGISTERED = f"{DOMAIN}_frontend"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve and load the card, once per Home Assistant run."""
    if hass.data.get(_REGISTERED) or hass.http is None or "frontend" not in hass.config.components:
        return
    try:
        from homeassistant.components import frontend  # noqa: PLC0415
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

        path = str(Path(__file__).parent / "www")
        await hass.http.async_register_static_paths([StaticPathConfig(URL_BASE, path, True)])
        frontend.add_extra_js_url(hass, f"{URL_BASE}/{CARD_FILE}?v={CARD_VERSION}")
        hass.data[_REGISTERED] = True
    except Exception:  # noqa: BLE001 - the planner runs without its card
        _LOGGER.exception("Could not register the Solar EV planner card")
