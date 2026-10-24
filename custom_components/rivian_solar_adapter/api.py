"""Thin charging-schedule wrapper around rivian-python-client.

Only two provider operations are used: read the complete schedule list and
replace it. All errors are mapped to adapter exceptions so the controller never
depends on the client library's exception hierarchy. Error messages carry
error codes and exception class names only: Rivian responses contain schedule
coordinates and must not reach logs, attributes, or diagnostics.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
import logging
from typing import Any

import aiohttp
from rivian import Rivian
from rivian.exceptions import (
    RivianApiException,
    RivianApiRateLimitError,
    RivianBadRequestError,
    RivianExpiredTokenError,
    RivianTemporarilyLockedError,
    RivianUnauthenticated,
)

_LOGGER = logging.getLogger(__name__)

LOCKED_BACKOFF = timedelta(minutes=30)
# Upper bound for one provider call, including reading the response body.
REQUEST_TIMEOUT = 45
# Error codes meaning Rivian refused the request as invalid; it was not applied.
INVALID_INPUT_CODES = frozenset({"BAD_REQUEST_ERROR", "BAD_USER_INPUT", "INVALID_INPUT"})

# Raised by the client library or our own decoding when a response is not the
# JSON we expect: HTML error pages, truncated bodies, unexpected shapes.
_DECODE_ERRORS = (
    aiohttp.ClientError,
    TimeoutError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
)


class ScheduleApiError(Exception):
    """A provider request failed. For a write, the outcome is unknown."""


class AuthError(ScheduleApiError):
    """The Rivian session is no longer valid; a new login is required."""


class RateLimitedError(ScheduleApiError):
    """Rivian rate-limited the request or temporarily locked the account."""

    def __init__(self, message: str, backoff: timedelta | None = None) -> None:
        super().__init__(message)
        self.backoff = backoff


class WriteRejectedError(ScheduleApiError):
    """Rivian definitely did not apply the request.

    ``invalid_input`` means Rivian refused the content as invalid (for example a
    schedule list it returned but will not accept back, upstream PR #308), so
    repeating the same request cannot succeed.
    """

    def __init__(self, message: str, *, invalid_input: bool = False) -> None:
        super().__init__(message)
        self.invalid_input = invalid_input


def error_codes(payload: dict[str, Any]) -> list[str]:
    """Return the GraphQL error codes in a response, in order."""
    codes = []
    errors = payload.get("errors")
    for error in errors if isinstance(errors, list) else []:
        if isinstance(error, dict):
            extensions = error.get("extensions")
            code = extensions.get("code") if isinstance(extensions, dict) else None
            codes.append(str(code or "UNKNOWN"))
    return codes


class RivianScheduleApi:
    """Read and replace one vehicle's complete charging schedule list."""

    def __init__(self, client: Rivian, vehicle_id: str) -> None:
        self._client = client
        self._vehicle_id = vehicle_id

    async def async_start_session(self) -> None:
        """Create the app session (CSRF token) the schedule requests need."""
        await self._call(self._client.create_csrf_token, decode=False)

    async def get_schedules(self) -> list[dict[str, Any]]:
        """Return the complete schedule list exactly as the provider sent it."""
        payload = await self._call(self._client.get_charging_schedules, self._vehicle_id)
        if codes := error_codes(payload):
            raise ScheduleApiError(f"Rivian returned errors reading schedules: {codes}")
        data = payload.get("data")
        vehicle = data.get("getVehicle") if isinstance(data, dict) else None
        if not isinstance(vehicle, dict):
            raise ScheduleApiError("No vehicle in the charging schedule response")
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
        payload = await self._call(self._client.set_charging_schedules, self._vehicle_id, schedules)
        if codes := error_codes(payload):
            # Errors next to data: the outcome is unknown until a fresh read.
            raise ScheduleApiError(f"Rivian returned errors writing schedules: {codes}")
        data = payload.get("data")
        result = data.get("setChargingSchedules") if isinstance(data, dict) else None
        if not isinstance(result, dict) or not result.get("success"):
            raise WriteRejectedError("Rivian did not report success for the schedule write")

    async def _call(
        self, method: Callable[..., Awaitable[Any]], *args: Any, decode: bool = True
    ) -> Any:
        """Call the client, retrying once with a fresh app session if rejected."""
        for attempt in range(2):
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    response = await method(*args)
                    payload = await response.json() if decode else None
            except (RivianUnauthenticated, RivianExpiredTokenError) as err:
                if attempt == 0 and decode:
                    await self._refresh_session()
                    continue
                raise AuthError("The Rivian session is unauthenticated") from err
            except RivianTemporarilyLockedError as err:
                raise RateLimitedError(
                    "Rivian temporarily locked the account", LOCKED_BACKOFF
                ) from err
            except RivianApiRateLimitError as err:
                raise RateLimitedError("Rivian rate-limited the request") from err
            except RivianBadRequestError as err:
                raise WriteRejectedError(
                    "Rivian rejected the request as invalid", invalid_input=True
                ) from err
            except RivianApiException as err:
                raise ScheduleApiError(f"Rivian request failed ({type(err).__name__})") from err
            except _DECODE_ERRORS as err:
                raise ScheduleApiError(
                    f"Unexpected response from Rivian ({type(err).__name__})"
                ) from err
            if not decode:
                return None
            if not isinstance(payload, dict):
                raise ScheduleApiError("Unexpected response from Rivian (not an object)")
            codes = error_codes(payload)
            if "UNAUTHENTICATED" in codes:
                # The client returns, rather than raises, errors that come with data.
                if attempt == 0:
                    await self._refresh_session()
                    continue
                raise AuthError("The Rivian session is unauthenticated")
            if "SESSION_MANAGER_ERROR" in codes:
                raise RateLimitedError("Rivian temporarily locked the account", LOCKED_BACKOFF)
            if "RATE_LIMIT" in codes:
                raise RateLimitedError("Rivian rate-limited the request")
            if invalid := INVALID_INPUT_CODES.intersection(codes):
                raise WriteRejectedError(
                    f"Rivian rejected the request as invalid: {sorted(invalid)}",
                    invalid_input=True,
                )
            return payload
        raise AuthError("The Rivian session is unauthenticated")  # pragma: no cover

    async def _refresh_session(self) -> None:
        _LOGGER.debug("Rivian rejected the session; creating a new app session and retrying")
        await self._call(self._client.create_csrf_token, decode=False)
