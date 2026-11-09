"""Tests for the Rivian schedule API wrapper's error mapping."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import aiohttp
import pytest
from rivian.exceptions import (
    RivianApiException,
    RivianApiRateLimitError,
    RivianTemporarilyLockedError,
    RivianUnauthenticated,
)

from custom_components.solar_ev_planner.rivian.api import (
    AuthError,
    RateLimitedError,
    RivianScheduleApi,
    ScheduleApiError,
    WriteRejectedError,
)

SCHEDULE = {
    "weekDays": ["Monday"],
    "startTime": 0,
    "duration": 60,
    "location": {"latitude": 30.123456, "longitude": -97.654321},
    "amperage": 16,
    "enabled": True,
}


class Response:
    def __init__(self, payload: Any = None, error: Exception | None = None):
        self.payload = payload
        self.error = error

    async def json(self):
        if self.error:
            raise self.error
        return self.payload


class FakeClient:
    """Stands in for rivian.Rivian; each call pops the next scripted outcome."""

    def __init__(self, *outcomes: Any):
        self.outcomes = list(outcomes)
        self.calls: list[str] = []
        self.csrf = 0

    async def _next(self, name: str):
        self.calls.append(name)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def get_charging_schedules(self, vehicle_id):
        return await self._next("get")

    async def set_charging_schedules(self, vehicle_id, schedules):
        return await self._next("set")

    async def create_csrf_token(self):
        self.csrf += 1


def schedules_payload(schedules=(SCHEDULE,)):
    return {"data": {"getVehicle": {"chargingSchedules": list(schedules)}}}


def errors(*codes):
    return [{"message": "x", "extensions": {"code": code}} for code in codes]


async def test_get_returns_the_list():
    api = RivianScheduleApi(FakeClient(Response(schedules_payload())), "v")
    assert await api.get_schedules() == [SCHEDULE]


async def test_null_list_is_an_error_not_an_empty_list():
    payload = {"data": {"getVehicle": {"chargingSchedules": None}}}
    api = RivianScheduleApi(FakeClient(Response(payload)), "v")
    with pytest.raises(ScheduleApiError, match="missing"):
        await api.get_schedules()


async def test_unauthenticated_with_data_refreshes_session_and_retries():
    client = FakeClient(
        Response({"data": {"getVehicle": None}, "errors": errors("UNAUTHENTICATED")}),
        Response(schedules_payload()),
    )
    api = RivianScheduleApi(client, "v")
    assert await api.get_schedules() == [SCHEDULE]
    assert client.csrf == 1


async def test_unauthenticated_twice_is_an_auth_error():
    client = FakeClient(RivianUnauthenticated(401, {}), RivianUnauthenticated(401, {}))
    api = RivianScheduleApi(client, "v")
    with pytest.raises(AuthError):
        await api.get_schedules()
    assert client.csrf == 1


@pytest.mark.parametrize(
    ("outcome", "backoff"),
    [
        (Response({"data": {"getVehicle": None}, "errors": errors("RATE_LIMIT")}), None),
        (RivianApiRateLimitError(429, {}), None),
        (
            Response({"data": {"getVehicle": None}, "errors": errors("SESSION_MANAGER_ERROR")}),
            timedelta(minutes=30),
        ),
        (RivianTemporarilyLockedError(400, {}), timedelta(minutes=30)),
    ],
)
async def test_rate_limits_and_locks_with_or_without_data(outcome, backoff):
    api = RivianScheduleApi(FakeClient(outcome), "v")
    with pytest.raises(RateLimitedError) as err:
        await api.get_schedules()
    assert err.value.backoff == backoff


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ContentTypeError(None, (), message="text/html"),
        ValueError("Expecting value"),
    ],
)
async def test_non_json_responses_are_api_errors(error):
    api = RivianScheduleApi(FakeClient(Response(error=error)), "v")
    with pytest.raises(ScheduleApiError):
        await api.get_schedules()


async def test_non_object_payload_is_an_api_error():
    api = RivianScheduleApi(FakeClient(Response(["unexpected"])), "v")
    with pytest.raises(ScheduleApiError):
        await api.get_schedules()


async def test_errors_never_carry_raw_responses():
    leaky = RivianApiException("boom", 500, {"data": schedules_payload()["data"]})
    api = RivianScheduleApi(FakeClient(leaky), "v")
    with pytest.raises(ScheduleApiError) as err:
        await api.get_schedules()
    assert "30.12" not in str(err.value) and "latitude" not in str(err.value)


async def test_write_errors_next_to_data_are_unknown_outcome_not_rejection():
    payload = {"data": {"setChargingSchedules": None}, "errors": errors("INTERNAL_SERVER_ERROR")}
    api = RivianScheduleApi(FakeClient(Response(payload)), "v")
    with pytest.raises(ScheduleApiError) as err:
        await api.set_schedules([SCHEDULE])
    assert not isinstance(err.value, WriteRejectedError)


async def test_write_without_success_is_rejected():
    payload = {"data": {"setChargingSchedules": {"success": False}}}
    api = RivianScheduleApi(FakeClient(Response(payload)), "v")
    with pytest.raises(WriteRejectedError):
        await api.set_schedules([SCHEDULE])


async def test_start_session_maps_errors():
    client = FakeClient()

    async def broken():
        raise aiohttp.ContentTypeError(None, (), message="text/html")

    client.create_csrf_token = broken
    with pytest.raises(ScheduleApiError):
        await RivianScheduleApi(client, "v").async_start_session()
