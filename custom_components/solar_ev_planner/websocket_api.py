"""Websocket commands for the dashboard card.

``solar_ev_planner/dashboard`` answers once with ``dashboard.snapshot``;
``solar_ev_planner/subscribe`` answers, then sends a new snapshot after every planning cycle
and every settings change until the card unsubscribes. Both read only: the card changes
settings through the planner's own entities (switch, select and number services), so the
usual permissions and logbook apply.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .coordinator import PlannerCoordinator
from .dashboard import snapshot

NOT_LOADED = "not_loaded"


def _coordinator(hass: HomeAssistant) -> PlannerCoordinator | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the card's commands (once per Home Assistant run)."""
    websocket_api.async_register_command(hass, ws_dashboard)
    websocket_api.async_register_command(hass, ws_subscribe)


@websocket_api.websocket_command({"type": f"{DOMAIN}/dashboard"})
@callback
def ws_dashboard(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The planner's state now."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], NOT_LOADED, "The Solar EV planner is not loaded")
        return
    connection.send_result(msg["id"], snapshot(hass, coordinator))


@websocket_api.websocket_command({"type": f"{DOMAIN}/subscribe"})
@callback
def ws_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The planner's state now and after every cycle or settings change."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], NOT_LOADED, "The Solar EV planner is not loaded")
        return

    @callback
    def _send() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], snapshot(hass, coordinator)))

    connection.subscriptions[msg["id"]] = coordinator.async_add_listener(_send)
    connection.send_result(msg["id"])
    _send()
