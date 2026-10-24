"""Pinned Home Assistant panel registration contract tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.rivian.solar_ev_charging import panel


def _hass() -> SimpleNamespace:
    return SimpleNamespace(
        data={},
        http=SimpleNamespace(async_register_static_paths=AsyncMock()),
    )


def test_registers_once_and_removes_panel_after_last_entry(monkeypatch) -> None:
    async def scenario() -> None:
        hass = _hass()
        register = AsyncMock()
        remove = Mock()
        monkeypatch.setattr(panel, "_register_panel", register)
        monkeypatch.setattr(panel, "async_remove_panel", remove)

        await panel.async_register_panel(hass, "entry-a")
        await panel.async_register_panel(hass, "entry-b")
        assert hass.http.async_register_static_paths.await_count == 1
        assert register.await_count == 1

        await panel.async_unregister_panel(hass, "entry-a")
        remove.assert_not_called()
        await panel.async_unregister_panel(hass, "entry-b")
        remove.assert_called_once_with(hass, "solar-charging")

    asyncio.run(scenario())


def test_missing_frontend_is_headless_noop(monkeypatch) -> None:
    async def scenario() -> None:
        hass = _hass()
        register = AsyncMock()
        monkeypatch.setattr(panel, "_register_panel", register)
        monkeypatch.setattr(
            panel, "_FRONTEND_DIR", Path("definitely-missing-panel-assets")
        )
        await panel.async_register_panel(hass, "entry-a")
        register.assert_not_awaited()
        assert hass.data == {}

    asyncio.run(scenario())


def test_unconfigured_integration_registers_setup_ui_without_controller(monkeypatch):
    from custom_components import rivian
    from custom_components.rivian.const import ATTR_SOLAR_COORDINATOR, DOMAIN

    async def scenario():
        hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock()),
        )
        entry = SimpleNamespace(
            entry_id="unconfigured-entry",
            options={},
            async_on_unload=Mock(),
            add_update_listener=Mock(),
        )
        client = SimpleNamespace(create_csrf_token=AsyncMock(), close=AsyncMock())
        account = SimpleNamespace(
            async_config_entry_first_refresh=AsyncMock(),
            get_vehicles=dict,
        )
        register = AsyncMock()
        make_controller = Mock()
        monkeypatch.setattr(rivian, "get_rivian_api_from_entry", lambda *_: client)
        monkeypatch.setattr(rivian, "UserCoordinator", lambda **_: account)
        monkeypatch.setattr(rivian, "WallboxCoordinator", lambda **_: account)
        monkeypatch.setattr(rivian, "async_delete_issue", Mock())
        monkeypatch.setattr(rivian, "_register_services", Mock())
        monkeypatch.setattr(
            rivian,
            "Store",
            lambda *_: SimpleNamespace(async_load=AsyncMock(return_value=None)),
        )
        monkeypatch.setattr(rivian, "SolarEvCoordinator", make_controller)
        monkeypatch.setattr(rivian, "async_register_panel", register)

        assert await rivian.async_setup_entry(hass, entry)
        register.assert_awaited_once_with(hass, "unconfigured-entry")
        make_controller.assert_not_called()
        assert ATTR_SOLAR_COORDINATOR not in hass.data[DOMAIN][entry.entry_id]

    asyncio.run(scenario())
