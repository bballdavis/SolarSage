"""Thin charging-schedule wrapper around rivian-python-client.

Only two provider operations are used: read the complete schedule list and
replace it. Errors are mapped to adapter exceptions so the controller never
depends on the client library's exception hierarchy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

from rivian import Rivian
from rivian.exceptions import (
    RivianApiException,
    RivianApiRateLimitError,
    RivianExpiredTokenError,
    RivianUnauthenticated,
)

_LOGGER = logging.getLogger(__name__)


class ScheduleApiError(Exception):
    """A provider request failed. For a write, the outcome is unknown."""


class AuthError(ScheduleApiError):
    """The Rivian session is no longer valid; a new login is required."""


class RateLimitedError(ScheduleApiError):
    """Rivian rate-limited the request."""


class WriteRejectedError(ScheduleApiError):
    """Rivian answered a write without reporting success."""


class RivianScheduleApi:
    """Read and replace one vehicle's complete charging schedule list."""

    def __init__(self, client: Rivian, vehicle_id: str) -> None:
        self._client = client
        self._vehicle_id = vehicle_id

    async def get_schedules(self) -> list[dict[str, Any]]:
        """Return the complete schedule list exactly as the provider sent it."""
        response = await self._call(self._client.get_charging_schedules, self._vehicle_id)
        payload = await response.json()
        vehicle = (payload.get("data") or {}).get("getVehicle")
        if not isinstance(vehicle, dict):
            raise ScheduleApiError(
                f"No vehicle in the charging schedule response: {payload.get('errors')}"
            )
        schedules = vehicle.get("chargingSchedules")
        if not isinstance(schedules, list):
            # A missing or null list is not treated as "no schedules": an exact
            # restore needs the provider's complete list.
            raise ScheduleApiError("The charging schedule list is missing from the response")
        return schedules

    async def set_schedules(self, schedules: list[dict[str, Any]]) -> None:
        """Replace the complete schedule list.

        Success here only means Rivian accepted the request. The controller
        verifies every write with a fresh read.
        """
        response = await self._call(
            self._client.set_charging_schedules, self._vehicle_id, schedules
        )
        payload = await response.json()
        result = (payload.get("data") or {}).get("setChargingSchedules") or {}
        if not result.get("success"):
            raise WriteRejectedError(
                f"Rivian did not accept the schedule write: {payload.get('errors')}"
            )

    async def _call(self, method: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        try:
            try:
                return await method(*args)
            except RivianExpiredTokenError:
                _LOGGER.debug("Rivian app session expired; creating a new one")
                await self._client.create_csrf_token()
                return await method(*args)
        except RivianUnauthenticated as err:
            raise AuthError("The Rivian session is unauthenticated") from err
        except RivianApiRateLimitError as err:
            raise RateLimitedError("Rivian rate-limited the request") from err
        except RivianApiException as err:
            raise ScheduleApiError(f"Rivian request failed: {err}") from err
