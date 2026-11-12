"""Websocket commands for the dashboard card.

``solar_ev_planner/dashboard`` answers once with ``dashboard.snapshot``;
``solar_ev_planner/subscribe`` answers, then sends a new snapshot after every planning cycle
and every settings change until the card unsubscribes. Both read only: the card changes
settings through the planner's own entities (switch, select and number services), so the
usual permissions and logbook apply. ``solar_ev_planner/refresh_schedules`` (admin) re-reads
the Schedule helpers at once and plans again, after the card has edited the free hours
(Home Assistant's own ``schedule/update``, also admin only).
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
    planners = [entry for entry in entries if "vehicle_id" not in entry.data]
    if not planners:
        return None
    # Two planner entries should not exist, but an older install may have left one: show the
    # one that is actually configured (cars and price windows), not an empty duplicate.
    return max(planners, key=lambda entry: len(entry.subentries)).runtime_data


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the card's commands (once per Home Assistant run)."""
    websocket_api.async_register_command(hass, ws_dashboard)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_refresh_schedules)


@websocket_api.websocket_command({"type": f"{DOMAIN}/dashboard"})
@callback
def ws_dashboard(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The planner's state now."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], NOT_LOADED, "The SolarSage is not loaded")
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
        connection.send_error(msg["id"], NOT_LOADED, "The SolarSage is not loaded")
        return

    @callback
    def _send() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], snapshot(hass, coordinator)))

    connection.subscriptions[msg["id"]] = coordinator.async_add_listener(_send)
    connection.send_result(msg["id"])
    _send()


@websocket_api.websocket_command({"type": f"{DOMAIN}/refresh_schedules"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_refresh_schedules(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Re-read the free hours and price windows now, rather than within 15 minutes."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], NOT_LOADED, "The SolarSage is not loaded")
        return
    await coordinator.async_refresh_schedules()
    connection.send_result(msg["id"])
