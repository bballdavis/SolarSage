"""Dependency-free tests for Rivian schedule contracts."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

_SPEC = spec_from_file_location(
    "rivian_schedule_under_test",
    Path(__file__).parents[1] / "custom_components/rivian/schedule.py",
)
assert _SPEC and _SPEC.loader
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
canonical_schedule_hash = _MODULE.canonical_schedule_hash
canonical_schedule_contract_hash = _MODULE.canonical_schedule_contract_hash
normalize_schedules = _MODULE.normalize_schedules


def _schedule(**updates):
    value = {
        "weekDays": ["Monday", "Friday"],
        "startTime": 60,
        "duration": 120,
        "location": {"latitude": 1.0, "longitude": 2.0},
        "amperage": 16,
        "enabled": True,
    }
    value.update(updates)
    return value


def test_hash_is_stable_for_mapping_order():
    schedule = _schedule()
    reordered = {key: schedule[key] for key in reversed(schedule)}
    assert canonical_schedule_hash([schedule]) == canonical_schedule_hash([reordered])


@pytest.mark.parametrize("field,value", [("amperage", 15), ("startTime", 1440), ("enabled", "true")])
def test_schedule_validation_rejects_invalid_values(field, value):
    with pytest.raises((TypeError, ValueError)):
        normalize_schedules([_schedule(**{field: value})])


def test_normalization_preserves_complete_list_and_extra_fields():
    first = _schedule(id="first")
    first["location"]["radius"] = 150
    schedules = [first, _schedule(id="second", enabled=False)]
    result = normalize_schedules(schedules)
    assert result == schedules
    assert result is not schedules


def test_contract_hash_ignores_server_metadata_but_not_control_changes():
    requested = _schedule()
    verified = _schedule(id="server-id")
    verified["location"]["radius"] = 150
    assert canonical_schedule_contract_hash([requested]) == canonical_schedule_contract_hash(
        [verified]
    )
    verified["amperage"] = 18
    assert canonical_schedule_contract_hash([requested]) != canonical_schedule_contract_hash(
        [verified]
    )
