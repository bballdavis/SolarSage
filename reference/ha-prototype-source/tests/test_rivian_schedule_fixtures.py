"""Replay schedule ownership and shape fixtures through the real validator."""

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FIXTURE = json.loads(
    (ROOT / "tests/fixtures/exploratory/synthetic_schedule_cases.json").read_text(
        encoding="utf-8"
    )
)
SPEC = spec_from_file_location(
    "rivian_schedule_fixture_under_test", ROOT / "custom_components/rivian/schedule.py"
)
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_fixture_is_explicitly_synthetic_and_preserves_schedule_shape():
    assert FIXTURE["synthetic"] is True
    result = MODULE.normalize_schedules([FIXTURE["valid"]])[0]
    assert result["weekDays"] == ["Monday", "Friday"]
    assert result["startTime"] == 60
    assert result["duration"] == 120
    assert result["amperage"] == 16
    assert result["owner"] == "integration"


def test_contract_hash_ignores_server_metadata_but_retains_control_ownership():
    requested = deepcopy(FIXTURE["valid"])
    verified = deepcopy(requested)
    verified.update(FIXTURE["server_metadata_variant"])
    assert MODULE.canonical_schedule_contract_hash(
        [requested]
    ) == MODULE.canonical_schedule_contract_hash([verified])
    verified["amperage"] = 18
    assert MODULE.canonical_schedule_contract_hash(
        [requested]
    ) != MODULE.canonical_schedule_contract_hash([verified])


def test_normalization_preserves_the_complete_schedule_list():
    schedules = MODULE.normalize_schedules(FIXTURE["full_list"])
    assert [schedule["id"] for schedule in schedules] == ["first", "second"]
    assert schedules[1]["enabled"] is False


@pytest.mark.parametrize("invalid", FIXTURE["invalid"], ids=lambda item: item["field"])
def test_fixture_shape_edges_are_rejected_by_schedule_validator(invalid):
    schedule = deepcopy(FIXTURE["valid"])
    schedule[invalid["field"]] = invalid["value"]
    with pytest.raises((TypeError, ValueError)):
        MODULE.normalize_schedules([schedule])
