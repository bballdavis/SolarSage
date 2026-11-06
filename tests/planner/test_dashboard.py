"""The dashboard card's websocket snapshot: complete, JSON-safe, and pushed after changes."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.solar_ev_planner.dashboard import snapshot

from .conftest import RIVIAN, TESLA, FakeCars, make_entry, setup_entry, turn


async def test_snapshot_describes_the_cycle(
    site: HomeAssistant, fake_cars: FakeCars, hass_ws_client: WebSocketGenerator
) -> None:
    hass = site
    entry = make_entry(RIVIAN, TESLA)
    await setup_entry(hass, entry, fake_cars)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "solar_ev_planner/dashboard"})
    msg = await client.receive_json()
    assert msg["success"], msg
    snap: dict[str, Any] = msg["result"]

    assert snap["mode"] == "day" and snap["shadow"] is True and snap["active"] is False
    assert snap["car_on_charger"] == "rivian"
    assert snap["healthy"] is True
    # the half-hourly forecast for today (and tomorrow), the battery path, the free hours
    assert len(snap["forecast"]) >= 48
    assert snap["trajectory"] and {"soc", "expected", "car_kw", "price"} <= set(
        snap["trajectory"][0]
    )
    first_free = datetime.fromisoformat(snap["free_windows"][0]["start"])
    assert (first_free.hour, first_free.minute) == (20, 0)
    # every setting the card changes has its entity
    planner = snap["entities"]["planner"]
    assert planner["shadow_mode"] == "switch.solar_ev_planner_shadow_mode"
    assert planner["caution"].startswith("select.")
    assert planner["battery_floor"].startswith("number.")
    rivian = snap["entities"]["cars"]["rivian"]
    assert rivian["planned_current"].startswith("sensor.r1s_planner_")
    assert "restore_current" in snap["entities"]["cars"]["tesla"]
    # the cars, the Tesla spending guard and the sensors for the charts
    cars = {c["car_id"]: c for c in snap["cars"]}
    assert cars["rivian"]["on_charger"] is True and cars["tesla"]["on_charger"] is False
    assert cars["rivian"]["outlook"]["summary"]
    assert snap["spend"]["cap"] > 0
    assert snap["sources"]["pv"] == {
        "entity_id": "sensor.sol_ark_inverter_pv_power",
        "scale": 0.001,
    }
    assert snap["caution_levels"]["conservative"] == 0.9
    json.dumps(snap)  # JSON-safe as is


async def test_subscription_pushes_after_a_setting_changes(
    site: HomeAssistant, fake_cars: FakeCars, hass_ws_client: WebSocketGenerator
) -> None:
    hass = site
    entry = make_entry(RIVIAN)
    await setup_entry(hass, entry, fake_cars)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "solar_ev_planner/subscribe"})
    msg = await client.receive_json()
    assert msg["success"], msg
    msg = await client.receive_json()
    assert msg["type"] == "event" and msg["event"]["shadow"] is True

    await turn(hass, "switch.solar_ev_planner_shadow_mode", False)
    await hass.async_block_till_done()
    while True:
        msg = await client.receive_json()
        if msg["event"]["shadow"] is False:
            break
    assert msg["event"]["active"] is True


async def test_not_loaded_is_an_error(
    planner_hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    hass = planner_hass
    from custom_components.solar_ev_planner import websocket_api  # noqa: PLC0415

    websocket_api.async_register(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "solar_ev_planner/dashboard"})
    msg = await client.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "not_loaded"


async def test_snapshot_before_the_first_cycle(site: HomeAssistant, fake_cars: FakeCars) -> None:
    """A coordinator without data (planning failed from the start) still gives a snapshot."""
    hass = site
    entry = make_entry(RIVIAN)
    coordinator = await setup_entry(hass, entry, fake_cars)
    coordinator.data = None  # type: ignore[assignment]
    snap = snapshot(hass, coordinator)
    assert snap["mode"] is None and snap["trajectory"] == [] and snap["cars"][0]["name"] == "R1S"
    json.dumps(snap)


async def test_card_is_served_and_loaded_once(planner_hass: HomeAssistant) -> None:
    """The card's folder is served at /solar_ev_planner and loaded on every dashboard."""
    from pathlib import Path  # noqa: PLC0415
    from unittest.mock import AsyncMock, MagicMock, patch  # noqa: PLC0415

    from custom_components.solar_ev_planner import frontend as card  # noqa: PLC0415

    hass = planner_hass
    hass.config.components.add("frontend")
    http = MagicMock(async_register_static_paths=AsyncMock())
    with (
        patch.object(hass, "http", http),
        patch("homeassistant.components.frontend.add_extra_js_url") as add_js,
    ):
        await card.async_register_card(hass)
        await card.async_register_card(hass)
    (paths,) = http.async_register_static_paths.call_args.args
    assert paths[0].url_path == "/solar_ev_planner"
    assert (Path(paths[0].path) / card.CARD_FILE).is_file()
    add_js.assert_called_once_with(
        hass, f"/solar_ev_planner/{card.CARD_FILE}?v={card.CARD_VERSION}"
    )
