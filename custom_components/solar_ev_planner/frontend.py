"""Serve the planner's dashboard card from the integration, so it needs no Lovelace resource.

The card lives in ``www/``; Home Assistant serves it at ``/solar_ev_planner/`` and loads it on
every dashboard (``add_extra_js_url``). The URL carries a hash of the card's content, so a
browser fetches the new card after any update and keeps the cached one otherwise.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_BASE = f"/{DOMAIN}"
CARD_FILE = "solar-ev-planner-card.js"
_REGISTERED = f"{DOMAIN}_frontend"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve and load the card, once per Home Assistant run."""
    if hass.data.get(_REGISTERED) or hass.http is None or "frontend" not in hass.config.components:
        return
    try:
        from homeassistant.components import frontend  # noqa: PLC0415
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

        www = Path(__file__).parent / "www"
        version = await hass.async_add_executor_job(card_version, www / CARD_FILE)
        await hass.http.async_register_static_paths([StaticPathConfig(URL_BASE, str(www), True)])
        frontend.add_extra_js_url(hass, f"{URL_BASE}/{CARD_FILE}?v={version}")
        hass.data[_REGISTERED] = True
    except Exception:  # noqa: BLE001 - the planner runs without its card
        _LOGGER.exception("Could not register the Solar EV planner card")


def card_version(path: Path) -> str:
    """The first 10 hex digits of the card's SHA-256."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]
