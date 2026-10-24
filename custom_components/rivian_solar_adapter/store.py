"""Durable storage for the ownership journal.

Home Assistant's ``Store`` is not used: it defers saves while Home Assistant
stops, logs instead of raising when a write fails, and moves an unparsable
file aside and loads nothing. The journal can hold the only copy of the
owner's schedules, so this store writes every save to disk atomically before
returning, raises when it cannot, and refuses to load a file it cannot parse.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.util.file import write_utf8_file_atomic

from .const import STORAGE_VERSION, journal_key, legacy_storage_key


class JournalLoadError(Exception):
    """The journal file exists but cannot be read or parsed."""


class JournalFileStore:
    """One vehicle's journal in ``.storage``, saved atomically and privately."""

    def __init__(self, hass: HomeAssistant, vehicle_id: str) -> None:
        self.hass = hass
        self.key = journal_key(vehicle_id)
        self.path = hass.config.path(STORAGE_DIR, self.key)
        # Saves run in executor threads; the lock keeps them in call order so
        # an older journal never replaces a newer one.
        self._lock = asyncio.Lock()

    @property
    def relative_path(self) -> str:
        """The path shown to people, relative to the configuration directory."""
        return f"{STORAGE_DIR}/{self.key}"

    async def async_load(self) -> dict[str, Any] | None:
        """Return the journal, or None if there is no file. Raises JournalLoadError."""
        return await self.hass.async_add_executor_job(_read_journal, self.path, self.key)

    async def async_save(self, data: dict[str, Any]) -> None:
        """Write the journal to disk before returning; raise if that fails."""
        # Serialized now, so the file holds the journal as it was at the call.
        payload = json.dumps(
            {"version": STORAGE_VERSION, "minor_version": 1, "key": self.key, "data": data},
            indent=2,
        )
        async with self._lock:
            await self.hass.async_add_executor_job(_write_journal, self.path, payload)

    async def async_remove(self) -> None:
        """Delete the journal file, if there is one."""
        async with self._lock:
            await self.hass.async_add_executor_job(_remove_file, self.path)


def _read_journal(path: str, key: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as file:
            text = file.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as err:
        raise JournalLoadError(f"{type(err).__name__}: {err}") from err
    try:
        content = json.loads(text)
    except ValueError as err:
        raise JournalLoadError(f"invalid JSON ({err})") from err
    if (
        not isinstance(content, dict)
        or content.get("key") != key
        or not isinstance(content.get("data"), dict)
    ):
        raise JournalLoadError("the file is not an adapter journal")
    return content["data"]


def _write_journal(path: str, payload: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_utf8_file_atomic(path, payload, private=True)


def _remove_file(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


async def async_check_legacy_journal(hass: HomeAssistant, entry_id: str) -> str | None:
    """Deal with a journal from when journals were keyed by config entry.

    An idle one holds nothing to restore and is deleted. Otherwise its path is
    returned: it may hold the owner's schedules and must not be ignored.
    """
    key = legacy_storage_key(entry_id)
    path = hass.config.path(STORAGE_DIR, key)

    def check() -> str | None:
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as file:
                data = json.load(file).get("data")
        except OSError, ValueError, AttributeError:
            return f"{STORAGE_DIR}/{key}"
        if isinstance(data, dict) and data.get("phase") == "idle" and data.get("original") is None:
            os.remove(path)
            return None
        return f"{STORAGE_DIR}/{key}"

    return await hass.async_add_executor_job(check)
