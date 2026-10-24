"""Focused tests for the pure forecast gate."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = "_solar_forecast_unit_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "custom_components/rivian/solar_ev_charging")]
sys.modules.setdefault(PACKAGE, package)
spec = spec_from_file_location(
    f"{PACKAGE}.forecast",
    ROOT / "custom_components/rivian/solar_ev_charging/forecast.py",
)
assert spec and spec.loader
forecast = module_from_spec(spec)
sys.modules[spec.name] = forecast
spec.loader.exec_module(forecast)
ForecastConfig = forecast.ForecastConfig
ForecastInputs = forecast.ForecastInputs
choose_forecast = forecast.choose_forecast

source_spec = spec_from_file_location(
    f"{PACKAGE}.forecast_sources",
    ROOT / "custom_components/rivian/solar_ev_charging/forecast_sources.py",
)
assert source_spec and source_spec.loader
forecast_sources = module_from_spec(source_spec)
sys.modules[source_spec.name] = forecast_sources
source_spec.loader.exec_module(forecast_sources)


def _inputs(**changes):
    values = {
        "remaining_today_kwh": 20.0,
        "next_hour_kwh": 8.0,
        "battery_soc": 50.0,
        "wall_voltage_v": 240.0,
        "data_fresh": True,
        "inputs_valid": True,
    }
    values.update(changes)
    return ForecastInputs(**values)


def _config(**changes):
    values = {
        "mode": "protect",
        "confidence": 0.75,
        "battery_capacity_kwh": 60.0,
        "battery_target_soc": 60.0,
        "household_budget_kwh": 2.0,
        "minimum_session_minutes": 30.0,
    }
    values.update(changes)
    return ForecastConfig(**values)


def test_protect_allows_when_daily_and_next_hour_budget_cover_session():
    decision = choose_forecast(_config(), _inputs())
    assert decision.allow_start
    assert decision.reason == "forecast_allows_start"
    assert decision.ev_budget_kwh == 7.0


def test_protect_rejects_low_remaining_or_next_hour_energy():
    assert not choose_forecast(_config(), _inputs(remaining_today_kwh=4)).allow_start
    assert not choose_forecast(_config(), _inputs(next_hour_kwh=1)).allow_start


def test_protect_rejects_stale_invalid_and_bad_voltage_without_raising():
    stale = choose_forecast(_config(), _inputs(data_fresh=False))
    assert not stale.allow_start and stale.reason == "forecast_stale"
    invalid = choose_forecast(_config(), _inputs(inputs_valid=False))
    assert not invalid.allow_start and invalid.reason == "forecast_invalid"
    malformed = choose_forecast(_config(), _inputs(wall_voltage_v=float("nan")))
    assert not malformed.allow_start and malformed.reason == "forecast_invalid"


def test_advisory_reports_data_but_never_blocks_and_off_has_no_gate():
    advisory = choose_forecast(_config(mode="advisory"), _inputs(data_fresh=False))
    assert advisory.allow_start and advisory.reason == "forecast_stale"
    off = choose_forecast(_config(mode="off"), _inputs(data_fresh=False))
    assert off.allow_start and off.reason == "forecast_off"
    assert off.remaining_kwh == 20


@pytest.mark.parametrize(
    "config_changes,input_changes",
    [
        ({"mode": "invalid"}, {}),
        ({"confidence": 0}, {}),
        ({"battery_capacity_kwh": 0}, {}),
        ({"minimum_session_minutes": 4}, {}),
        ({}, {"remaining_today_kwh": -1}),
        ({}, {"next_hour_kwh": None}),
        ({}, {"battery_soc": "invalid"}),
        ({}, {"wall_voltage_v": True}),
    ],
)
def test_protect_rejects_invalid_configuration_and_measurements(
    config_changes, input_changes
):
    decision = choose_forecast(_config(**config_changes), _inputs(**input_changes))
    assert not decision.allow_start
    assert decision.reason == "forecast_invalid"


def test_advisory_never_blocks_invalid_data_but_protect_requires_freshness():
    advisory = choose_forecast(
        _config(mode="advisory"), _inputs(remaining_today_kwh=None)
    )
    assert advisory.allow_start and advisory.reason == "forecast_invalid"
    stale = choose_forecast(_config(), _inputs(data_fresh=False))
    assert not stale.allow_start and stale.reason == "forecast_stale"


class _State:
    def __init__(self, state, attributes):
        self.state = state
        self.attributes = attributes


def test_solcast_percentile_attributes_keep_the_sensor_native_unit():
    remaining = _State(
        "12000",
        {"unit_of_measurement": "Wh", "estimate": 12000, "estimate10": 9800},
    )
    next_hour = _State(
        "2200",
        {"unit_of_measurement": "Wh", "estimate": 2200, "estimate10": 1600},
    )
    assert (
        forecast_sources.forecast_energy_kwh(
            forecast_sources.forecast_attribute_value(remaining, "estimate10"), "Wh"
        )
        == 9.8
    )
    assert (
        forecast_sources.forecast_energy_kwh(
            forecast_sources.forecast_attribute_value(next_hour, "estimate10"), "Wh"
        )
        == 1.6
    )


def test_forecast_source_helpers_reject_missing_percentiles_and_invalid_numbers():
    state = _State("12", {"unit_of_measurement": "kWh", "estimate": 12})
    assert forecast_sources.forecast_attribute_value(state, "estimate10") is None
    for value in (True, float("nan"), float("inf"), -1, "unavailable"):
        assert forecast_sources.forecast_energy_kwh(value, "Wh") is None


def test_energy_helpers_support_generic_state_and_legitimate_night_zeroes():
    assert forecast_sources.forecast_energy_kwh("2500", "Wh") == 2.5
    assert forecast_sources.forecast_energy_kwh("0", "kWh") == 0


def test_source_timestamp_requires_aware_non_future_value():
    from datetime import UTC, datetime

    now = datetime(2026, 9, 19, 21, 0, tzinfo=UTC)
    assert (
        forecast_sources.forecast_source_age_seconds("2026-09-19T16:54:00+00:00", now)
        == 14760
    )
    assert (
        forecast_sources.forecast_source_age_seconds("2026-09-19T21:01:00+00:00", now)
        is None
    )
    assert (
        forecast_sources.forecast_source_age_seconds("2026-09-19T16:54:00", now) is None
    )
