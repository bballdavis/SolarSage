"""The tools' config file, sanitizing, and finding the planner core."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

from tools import config as tool_config, core as tool_core
from tools.config import ConfigError, load_config, validate
from tools.sanitize import REDACTED, scrub, scrub_state, tracker_state

REPO = Path(__file__).resolve().parent.parent.parent


def test_example_is_the_default() -> None:
    cfg = load_config()
    assert cfg.source is None
    assert cfg.site["grid_power"] == "sensor.sol_ark_inverter_grid_power"
    assert cfg.car_entity("rivian", "switch") == "switch.r1s_schedule_control_solar_charging"
    assert cfg.car_entity("tesla", "switch") == "switch.roxxo_charge"
    assert cfg.kw_scale("pv_power") == 0.001 and cfg.kw_scale("wall_power") == 1.0
    assert ("rivian.location", "device_tracker.r1s_location") in list(cfg.entities())
    assert "sensor.r1s_schedule_control_control_state" in cfg.entity_ids()
    assert "_about" not in cfg.data


def test_example_matches_the_planner_core(core: ModuleType) -> None:
    """The example and the core's config-flow suggestions name the same entities."""
    from planner_core.entities import CAR_ENTITIES, SITE_ENTITIES, TESLA_CUSTOM_ENTITIES

    cfg = load_config()
    for role, eid in SITE_ENTITIES.items():
        assert cfg.site[role] == eid, role
    for car_id, keys in CAR_ENTITIES.items():
        for key, eid in keys.items():
            assert cfg.car_entity(car_id, key) == eid, (car_id, key)
    assert cfg.car_entity("tesla", "legacy_switch") == TESLA_CUSTOM_ENTITIES["switch"]
    assert cfg.car_entity("tesla", "legacy_current") == TESLA_CUSTOM_ENTITIES["current"]


def test_owner_file_overrides_keys(tmp_path: Path) -> None:
    f = tmp_path / "mine.json"
    f.write_text(json.dumps({"site": {"pv_power": "sensor.my_pv"}, "units": {"pv_power": "kW"}}))
    cfg = load_config(f)
    assert cfg.source == f
    assert cfg.site["pv_power"] == "sensor.my_pv"
    assert cfg.site["grid_power"] == "sensor.sol_ark_inverter_grid_power"  # kept
    assert cfg.kw_scale("pv_power") == 1.0


def test_owner_file_found_by_default(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"tariff": {"price": 0.4}}))
    monkeypatch.setattr(tool_config, "OWNER_FILES", (tmp_path / "config.yaml", f))
    assert load_config().section("tariff")["price"] == 0.4


def test_yaml_config(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    f = tmp_path / "c.yaml"
    f.write_text("cars:\n  tesla:\n    name: Other\n")
    assert load_config(f).cars["tesla"]["name"] == "Other"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{", "not valid JSON"),
        ("[]", "mapping at the top level"),
        ('{"site": {"pv_power": "Not An Id"}}', "is not an entity id"),
        ('{"units": {"pv_power": "hp"}}', "units.pv_power"),
        ('{"cars": {"x": {"kind": "bike"}}}', "cars.x.kind"),
        ('{"site": {"grid_power": ""}}', "site.grid_power is not mapped"),
    ],
)
def test_bad_configs(tmp_path: Path, content: str, message: str) -> None:
    f = tmp_path / "c.json"
    f.write_text(content)
    with pytest.raises(ConfigError, match=message):
        load_config(f)


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(tmp_path / "nope.json")
    assert validate({"site": "x"}) == ["'site' must be a mapping of role -> entity id"]


def test_env_file_is_git_ignored() -> None:
    lines = (REPO / ".gitignore").read_text().splitlines()
    assert "tools/.env" in lines
    assert "tools/out/" in lines


# --------------------------------------------------------------------------- sanitize


def test_scrub_drops_and_redacts() -> None:
    data = {
        "latitude": 32.87,
        "attributes": {"Longitude": -117.2, "gps_accuracy": 4, "source_type": "gps"},
        "context": {"user_id": "abc"},
        "rows": [{"vin": "x", "note": "VIN TESTVEHCLE0000001 seen", "n": 3}],
        "mail": "write to owner@example.invalid",
        "jwt": "Bearer eyJfake000000.eyJfake111111.fakesig222222",
        "where": "at 32.873410, -117.227490 now",
        "timestamp": "1792515600000",  # digits only: not a VIN
        "ok": "lease_active",
    }
    out = scrub(data)
    assert "latitude" not in out and "context" not in out
    assert out["attributes"] == {"source_type": "gps"}
    assert out["rows"] == [{"note": f"VIN {REDACTED} seen", "n": 3}]
    assert out["mail"] == f"write to {REDACTED}"
    assert out["jwt"] == f"Bearer {REDACTED}"
    assert out["where"] == f"at {REDACTED} now"
    assert out["timestamp"] == "1792515600000" and out["ok"] == "lease_active"


def test_trackers_keep_only_home_or_away() -> None:
    assert tracker_state("Office") == "not_home"
    assert tracker_state("home") == "home" and tracker_state("unavailable") == "unavailable"
    st = scrub_state(
        {
            "entity_id": "device_tracker.car",
            "state": "Grandma's",
            "attributes": {"latitude": 1.0, "friendly_name": "Car", "battery_level": 50},
        }
    )
    assert st["state"] == "not_home" and st["attributes"] == {"friendly_name": "Car"}


# --------------------------------------------------------------------------- the core


def test_core_from_a_folder(tmp_path: Path, monkeypatch) -> None:
    pkg = tmp_path / "somewhere" / "planner_core"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .part import VALUE\n")
    (pkg / "part.py").write_text("VALUE = 42\n")
    monkeypatch.setitem(sys.modules, "planner_core", None)  # restored after the test
    monkeypatch.setenv("PLANNER_CORE_PATH", str(pkg))
    mod = tool_core.load_core()
    assert mod.VALUE == 42
    assert tool_core.candidates()[0] == pkg
    assert tool_core.candidates()[1] == REPO / "custom_components/solar_ev_planner/core"


def test_core_not_found(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "planner_core", None)
    monkeypatch.delenv("PLANNER_CORE_PATH", raising=False)
    monkeypatch.setattr(tool_core, "INTEGRATED", tmp_path / "none")
    with pytest.raises(tool_core.CoreNotFound, match="PLANNER_CORE_PATH"):
        tool_core.load_core()
    assert not tool_core.core_available()
