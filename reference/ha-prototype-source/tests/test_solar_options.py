"""Native configuration groups and cross-field safety constraints."""

import pytest

pytest.importorskip("homeassistant")

from custom_components.rivian.config_flow import OPTIONS_FLOW
from custom_components.rivian.solar_ev_charging.options import (
    OPTION_GROUPS,
    solar_group_schema,
    validate_solar_options,
)


def test_each_option_is_owned_by_exactly_one_solar_page():
    seen = set()
    for group, fields in OPTION_GROUPS.items():
        assert group in OPTIONS_FLOW
        assert not seen & fields
        seen.update(fields)
        assert {key.schema for key in solar_group_schema(group, {}).schema} == fields


def test_solar_page_preserves_existing_numeric_defaults():
    schema = solar_group_schema(
        "solar_tuning", {"reserve_soc": 40, "min_off_seconds": 240}
    )
    values = schema({})
    assert values["reserve_soc"] == 40
    assert values["min_off_seconds"] == 240


@pytest.mark.parametrize(
    "options,reason",
    [
        ({"reserve_soc": 95, "reserve_entry_margin": 10}, "solar_reserve_range"),
        ({"grid_import_limit_w": 900, "hard_grid_import_w": 500}, "solar_hard_limits"),
        (
            {"battery_discharge_limit_w": 900, "hard_battery_discharge_w": 500},
            "solar_hard_limits",
        ),
        ({"forecast_mode": "protect"}, "solar_forecast_required"),
        (
            {
                "forecast_mode": "protect",
                "forecast_remaining_entity": "sensor.remaining",
                "forecast_next_hour_entity": "sensor.next_hour",
                "battery_capacity_kwh": 27,
            },
            "solar_forecast_required",
        ),
    ],
)
def test_invalid_cross_field_configuration_is_rejected(options, reason):
    assert validate_solar_options(options) == reason


def test_complete_forecast_configuration_is_valid():
    assert (
        validate_solar_options(
            {
                "forecast_mode": "protect",
                "forecast_remaining_entity": "sensor.remaining",
                "forecast_next_hour_entity": "sensor.next_hour",
                "forecast_source_updated_entity": "sensor.source_last_polled",
                "forecast_value_field": "estimate10",
                "forecast_source_max_age_hours": 6,
                "battery_capacity_kwh": 27,
                "battery_target_soc": 80,
                "reserve_soc": 25,
            }
        )
        is None
    )
