"""Pure solar-surplus control engine."""

from __future__ import annotations

import math
from typing import Any

from .const import STATE_BLOCKED, STATE_NORMAL, STATE_SOLAR, STATE_WAITING
from .models import ControlConfig, ControlDecision, ControlInputs

MIN_CHARGING_AMPS = 8
MAX_ELAPSED_SECONDS = 60.0


class SolarControlEngine:
    """Convert normalized measurements into a conservative charging target."""

    def __init__(self, config: ControlConfig) -> None:
        self.config = config
        self.reset()

    @property
    def active(self) -> bool:
        """Whether the controller currently owns an active solar session."""
        return self._active

    def reset(self) -> None:
        """Explicitly clear all controller state, including a restart cooldown."""
        self._surplus_since: float | None = None
        self._adverse_since: float | None = None
        self._off_since: float | None = None
        self._last_evaluation: float | None = None
        self._last_vehicle: str | None = None
        self._active = False
        self._target_amps = 0
        self._smoothed_watts = 0.0
        self._ramp_credit = 0.0
        self._transient_wh = 0.0

    def stop(self, now_monotonic: float) -> None:
        """Record a physical/external stop, including manually owned sessions.

        The coordinator calls this when release completes; min-off time must be
        measured from the actual stop, not from an earlier policy decision.
        """
        now = self._safe_timestamp(now_monotonic)
        self._stop_if_active(now)
        self._surplus_since = None
        if now is not None:
            self._off_since = now
            self._last_evaluation = now

    def evaluate(self, inputs: ControlInputs, *, amp_step: int) -> ControlDecision:
        """Evaluate one point in time without raising on external sensor values."""
        invalid = self._invalid_reason(inputs, amp_step)
        if invalid:
            available = self._safe_available(inputs)
            stop_time = self._safe_timestamp(inputs.now_monotonic)
            if stop_time is None:
                stop_time = self._last_evaluation
            self._stop_if_active(stop_time)
            if self._safe_timestamp(inputs.now_monotonic) is not None:
                self._last_evaluation = stop_time
            self._surplus_since = None
            return self._decision(
                STATE_BLOCKED, 0, available, invalid, False, stop_time
            )

        now = float(inputs.now_monotonic)
        if self._last_vehicle is not None and inputs.vehicle != self._last_vehicle:
            self._restart_for_identity(now, str(inputs.vehicle))
        elif self._last_evaluation is not None and now < self._last_evaluation:
            self._restart_for_clock(now)

        first_evaluation = self._last_evaluation is None
        elapsed = self._elapsed(now)
        self._last_evaluation = now
        self._last_vehicle = str(inputs.vehicle)

        available = self._safe_available(inputs)
        raw_amps = self._raw_amps(available, float(inputs.wall_voltage_v), amp_step)
        self._update_smoothed_watts(available, elapsed, first_evaluation)
        smoothed_amps = self._raw_amps(
            self._smoothed_watts, float(inputs.wall_voltage_v), amp_step
        )

        discharge_w = max(0.0, -float(inputs.battery_power_w))
        hard_reason = self._hard_reason(inputs, discharge_w)
        if hard_reason:
            self._stop_if_active(now)
            self._surplus_since = None
            return self._decision(STATE_BLOCKED, 0, available, hard_reason, False, now)

        if not self._active:
            return self._evaluate_waiting(
                inputs,
                now,
                raw_amps,
                discharge_w,
                available,
            )

        return self._evaluate_active(
            inputs,
            now,
            elapsed,
            raw_amps,
            smoothed_amps,
            discharge_w,
            available,
            amp_step,
        )

    def _evaluate_waiting(
        self,
        inputs: ControlInputs,
        now: float,
        raw_amps: int,
        discharge_w: float,
        available: float,
    ) -> ControlDecision:
        eligible = (
            raw_amps >= MIN_CHARGING_AMPS
            and float(inputs.battery_soc)
            >= self.config.reserve_soc + self.config.reserve_entry_margin
            and float(inputs.grid_power_w) <= self.config.grid_import_limit_w
            and discharge_w <= self.config.battery_discharge_limit_w
            and inputs.forecast_allows_start
        )
        if not eligible:
            self._surplus_since = None
            return self._decision(
                STATE_WAITING,
                0,
                available,
                self._waiting_reason(inputs, raw_amps, discharge_w),
                False,
                now,
            )

        if self._surplus_since is None:
            self._surplus_since = now
        surplus_age = max(0.0, now - self._surplus_since)
        restart_remaining = self._restart_remaining(now)
        if surplus_age >= self.config.start_hold_seconds and restart_remaining <= 0.0:
            self._active = True
            self._target_amps = MIN_CHARGING_AMPS
            self._ramp_credit = 0.0
            self._adverse_since = None
            self._transient_wh = 0.0
            return self._decision(
                STATE_SOLAR, MIN_CHARGING_AMPS, available, None, True, now
            )

        reason = (
            "restart_cooldown" if restart_remaining > 0.0 else "surplus_stabilizing"
        )
        return self._decision(STATE_WAITING, 0, available, reason, False, now)

    def _evaluate_active(
        self,
        inputs: ControlInputs,
        now: float,
        elapsed: float,
        raw_amps: int,
        smoothed_amps: int,
        discharge_w: float,
        available: float,
        amp_step: int,
    ) -> ControlDecision:
        grid_import_w = max(0.0, float(inputs.grid_power_w))
        deficit_w = grid_import_w + discharge_w
        self._transient_wh += deficit_w * elapsed / 3600.0

        soft_deficit = (
            grid_import_w > self.config.grid_import_limit_w
            or discharge_w > self.config.battery_discharge_limit_w
            or raw_amps < MIN_CHARGING_AMPS
        )
        if soft_deficit:
            if self._adverse_since is None:
                self._adverse_since = now
        else:
            self._adverse_since = None

        if (
            deficit_w > 0.0
            and self._transient_wh >= self.config.max_transient_deficit_wh
        ):
            return self._stop_for_surplus_loss(now, available)
        if (
            self._adverse_since is not None
            and now - self._adverse_since >= self.config.stop_hold_seconds
        ):
            return self._stop_for_surplus_loss(now, available)

        # A cloud can lower the current immediately, but a bounded soft deficit may
        # keep the session at the minimum current until its temporal/energy guard trips.
        desired_amps = max(MIN_CHARGING_AMPS, smoothed_amps)
        if raw_amps < MIN_CHARGING_AMPS:
            desired_amps = MIN_CHARGING_AMPS

        if desired_amps < self._target_amps:
            self._target_amps = max(MIN_CHARGING_AMPS, desired_amps)
            self._ramp_credit = 0.0
        elif desired_amps > self._target_amps:
            self._ramp_credit += self.config.ramp_up_amps_per_minute * elapsed / 60.0
            increments = math.floor((self._ramp_credit + 1e-9) / amp_step) * amp_step
            if increments:
                self._target_amps = min(desired_amps, self._target_amps + increments)
                self._ramp_credit = max(0.0, self._ramp_credit - increments)

        reason = "transient_power_deficit" if soft_deficit else None
        return self._decision(
            STATE_SOLAR,
            self._target_amps,
            available,
            reason,
            True,
            now,
        )

    def _stop_for_surplus_loss(self, now: float, available: float) -> ControlDecision:
        self._stop_if_active(now)
        self._surplus_since = None
        return self._decision(STATE_NORMAL, 0, available, "surplus_lost", False, now)

    def _stop_if_active(self, now: float | None) -> None:
        """End an active session while retaining its min-off timestamp."""
        if self._active and now is not None:
            self._off_since = now
        self._active = False
        self._target_amps = 0
        self._adverse_since = None
        self._ramp_credit = 0.0
        self._transient_wh = 0.0

    def _restart_for_identity(self, now: float, vehicle: str) -> None:
        """Do not transfer temporal state or ramp credit between vehicles."""
        was_active = self._active
        self.reset()
        if was_active:
            self._off_since = now
        self._last_evaluation = now
        self._last_vehicle = vehicle

    def _restart_for_clock(self, now: float) -> None:
        """Restart temporal accounting when a monotonic source moves backwards."""
        was_active = self._active
        self._active = False
        self._target_amps = 0
        self._surplus_since = None
        self._adverse_since = None
        self._ramp_credit = 0.0
        self._transient_wh = 0.0
        if was_active or self._off_since is not None:
            self._off_since = now
        self._last_evaluation = now

    def _elapsed(self, now: float) -> float:
        if self._last_evaluation is None:
            return 0.0
        return min(max(0.0, now - self._last_evaluation), MAX_ELAPSED_SECONDS)

    @staticmethod
    def _safe_timestamp(value: Any) -> float | None:
        if _is_finite_number(value) and float(value) >= 0.0:
            return float(value)
        return None

    def _update_smoothed_watts(
        self, available: float, elapsed: float, first_evaluation: bool
    ) -> None:
        if first_evaluation or available <= self._smoothed_watts:
            self._smoothed_watts = available
            return
        if self.config.smoothing_seconds <= 0.0:
            self._smoothed_watts = available
            return
        alpha = 1.0 - math.exp(-elapsed / self.config.smoothing_seconds)
        self._smoothed_watts += alpha * (available - self._smoothed_watts)

    def _safe_available(self, inputs: ControlInputs) -> float:
        values = (
            inputs.ev_power_w,
            inputs.grid_power_w,
            inputs.battery_power_w,
            self.config.safety_margin_w,
        )
        if not all(_is_finite_number(value) for value in values):
            return 0.0
        return (
            float(inputs.ev_power_w)
            - float(inputs.grid_power_w)
            - max(0.0, -float(inputs.battery_power_w))
            - self.config.safety_margin_w
        )

    def _raw_amps(self, watts: float, voltage: float, amp_step: int) -> int:
        amps = min(max(0, math.floor(watts / voltage)), self.config.max_solar_amps)
        amps -= amps % amp_step
        return amps if amps >= MIN_CHARGING_AMPS else 0

    def _invalid_reason(self, inputs: ControlInputs, amp_step: int) -> str | None:
        if not self._valid_config() or not _is_positive_int(amp_step):
            return "invalid_controller_configuration"
        actual_values = (
            inputs.now_monotonic,
            inputs.grid_power_w,
            inputs.battery_power_w,
            inputs.battery_soc,
            inputs.ev_power_w,
            inputs.wall_voltage_v,
        )
        if not all(_is_finite_number(value) for value in actual_values):
            return "invalid_or_unavailable_input"
        if not inputs.inputs_fresh:
            return "stale_or_unavailable_input"
        if not 100.0 <= float(inputs.wall_voltage_v) <= 260.0:
            return "invalid_wall_voltage"
        if not 0.0 <= float(inputs.battery_soc) <= 100.0:
            return "invalid_power_or_soc"
        if float(inputs.ev_power_w) < 0.0 or float(inputs.now_monotonic) < 0.0:
            return "invalid_power_or_soc"
        if not isinstance(inputs.vehicle, str) or not inputs.vehicle:
            return "vehicle_identity_ambiguous"
        return None

    def _valid_config(self) -> bool:
        positive = (
            self.config.max_solar_amps,
            self.config.ramp_up_amps_per_minute,
        )
        nonnegative = (
            self.config.reserve_soc,
            self.config.reserve_entry_margin,
            self.config.safety_margin_w,
            self.config.start_hold_seconds,
            self.config.stop_hold_seconds,
            self.config.battery_discharge_limit_w,
            self.config.grid_import_limit_w,
            self.config.smoothing_seconds,
            self.config.min_off_seconds,
            self.config.max_transient_deficit_wh,
            self.config.hard_grid_import_w,
            self.config.hard_battery_discharge_w,
        )
        return (
            all(
                _is_finite_number(value) and float(value) >= 0.0
                for value in nonnegative
            )
            and all(
                _is_finite_number(value) and float(value) > 0.0 for value in positive
            )
            and self.config.max_solar_amps >= MIN_CHARGING_AMPS
            and self.config.hard_grid_import_w >= self.config.grid_import_limit_w
            and self.config.hard_battery_discharge_w
            >= self.config.battery_discharge_limit_w
            and self.config.reserve_soc + self.config.reserve_entry_margin <= 100.0
        )

    def _hard_reason(self, inputs: ControlInputs, discharge_w: float) -> str | None:
        if float(inputs.battery_soc) < self.config.reserve_soc:
            return "battery_reserve"
        if inputs.blocker:
            return inputs.blocker
        if float(inputs.grid_power_w) > self.config.hard_grid_import_w:
            return "grid_importing"
        if discharge_w > self.config.hard_battery_discharge_w:
            return "battery_discharging"
        return None

    def _waiting_reason(
        self, inputs: ControlInputs, raw_amps: int, discharge_w: float
    ) -> str:
        if not inputs.forecast_allows_start:
            return inputs.forecast_reason or "forecast_blocked"
        if (
            float(inputs.battery_soc)
            < self.config.reserve_soc + self.config.reserve_entry_margin
        ):
            return "battery_reserve"
        if discharge_w > self.config.battery_discharge_limit_w:
            return "battery_discharging"
        if float(inputs.grid_power_w) > self.config.grid_import_limit_w:
            return "grid_importing"
        return (
            "insufficient_surplus"
            if raw_amps < MIN_CHARGING_AMPS
            else "surplus_stabilizing"
        )

    def _decision(
        self,
        state: str,
        amps: int,
        available: float,
        reason: str | None,
        should_charge: bool,
        now: float | None,
    ) -> ControlDecision:
        return ControlDecision(
            state,
            amps,
            available,
            reason,
            should_charge,
            self._smoothed_watts,
            self._restart_remaining(now),
            self._transient_wh,
        )

    def _restart_remaining(self, now: float | None) -> float:
        if self._off_since is None or now is None:
            return 0.0
        return max(0.0, self.config.min_off_seconds - max(0.0, now - self._off_since))


def _is_finite_number(value: Any) -> bool:
    """Return whether a sensor/config value is a real numeric scalar, not bool."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
