"""The tools' entity mapping: ``tools/config.example.json`` overlaid with the owner's file.

The example holds the owner's real entity ids, so the tools work without a config file.
A config file (``--config``, else ``tools/config.json`` / ``config.yaml`` if present) only
needs the keys that differ; nested mappings are merged key by key. JSON always works; YAML
needs PyYAML, which Home Assistant's own Python has but a plain Python may not.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

TOOLS = Path(__file__).resolve().parent
EXAMPLE = TOOLS / "config.example.json"
OWNER_FILES = (TOOLS / "config.json", TOOLS / "config.yaml", TOOLS / "config.yml")
OUT_DIR = TOOLS / "out"  # git-ignored: snapshots, replays and logs hold household data

ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
SITE_ROLES = (
    "pv_power",
    "grid_power",
    "battery_power",
    "battery_soc",
    "battery_capacity",
    "wall_power",
    "wall_current",
    "wall_connected",
    "wall_contactor",
    "forecast_today",
    "forecast_tomorrow",
    "forecast_remaining_today",
    "forecast_last_polled",
    "free_hours",
)
POWER_ROLES = ("pv_power", "grid_power", "battery_power", "wall_power")
CAR_KINDS = ("tesla", "rivian")
# car keys that are not entity ids
CAR_META = ("kind", "name")
TO_KW = {"W": 0.001, "kW": 1.0, "MW": 1000.0}


class ConfigError(ValueError):
    """The config file is missing, unreadable or inconsistent."""


def _merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def _read(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError(f"cannot read {path}: {err}") from err
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # optional: only YAML configs need it
        except ImportError as err:
            raise ConfigError(
                f"{path.name} is YAML but PyYAML is not installed; use a JSON config"
            ) from err
        data = yaml.safe_load(text) or {}
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise ConfigError(f"{path} is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a mapping at the top level")
    return data


@dataclass(frozen=True)
class Config:
    """The merged mapping plus the file it came from (None = the example only)."""

    data: dict[str, Any]
    source: Path | None = None

    @property
    def site(self) -> dict[str, str]:
        return self.data["site"]

    @property
    def cars(self) -> dict[str, dict[str, str]]:
        return self.data.get("cars", {})

    @property
    def planner(self) -> dict[str, str]:
        """The planner integration's own entities (absent until it is installed)."""
        return self.data.get("planner", {})

    def car_entity(self, car_id: str, key: str) -> str | None:
        return self.cars.get(car_id, {}).get(key) or None

    def unit(self, role: str) -> str | None:
        return self.data.get("units", {}).get(role)

    def kw_scale(self, role: str) -> float:
        """Factor that turns the role's configured unit into kW."""
        unit = self.unit(role) or ("kW" if role == "wall_power" else "W")
        if unit not in TO_KW:
            raise ConfigError(f"units.{role} must be one of {', '.join(TO_KW)}, not {unit!r}")
        return TO_KW[unit]

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.data.get(name) or {})

    def entities(self) -> Iterator[tuple[str, str]]:
        """(role, entity id) for every mapped entity; car roles are '<car>.<key>'."""
        for role in SITE_ROLES:
            if eid := self.site.get(role):
                yield role, eid
        for car_id, car in self.cars.items():
            for key, eid in car.items():
                if key not in CAR_META and eid:
                    yield f"{car_id}.{key}", eid
        for key, eid in self.planner.items():
            if eid:
                yield f"planner.{key}", eid

    def entity_ids(self) -> list[str]:
        return sorted({eid for _, eid in self.entities()})


def validate(data: Mapping[str, Any]) -> list[str]:
    """Problems in a merged config (empty when it is usable)."""
    problems = []
    site = data.get("site")
    if not isinstance(site, Mapping):
        return ["'site' must be a mapping of role -> entity id"]
    for role in SITE_ROLES:
        if role != "free_hours" and not site.get(role):
            problems.append(f"site.{role} is not mapped")
    for car_id, car in (data.get("cars") or {}).items():
        if not isinstance(car, Mapping):
            problems.append(f"cars.{car_id} must be a mapping")
            continue
        if car.get("kind") not in CAR_KINDS:
            problems.append(f"cars.{car_id}.kind must be one of {', '.join(CAR_KINDS)}")
        for key in ("soc", "limit", "plugged", "switch", "current"):
            if not car.get(key):
                problems.append(f"cars.{car_id}.{key} is not mapped")
    pairs = [(f"site.{k}", v) for k, v in site.items()]
    for car_id, car in (data.get("cars") or {}).items():
        if isinstance(car, Mapping):
            pairs += [(f"cars.{car_id}.{k}", v) for k, v in car.items() if k not in CAR_META]
    planner = data.get("planner") or {}
    if not isinstance(planner, Mapping):
        problems.append("'planner' must be a mapping of role -> entity id")
    else:
        pairs += [(f"planner.{k}", v) for k, v in planner.items()]
    for where, eid in pairs:
        if eid and (not isinstance(eid, str) or not ENTITY_ID.match(eid)):
            problems.append(f"{where} = {eid!r} is not an entity id")
    for role, unit in (data.get("units") or {}).items():
        if unit not in TO_KW:
            problems.append(f"units.{role} must be one of {', '.join(TO_KW)}")
    return problems


def load_config(path: str | Path | None = None) -> Config:
    """The example overlaid with ``path`` (or the first owner file that exists)."""
    data = _read(EXAMPLE)
    source = None
    if path is not None:
        source = Path(path)
        if not source.is_file():
            raise ConfigError(f"config file {source} does not exist")
    else:
        source = next((p for p in OWNER_FILES if p.is_file()), None)
    if source is not None:
        data = _merge(data, _read(source))
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    if problems := validate(data):
        raise ConfigError("; ".join(problems))
    return Config(data, source)
