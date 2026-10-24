"""Pure, unit-normalized forecast gate for solar charging."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True, slots=True)
class ForecastConfig:
    mode: str = "off"
    confidence: float = 0.75
    battery_capacity_kwh: float = 0.0
    battery_target_soc: float = 80.0
    household_budget_kwh: float = 5.0
    minimum_session_minutes: float = 15.0


@dataclass(frozen=True, slots=True)
class ForecastInputs:
    remaining_today_kwh: float
    next_hour_kwh: float
    battery_soc: float
    wall_voltage_v: float
    data_fresh: bool = True
    inputs_valid: bool = True


@dataclass(frozen=True, slots=True)
class ForecastDecision:
    allow_start: bool
    reason: str
    remaining_kwh: float
    next_hour_kwh: float
    battery_needed_kwh: float
    ev_budget_kwh: float


def choose_forecast(config: ForecastConfig, inputs: ForecastInputs) -> ForecastDecision:
    """Assess forecast viability; malformed external values always fail safely."""
    mode = config.mode
    values = _calculation_values(config, inputs)

    if mode not in {"off", "advisory", "protect"}:
        return _decision(False, "forecast_invalid", values)
    if mode == "off":
        # Forecasting is disabled: retain safe display values rather than implying
        # that the forecast itself predicted zero production.
        return _decision(True, "forecast_off", values)

    valid = _valid_values(config, inputs)
    fresh = inputs.data_fresh is True
    supplied = inputs.inputs_valid is True
    if not valid or not supplied:
        reason = "forecast_invalid"
    elif not fresh:
        reason = "forecast_stale"
    else:
        minimum_kwh = _minimum_session_kwh(config, inputs)
        sufficient = (
            values.ev_budget_kwh >= minimum_kwh
            and values.next_hour_kwh * float(config.confidence) >= minimum_kwh
        )
        reason = (
            "forecast_allows_start" if sufficient else "forecast_insufficient_energy"
        )

    if mode == "advisory":
        return _decision(True, reason, values)
    return _decision(reason == "forecast_allows_start", reason, values)


@dataclass(frozen=True, slots=True)
class _CalculationValues:
    remaining_kwh: float
    next_hour_kwh: float
    battery_needed_kwh: float
    ev_budget_kwh: float


def _calculation_values(
    config: ForecastConfig, inputs: ForecastInputs
) -> _CalculationValues:
    """Produce safe diagnostics even when the gate itself cannot trust its input."""
    remaining = _nonnegative_finite(inputs.remaining_today_kwh)
    next_hour = _nonnegative_finite(inputs.next_hour_kwh)
    capacity = _nonnegative_finite(config.battery_capacity_kwh)
    target = _bounded_finite(config.battery_target_soc, 0.0, 100.0)
    battery_soc = _bounded_finite(inputs.battery_soc, 0.0, 100.0)
    confidence = _bounded_finite(config.confidence, 0.0, 1.0)
    household = _nonnegative_finite(config.household_budget_kwh)
    battery_needed = max(0.0, capacity * (target - battery_soc) / 100.0)
    budget = max(0.0, remaining * confidence - battery_needed - household)
    return _CalculationValues(remaining, next_hour, battery_needed, budget)


def _valid_values(config: ForecastConfig, inputs: ForecastInputs) -> bool:
    numeric_values = (
        config.confidence,
        config.battery_capacity_kwh,
        config.battery_target_soc,
        config.household_budget_kwh,
        config.minimum_session_minutes,
        inputs.remaining_today_kwh,
        inputs.next_hour_kwh,
        inputs.battery_soc,
        inputs.wall_voltage_v,
    )
    if not all(_is_finite_number(value) for value in numeric_values):
        return False
    return (
        0.0 < float(config.confidence) <= 1.0
        and float(config.battery_capacity_kwh) > 0.0
        and 0.0 <= float(config.battery_target_soc) <= 100.0
        and float(config.household_budget_kwh) >= 0.0
        and float(config.minimum_session_minutes) >= 5.0
        and float(inputs.remaining_today_kwh) >= 0.0
        and float(inputs.next_hour_kwh) >= 0.0
        and 0.0 <= float(inputs.battery_soc) <= 100.0
        and 100.0 <= float(inputs.wall_voltage_v) <= 260.0
    )


def _minimum_session_kwh(config: ForecastConfig, inputs: ForecastInputs) -> float:
    return (
        8.0
        * float(inputs.wall_voltage_v)
        * float(config.minimum_session_minutes)
        / 60000.0
    )


def _decision(
    allow_start: bool, reason: str, values: _CalculationValues
) -> ForecastDecision:
    return ForecastDecision(
        allow_start,
        reason,
        values.remaining_kwh,
        values.next_hour_kwh,
        values.battery_needed_kwh,
        values.ev_budget_kwh,
    )


def _is_finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _nonnegative_finite(value: Any) -> float:
    return float(value) if _is_finite_number(value) and float(value) >= 0.0 else 0.0


def _bounded_finite(value: Any, lower: float, upper: float) -> float:
    if not _is_finite_number(value):
        return 0.0
    return min(upper, max(lower, float(value)))


evaluate_forecast = choose_forecast
