"""Buttons for Solar EV Charging."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.button import ButtonEntity

from .coordinator import SolarEvCoordinator
from .entity import SolarEvEntity


class SolarEvButton(SolarEvEntity, ButtonEntity):
    """Run a controller command."""

    def __init__(
        self,
        coordinator: SolarEvCoordinator,
        key: str,
        translation_key: str,
        action: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = translation_key
        self._action = action

    async def async_press(self) -> None:
        await self._action()
