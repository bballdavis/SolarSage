"""The stable API: names, no Home Assistant imports, HA-shaped inputs end to end."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from custom_components.solar_ev_planner import core as planner_core

from . import ha_fixture

PKG = Path(planner_core.__file__).resolve().parent


def test_public_names_are_exported():
    for name in planner_core.__all__:
        assert hasattr(planner_core, name), name
    assert planner_core.__version__


def test_no_home_assistant_or_third_party_imports():
    allowed = {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "math",
        "statistics",
        "typing",
    }
    for f in PKG.glob("*.py"):
        for line in f.read_text().splitlines():
            s = line.strip()
            if s.startswith("import ") or (s.startswith("from ") and " import " in s):
                mod = s.split()[1]
                if mod.startswith("."):
                    continue
                assert mod.split(".")[0] in allowed, f"{f.name}: {s}"
        assert "homeassistant" not in f.read_text().replace("Home Assistant", "")


def test_ha_shaped_fixture_end_to_end():
    fx = ha_fixture.build_fixture(datetime(2026, 6, 15, 12, 0))
    snap, d = ha_fixture.run(fx)
    assert snap.load.n_days == 14 and len(snap.forecast) == 96
    assert snap.measured is not None and abs(snap.measured.house - 0.95) < 1e-6
    assert d.car_id == "rivian" and d.shadow and d.mode == "day"
    assert d.command_for("tesla").amps is None
    assert d.amps is not None and d.amps >= 8
    assert d.debug["tonight_car"] == "tesla"  # its weekday plan needs tonight's free hours
    tesla = next(o for o in d.outlooks if o.car_id == "tesla")
    assert tesla.by == datetime(2026, 6, 16, 7, 30) and tesla.free_kwh > 0
