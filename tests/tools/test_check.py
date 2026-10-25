"""check.py against the fake Home Assistant: the table, the problems it must catch, and that
it never calls a service."""

from __future__ import annotations

from datetime import datetime, timedelta

from fake_hass import FakeHass
import pytest
from synthetic import TZ, E, adapter_attrs, build_site

from tools import check
from tools.config import load_config
from tools.hass_client import HassClient

NOW = datetime(2026, 10, 21, 13, 0, tzinfo=TZ)  # a Wednesday, the Rivian plugged at 10:00


def rows_by_check(rows: list[check.Row]) -> dict[str, check.Row]:
    return {r.check: r for r in rows}


async def run(client: HassClient) -> dict[str, check.Row]:
    rows = await check.run_checks(client, load_config(), NOW)
    return rows_by_check(rows)


async def test_healthy_site_passes(client: HassClient, fake: FakeHass) -> None:
    build_site(fake, NOW)
    rows = await check.run_checks(client, load_config(), NOW)
    by = rows_by_check(rows)
    bad = [r for r in rows if r.status == check.FAIL]
    assert not bad, check.render(bad)
    assert check.exit_code(rows) == 0
    assert by["pv_power"].status == check.OK
    assert by["Sol-Ark alive"].status == check.OK
    assert by["signs (24 h)"].status == check.OK
    assert by["battery direction"].status == check.OK
    assert by["forecast_today detail"].value == "48 slots"
    assert by["Solcast last poll"].status == check.OK
    assert by["free hours weekly"].detail == "Mon-Sun 00:00-06:00 20:00-24:00"
    assert by["tesla on Tesla Fleet"].value == "tesla_fleet"
    assert by["rivian control state"].value == "dry_run"
    assert by["car on the charger"].value == "rivian"
    # the Tesla is at a named zone: shown as away, and its coordinates never appear
    table = check.render(rows)
    assert "Office" not in table and "32.87" not in table
    assert fake.service_calls == []
    assert not [m for m in fake.ws_messages if m["type"] == "call_service"]


async def test_old_unchanged_values_are_not_stale(client: HassClient, fake: FakeHass) -> None:
    """The battery capacity and the Tesla's values have not changed for days: fine."""
    build_site(fake, NOW)
    fake.states[E["capacity"]]["last_changed"] = (NOW - timedelta(days=9)).isoformat()
    fake.states[E["capacity"]]["last_updated"] = (NOW - timedelta(days=9)).isoformat()
    fake.states[E["capacity"]]["last_reported"] = (NOW - timedelta(days=9)).isoformat()
    by = await run(client)
    assert by["battery_capacity"].status == check.OK
    assert "9.0 d" in by["battery_capacity"].detail


async def test_quiet_sol_ark_and_unavailable_values_fail(
    client: HassClient, fake: FakeHass
) -> None:
    build_site(fake, NOW)
    old = (NOW - timedelta(hours=3)).isoformat()
    for key in ("pv", "grid", "battery", "soc"):
        for field in ("last_changed", "last_updated", "last_reported"):
            fake.states[E[key]][field] = old
    fake.set_state(E["wall_connected"], "unavailable")
    by = await run(client)
    assert by["Sol-Ark alive"].status == check.FAIL
    assert by["wall_connected"].status == check.FAIL
    assert by["car on the charger"].status == check.FAIL


async def test_wrong_units_and_missing_entities(client: HassClient, fake: FakeHass) -> None:
    build_site(fake, NOW)
    fake.set_state(E["pv"], 5.1, {"unit_of_measurement": "kW"})
    fake.remove(E["fc_remaining"])
    fake.remove(E["soc"])
    by = await run(client)
    assert by["pv_power"].status == check.FAIL and "unit 'kW'" in by["pv_power"].detail
    assert by["battery_soc"].status == check.FAIL and by["battery_soc"].value == "missing"
    assert by["forecast_remaining_today"].status == check.WARN  # optional


@pytest.mark.parametrize(("grid", "battery", "flipped"), [(-1, 1, "grid"), (1, -1, "battery")])
async def test_reversed_signs_are_caught(
    client: HassClient, fake: FakeHass, grid: int, battery: int, flipped: str
) -> None:
    build_site(fake, NOW, grid_sign=grid, battery_sign=battery)
    by = await run(client)
    assert by["signs (24 h)"].status == check.FAIL
    assert f"the {flipped} sign looks reversed" in by["signs (24 h)"].detail
    if flipped == "battery":
        assert by["battery direction"].status == check.FAIL


async def test_solcast_without_detail_or_estimates(client: HassClient, fake: FakeHass) -> None:
    build_site(fake, NOW)
    attrs = dict(fake.states[E["fc_today"]]["attributes"])
    attrs.pop("detailedForecast")
    fake.set_state(E["fc_today"], 30, attrs)
    attrs = dict(fake.states[E["fc_tomorrow"]]["attributes"])
    attrs["detailedForecast"] = [
        {k: v for k, v in r.items() if k != "pv_estimate10"} for r in attrs["detailedForecast"]
    ]
    fake.set_state(E["fc_tomorrow"], 30, attrs)
    fake.set_state(E["fc_polled"], (NOW - timedelta(hours=30)).isoformat())
    by = await run(client)
    assert by["forecast_today detail"].status == check.FAIL
    assert by["forecast_tomorrow detail"].status == check.WARN
    assert by["Solcast last poll"].status == check.FAIL


async def test_tesla_custom_only(client: HassClient, fake: FakeHass) -> None:
    build_site(fake, NOW)
    for key in ("t_switch", "t_current"):
        fake.remove(E[key])
    fake.set_state("switch.roxxo_charger", "off", {})
    by = await run(client)
    row = by["tesla on Tesla Fleet"]
    assert row.status == check.FAIL and "switch.roxxo_charger" in row.detail


async def test_adapter_fault_and_handed_back(client: HassClient, fake: FakeHass) -> None:
    build_site(fake, NOW, adapter_dry_run=False)
    fake.set_state(
        E["r_state"],
        "fault",
        adapter_attrs(dry_run=False, fault_code="external_edit", fault_message="edited"),
    )
    by = await run(client)
    assert by["rivian control state"].status == check.FAIL
    assert "external_edit" in by["rivian control state"].detail
    fake.set_state(E["r_state"], "handed_back", adapter_attrs(dry_run=False, handed_back=True))
    by = await run(client)
    assert by["rivian control state"].status == check.WARN


async def test_which_car_the_wall_connector_sees(client: HassClient, fake: FakeHass) -> None:
    build_site(fake, NOW)
    fake.set_state(E["t_plugged"], "on")
    fake.set_state(E["t_location"], "home")
    by = await run(client)
    assert by["car on the charger"].value == "ambiguous"
    assert "commands neither" in by["car on the charger"].detail
    fake.set_state(E["r_plugged"], "off")
    fake.set_state(E["t_plugged"], "off")
    by = await run(client)
    assert by["car on the charger"].value == "unknown"
    fake.set_state(E["wall_connected"], "off")
    fake.set_state(E["r_plugged"], "on")
    by = await run(client)
    assert by["car on the charger"].value == "none"
    assert by["car on the charger"].status == check.WARN
    fake.set_state(E["wall_connected"], "on")
    fake.set_state(E["r_status"], "a")
    by = await run(client)
    assert by["rivian charger status"].status == check.WARN


HEARTBEAT = "sensor.solar_ev_planner_heartbeat"
R_WANTED = "binary_sensor.r1s_planner_charging_wanted"


def add_planner(fake: FakeHass, heartbeat: str = "shadow", wanted: str = "unknown") -> None:
    """The planner integration's entities, as it shows them."""
    fake.set_state(HEARTBEAT, heartbeat, {})
    fake.set_state("switch.solar_ev_planner_planning", "on", {})
    fake.set_state("switch.solar_ev_planner_shadow_mode", "on" if heartbeat != "active" else "off")
    fake.set_state("sensor.solar_ev_planner_mode", "day", {})
    fake.set_state("sensor.solar_ev_planner_car_on_charger", "rivian", {})
    fake.set_state(
        "sensor.solar_ev_planner_reason", "Solar charging at 10 A.", {"full_reason": "Solar."}
    )
    fake.set_state(R_WANTED, wanted, {})


def observe(fake: FakeHass, **obs: object) -> None:
    """The adapter's control state, with what it reads from its controller options."""
    base = {"plugged": True, "home": True, "at_charger": True}
    fake.set_state(E["r_state"], "dry_run", adapter_attrs(observation=base | obs))


async def test_planner_not_installed_is_only_information(
    client: HassClient, fake: FakeHass
) -> None:
    build_site(fake, NOW)
    by = await run(client)
    assert by["planner"].status == check.INFO and by["planner"].value == "not installed"
    assert by["rivian controller liveness"].status == check.INFO


async def test_planner_in_shadow_mode_with_the_adapter_mapped(
    client: HassClient, fake: FakeHass
) -> None:
    build_site(fake, NOW)
    add_planner(fake)
    observe(fake, controller_alive=True, controller_charging=None)
    by = await run(client)
    assert by["planner heartbeat"].status == check.OK
    assert by["planner shadow mode"].status == check.OK
    assert by["planner reason"].detail == "Solar."
    assert by["rivian controller liveness"].status == check.OK
    assert by["rivian controller charging"].status == check.INFO  # no decision in shadow mode
    assert fake.service_calls == []


async def test_adapter_options_not_pointing_at_the_planner(
    client: HassClient, fake: FakeHass
) -> None:
    build_site(fake, NOW)
    add_planner(fake, heartbeat="active", wanted="on")
    observe(fake)  # neither controller option mapped
    by = await run(client)
    assert by["planner heartbeat"].status == check.INFO
    assert by["rivian controller liveness"].status == check.WARN
    assert HEARTBEAT in by["rivian controller liveness"].detail
    assert by["rivian controller charging"].status == check.WARN
    assert R_WANTED in by["rivian controller charging"].detail
    # mapped to something else: the adapter reads down and off while the planner is up and on
    observe(fake, controller_alive=False, controller_charging=False)
    by = await run(client)
    assert by["rivian controller liveness"].value == "mapped elsewhere"
    assert by["rivian controller charging"].value == "mapped elsewhere"
    observe(fake, controller_alive=True, controller_charging=True)
    by = await run(client)
    assert by["rivian controller liveness"].status == check.OK
    assert by["rivian controller charging"].status == check.OK
    # the planner down (Planning off, or it cannot plan)
    add_planner(fake, heartbeat="unavailable")
    observe(fake, controller_alive=False)
    by = await run(client)
    assert by["planner heartbeat"].status == check.WARN
    assert by["rivian controller liveness"].value == "down"


async def test_cli_exit_codes(client: HassClient, fake: FakeHass, capsys) -> None:
    build_site(fake, datetime.now(TZ).replace(second=0, microsecond=0))
    code = await check.amain([], client=client)
    out = capsys.readouterr().out
    assert "car on the charger" in out
    assert code in (0, 1)
    assert await check.amain(["--json"], client=client) == code
    assert '"check": "Home Assistant"' in capsys.readouterr().out
    fake.remove(E["pv"])
    assert await check.amain([], client=client) == 1


async def test_cli_without_connection_settings(capsys) -> None:
    assert await check.amain([]) == 2
    assert "HASS_TOKEN" in capsys.readouterr().err


async def test_cli_unreachable(socket_enabled: None, capsys, monkeypatch) -> None:
    monkeypatch.setenv("HASS_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HASS_TOKEN", "x")
    assert await check.amain([]) == 2
    assert "cannot reach" in capsys.readouterr().err
