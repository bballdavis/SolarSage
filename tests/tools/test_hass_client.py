"""The minimal HA client against the fake server: REST, WebSocket and the refusal paths."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fake_hass import TOKEN, FakeHass
import pytest

from tools.hass_client import (
    HassAuthError,
    HassClient,
    HassError,
    ServiceCallRefused,
    connection_settings,
    read_env_file,
)

T0 = datetime(2026, 10, 20, 17, 0, tzinfo=UTC)


def test_env_file_parsing(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text(
        "# comment\nexport HASS_URL='http://ha.test:8123'\nHASS_TOKEN=\"abc\"\nJUNK\nX=1\n"
    )
    assert read_env_file(f) == {"HASS_URL": "http://ha.test:8123", "HASS_TOKEN": "abc", "X": "1"}
    assert read_env_file(tmp_path / "missing") == {}


def test_environment_wins_over_file(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("HASS_URL=http://file:8123\nHASS_TOKEN=file-token\n")
    s = connection_settings({"HASS_TOKEN": "env-token", "OTHER": "x"}, f)
    assert s["HASS_URL"] == "http://file:8123"
    assert s["HASS_TOKEN"] == "env-token"
    assert "OTHER" not in s


def test_missing_settings_explain_the_token() -> None:
    with pytest.raises(HassError, match="long-lived access token"):
        connection_settings({}, None)


def test_repr_hides_the_token() -> None:
    c = HassClient("http://ha.test:8123/", "secret-token")
    assert "secret" not in repr(c)
    assert c.url == "http://ha.test:8123"


async def test_rest_states_and_history(client: HassClient, fake: FakeHass) -> None:
    fake.set_state("sensor.a", 1, {"unit_of_measurement": "W"}, when=T0)
    fake.set_state("sensor.a", 2, when=T0 + timedelta(minutes=5))
    fake.set_state("sensor.a", 3, when=T0 + timedelta(minutes=10))
    fake.set_state("sensor.a", 4, when=T0 + timedelta(minutes=15))
    assert (await client.api_status())["message"] == "API running."
    assert (await client.state("sensor.a"))["state"] == "4"
    assert await client.state("sensor.missing") is None
    got = await client.states_of(["sensor.a", "sensor.missing"])
    assert got["sensor.missing"] is None and got["sensor.a"]["state"] == "4"
    hist = await client.history(["sensor.a"], T0 + timedelta(minutes=2), T0 + timedelta(hours=1))
    rows = hist["sensor.a"]
    assert [r["state"] for r in rows] == ["1", "2", "3", "4"]
    # the first row is the state at the start; minimal rows in between carry no attributes
    assert rows[0]["last_changed"].startswith("2026-10-20T17:02")
    assert "attributes" not in rows[1] and rows[1]["entity_id"] == "sensor.a"
    bare = await client.history(["sensor.a"], T0, minimal=False, no_attributes=True)
    assert all("attributes" not in r for r in bare["sensor.a"])
    assert await client.history([], T0) == {}


async def test_websocket_commands(client: HassClient, fake: FakeHass) -> None:
    ms = T0.timestamp() * 1000
    fake.statistics["hour"]["sensor.p"] = [
        {"start": ms, "end": ms + 3.6e6, "mean": 1.0, "min": 0.0, "max": 2.0},
        {"start": ms + 3.6e6, "end": ms + 7.2e6, "mean": 3.0, "min": 0.0, "max": 4.0},
    ]
    fake.stat_units = {"sensor.p": "W"}
    fake.register("schedule.free_hours", "schedule", "free_hours")
    fake.schedules = [{"id": "free_hours", "monday": [{"from": "20:00:00", "to": "23:59:59"}]}]
    stats = await client.statistics_during_period(
        ["sensor.p"], T0, T0 + timedelta(minutes=59), types=("mean",)
    )
    assert stats == {"sensor.p": [{"start": ms, "end": ms + 3.6e6, "mean": 1.0}]}
    meta = await client.statistics_metadata(["sensor.p"])
    assert meta[0]["statistics_unit_of_measurement"] == "W"
    assert (await client.registry_entry("schedule.free_hours"))["platform"] == "schedule"
    assert await client.registry_entry("sensor.nope") is None
    assert (await client.schedule_config("schedule.free_hours"))["id"] == "free_hours"
    assert await client.schedule_config("schedule.other") is None
    with pytest.raises(HassError, match="unknown_command"):
        await client.ws_command({"type": "nope"})
    # one WebSocket for all of them, authenticated once
    assert fake.requests.count("GET /api/websocket") == 1


async def test_service_calls_are_refused_by_default(client: HassClient, fake: FakeHass) -> None:
    with pytest.raises(ServiceCallRefused, match="read-only"):
        await client.call_service("switch", "turn_on", {"entity_id": "switch.x"})
    with pytest.raises(ServiceCallRefused):
        await client.ws_command({"type": "call_service", "domain": "switch", "service": "x"})
    assert fake.service_calls == []
    assert not [m for m in fake.ws_messages if m.get("type") == "call_service"]


async def test_service_calls_when_enabled(url: str, fake: FakeHass) -> None:
    async with HassClient(url, TOKEN, allow_service_calls=True) as c:
        await c.call_service("number", "set_value", {"value": 10}, target={"entity_id": "n.x"})
        assert c.service_calls[0]["service"] == "set_value"
    assert fake.service_calls == [
        {
            "domain": "number",
            "service": "set_value",
            "data": {"value": 10},
            "target": {"entity_id": "n.x"},
        }
    ]


async def test_bad_token_is_refused(url: str) -> None:
    async with HassClient(url, "wrong-token") as c:
        with pytest.raises(HassAuthError):
            await c.states()
        with pytest.raises(HassAuthError):
            await c.schedule_helpers()


async def test_unreachable_server_is_a_hass_error(socket_enabled: None) -> None:
    async with HassClient("http://127.0.0.1:9", TOKEN, timeout=5) as c:
        with pytest.raises(HassError, match="cannot reach"):
            await c.states()
        with pytest.raises(HassError, match="WebSocket"):
            await c.schedule_helpers()
