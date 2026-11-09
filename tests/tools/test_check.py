"""check.py against the fake Home Assistant: the table, the problems it must catch, and that
it never calls a service."""

from __future__ import annotations

from datetime import datetime, timedelta

from fake_hass import FakeHass
import pytest
from synthetic import TZ, E, build_site, schedule_control_attrs

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
    fake.set_state("switch.tesla_charger", "off", {})
    by = await run(client)
    row = by["tesla on Tesla Fleet"]
    assert row.status == check.FAIL and "switch.tesla_charger" in row.detail


@pytest.mark.parametrize(
    ("platform", "note"),
    [
        ("tesla_fleet", "billed by Tesla's Fleet API"),
        ("teslemetry", "Teslemetry credits"),
        ("tessie", "Tessie subscription"),
    ],
)
async def test_tesla_platforms_and_their_billing(
    client: HassClient, fake: FakeHass, platform: str, note: str
) -> None:
    build_site(fake, NOW)
    for key in ("t_soc", "t_limit", "t_plugged", "t_switch", "t_current", "t_charging"):
        fake.register(E[key], platform)
    fake.register(E["t_location"], platform)
    by = await run(client)
    row = by["tesla on Tesla Fleet"]
    assert (row.status, row.value) == (check.OK, platform)
    assert note in row.detail
    assert by["tesla.charging_state"].status == check.OK


@pytest.mark.parametrize("key", ["t_limit", "t_location"])
async def test_a_leftover_tesla_custom_limit_or_location_fails(
    client: HassClient, fake: FakeHass, key: str
) -> None:
    """tesla_custom holds number.tesla_charge_limit (and the tracker) under Fleet's IDs."""
    build_site(fake, NOW)
    fake.register(E[key], "tesla_custom")
    by = await run(client)
    row = by["tesla on Tesla Fleet"]
    assert row.status == check.FAIL and row.entity == E[key]
    assert "remove tesla_custom" in row.detail


async def test_tesla_entities_from_two_integrations_warn(
    client: HassClient, fake: FakeHass
) -> None:
    build_site(fake, NOW)
    fake.register(E["t_soc"], "teslemetry")
    by = await run(client)
    row = by["tesla on Tesla Fleet"]
    assert row.status == check.WARN and row.value == "tesla_fleet, teslemetry"


async def test_tesla_custom_leftovers_beside_tesla_fleet_warn(
    client: HassClient, fake: FakeHass
) -> None:
    build_site(fake, NOW)
    fake.set_state("number.tesla_charging_amps", 32, {"unit_of_measurement": "A"})
    by = await run(client)
    assert by["tesla on Tesla Fleet"].status == check.OK
    row = by["tesla tesla_custom leftovers"]
    assert row.status == check.WARN and row.entity == "number.tesla_charging_amps"


async def test_the_rivian_tracker_under_its_newer_id(client: HassClient, fake: FakeHass) -> None:
    """Fresh upstream 2.0.0 installs name the tracker after the car: device_tracker.r1s."""
    build_site(fake, NOW)
    state = fake.states[E["r_location"]]
    fake.remove(E["r_location"])
    fake.set_state("device_tracker.r1s", "home", state["attributes"])
    by = await run(client)
    row = by["rivian.location id"]
    assert row.status == check.WARN and row.entity == "device_tracker.r1s"
    assert "set cars.rivian.location" in row.detail
    assert by["rivian.location"].status == check.OK
    assert by["rivian.location"].entity == "device_tracker.r1s"
    assert by["car on the charger"].value == "rivian"


async def test_the_rivian_tracker_under_its_older_id(client: HassClient, fake: FakeHass) -> None:
    build_site(fake, NOW)
    fake.set_state("device_tracker.r1s", "home", {})
    rows = await check.run_checks(client, load_config(), NOW)
    assert "rivian.location id" not in rows_by_check(rows)  # the mapped one exists: no warning


async def test_upstream_rivian_schedule_entities_must_be_disabled(
    client: HassClient, fake: FakeHass
) -> None:
    build_site(fake, NOW)
    by = await run(client)
    assert by["rivian upstream schedule entities"].status == check.OK  # none at all
    # enabled: present (with or without a registry entry)
    fake.set_state("number.r1s_charging_schedule_amperage", 48, {"unit_of_measurement": "A"})
    fake.set_state("time.r1s_charging_schedule_start", "00:00:00", {})
    fake.register("time.r1s_charging_schedule_start", "rivian")
    by = await run(client)
    row = by["rivian upstream schedule entities"]
    assert row.status == check.WARN
    assert row.entity == "number.r1s_charging_schedule_amperage, time.r1s_charging_schedule_start"
    assert check.exit_code(list(by.values()), strict=True) == 1
    # disabled in the registry: no state, and fine
    for eid in ("number.r1s_charging_schedule_amperage", "time.r1s_charging_schedule_start"):
        fake.remove(eid)
        fake.register(eid, "rivian")
        fake.registry[eid]["disabled_by"] = "user"
    by = await run(client)
    assert by["rivian upstream schedule entities"].status == check.OK


async def test_schedule_control_fault_and_handed_back(client: HassClient, fake: FakeHass) -> None:
    build_site(fake, NOW, schedule_control_dry_run=False)
    fake.set_state(
        E["r_state"],
        "fault",
        schedule_control_attrs(dry_run=False, fault_code="external_edit", fault_message="edited"),
    )
    by = await run(client)
    assert by["rivian control state"].status == check.FAIL
    assert "external_edit" in by["rivian control state"].detail
    fake.set_state(
        E["r_state"], "handed_back", schedule_control_attrs(dry_run=False, handed_back=True)
    )
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


HEARTBEAT = "sensor.solarsage_heartbeat"
R_WANTED = "binary_sensor.r1s_planner_charging_wanted"


def add_planner(fake: FakeHass, heartbeat: str = "shadow", wanted: str = "unknown") -> None:
    """The planner integration's entities, as it shows them."""
    fake.set_state(HEARTBEAT, heartbeat, {})
    fake.set_state("switch.solarsage_planning", "on", {})
    fake.set_state("switch.solarsage_shadow_mode", "on" if heartbeat != "active" else "off")
    fake.set_state("sensor.solarsage_mode", "day", {})
    fake.set_state("sensor.solarsage_car_on_charger", "rivian", {})
    fake.set_state("sensor.solarsage_reason", "Solar charging at 10 A.", {"full_reason": "Solar."})
    fake.set_state(R_WANTED, wanted, {})


def observe(fake: FakeHass, **obs: object) -> None:
    """Rivian schedule control's control state, with what it reads from its controller options."""
    base = {"plugged": True, "home": True, "at_charger": True}
    fake.set_state(E["r_state"], "dry_run", schedule_control_attrs(observation=base | obs))


async def test_planner_not_installed_is_only_information(
    client: HassClient, fake: FakeHass
) -> None:
    build_site(fake, NOW)
    by = await run(client)
    assert by["planner"].status == check.INFO and by["planner"].value == "not installed"
    assert by["rivian controller liveness"].status == check.INFO


async def test_planner_in_shadow_mode_with_the_schedule_control_mapped(
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


async def test_schedule_control_options_not_pointing_at_the_planner(
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
    # mapped elsewhere: the schedule control reads down and off while the planner is up and on
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
