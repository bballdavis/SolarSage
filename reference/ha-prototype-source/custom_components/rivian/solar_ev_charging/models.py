"""Dependency-free data models for Solar EV Charging."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ControlConfig:
    """Tunable controller thresholds."""

    reserve_soc: float = 25.0
    reserve_entry_margin: float = 5.0
    safety_margin_w: float = 500.0
    start_hold_seconds: int = 120
    stop_hold_seconds: int = 30
    battery_discharge_limit_w: float = 250.0
    grid_import_limit_w: float = 500.0
    max_solar_amps: int = 48
    ramp_up_amps_per_minute: float = 2.0
    smoothing_seconds: float = 60.0
    min_off_seconds: float = 180.0
    max_transient_deficit_wh: float = 10.0
    hard_grid_import_w: float = 1500.0
    hard_battery_discharge_w: float = 1000.0


@dataclass(slots=True, frozen=True)
class ControlInputs:
    """Normalized point-in-time inputs."""

    now_monotonic: float
    grid_power_w: float
    battery_power_w: float
    battery_soc: float
    ev_power_w: float
    wall_voltage_v: float
    vehicle: str | None
    inputs_fresh: bool = True
    blocker: str | None = None
    forecast_allows_start: bool = True
    forecast_reason: str | None = None


@dataclass(slots=True, frozen=True)
class ControlDecision:
    """Controller output for one evaluation."""

    state: str
    target_amps: int
    available_watts: float
    reason: str | None
    should_charge: bool
    smoothed_watts: float = 0.0
    restart_remaining_seconds: float = 0.0
    transient_deficit_wh: float = 0.0
