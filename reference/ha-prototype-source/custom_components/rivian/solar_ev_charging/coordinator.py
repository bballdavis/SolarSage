"""Feedback-verified, fail-safe solar EV charging coordinator."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import logging
import math
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from ..const import WEEK_DAYS_ORDERED
from ..schedule import canonical_schedule_hash
from .const import (
    CONF_BATTERY_DISCHARGE_LIMIT_W,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_BLOCKERS,
    CONF_COMMAND_INTERVAL_SECONDS,
    CONF_DRY_RUN,
    CONF_FEEDBACK_TIMEOUT_SECONDS,
    CONF_FORECAST_CONFIDENCE,
    CONF_FORECAST_MODE,
    CONF_FORECAST_NEXT_HOUR,
    CONF_FORECAST_REMAINING,
    CONF_FORECAST_SOURCE_MAX_AGE_HOURS,
    CONF_FORECAST_SOURCE_UPDATED,
    CONF_FORECAST_VALUE_FIELD,
    CONF_GRID_IMPORT_LIMIT_W,
    CONF_GRID_POWER,
    CONF_HARD_BATTERY_DISCHARGE_W,
    CONF_HARD_GRID_IMPORT_W,
    CONF_MAX_SOLAR_AMPS,
    CONF_MAX_TRANSIENT_DEFICIT_WH,
    CONF_MIN_OFF_SECONDS,
    CONF_RESERVE_ENTRY_MARGIN,
    CONF_RESERVE_SOC,
    CONF_RIVIAN_CONNECTED,
    CONF_RIVIAN_CONTROL_CONFIRMED,
    CONF_RIVIAN_HOME,
    CONF_RIVIAN_LIMIT,
    CONF_RIVIAN_SOC,
    CONF_RIVIAN_SWITCH,
    CONF_SAFETY_MARGIN_W,
    CONF_SMOOTHING_SECONDS,
    CONF_SOLAR_ENABLED,
    CONF_START_HOLD_SECONDS,
    CONF_STOP_HOLD_SECONDS,
    CONF_TESLA_AMPS,
    CONF_TESLA_CONNECTED,
    CONF_TESLA_HOME,
    CONF_TESLA_LIMIT,
    CONF_TESLA_SOC,
    CONF_TESLA_SWITCH,
    CONF_VEHICLE_MODE,
    CONF_WALL_CONNECTED,
    CONF_WALL_POWER,
    CONF_WALL_VOLTAGE,
    DEFAULT_BATTERY_DISCHARGE_LIMIT_W,
    DEFAULT_DRY_RUN,
    DEFAULT_FORECAST_SOURCE_MAX_AGE_HOURS,
    DEFAULT_GRID_IMPORT_LIMIT_W,
    DEFAULT_MAX_SOLAR_AMPS,
    DEFAULT_RESERVE_ENTRY_MARGIN,
    DEFAULT_RESERVE_SOC,
    DEFAULT_SAFETY_MARGIN_W,
    DEFAULT_START_HOLD_SECONDS,
    DEFAULT_STOP_HOLD_SECONDS,
    DOMAIN,
    FORECAST_VALUE_FIELDS,
    RIVIAN_LEASE_MINUTES,
    RIVIAN_RENEW_SECONDS,
    STATE_FAULT,
    STATE_MANUAL,
    STATE_NORMAL,
    STATE_SOLAR,
    STORE_VERSION,
    UPDATE_INTERVAL,
    VEHICLE_MODE_BOTH,
    VEHICLE_RIVIAN,
    VEHICLE_TESLA,
)
from .engine import SolarControlEngine
from .forecast import ForecastConfig, ForecastInputs, choose_forecast
from .forecast_sources import (
    forecast_attribute_value,
    forecast_energy_kwh,
    forecast_source_age_seconds,
)
from .input_diagnostics import measurement, state_age
from .models import ControlConfig, ControlDecision, ControlInputs

_LOGGER = logging.getLogger(__name__)
# The wall source is normally polled every 30 seconds; allow bounded scheduler jitter.
POWER_AGE, WALL_AGE, VEHICLE_AGE = 180, 75, 300
IDLE_POWER_W, SERVICE_TIMEOUT = 150.0, 30.0
STOP_BACKOFF = (15, 60, 300)


class SolarEvCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Own EV actuation only when it is durable and physically confirmed."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry, self.config, self.options = entry, entry.options, entry.options
        self.store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._lock, self._runtime = asyncio.Lock(), {}
        self._rivian_vehicle_id: str | None = None
        self._engine = SolarControlEngine(self._control_config())
        self._last_rivian_renewal = self._last_command_monotonic = 0.0
        self._last_command: str | None = None
        self._fault: str | None = None
        self._last_vehicle: str | None = None
        self._observed_power_w: float | None = None
        self._observed_amps: float | None = None
        self._last_feedback_at: datetime | None = None
        self._command_acknowledged = self._shutdown_complete = False
        self._initialized = False
        self._forecast: dict[str, Any] = {}
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            always_update=True,
        )

    @property
    def enabled(self) -> bool:
        return bool(self._runtime.get("enabled", True))

    @property
    def manual_override(self) -> bool:
        return bool(self._runtime.get("manual_override", False))

    @property
    def dry_run(self) -> bool:
        return bool(self.options.get(CONF_DRY_RUN, DEFAULT_DRY_RUN))

    @property
    def rivian_compatible(self) -> bool:
        return bool(self._runtime.get("rivian_compatible")) and not bool(
            self._runtime.get("rivian_write_blocked")
        )

    @property
    def rivian_enabled(self) -> bool:
        return self.options.get(CONF_VEHICLE_MODE, VEHICLE_MODE_BOTH) in (
            VEHICLE_RIVIAN,
            VEHICLE_MODE_BOTH,
        )

    @property
    def tesla_enabled(self) -> bool:
        return self.options.get(CONF_VEHICLE_MODE, VEHICLE_MODE_BOTH) in (
            VEHICLE_TESLA,
            VEHICLE_MODE_BOTH,
        )

    async def async_initialize(self) -> None:
        """Recover a prior actuator journal even if the entry was changed to dry-run."""
        self._runtime = await self.store.async_load() or {}
        for key, value in (
            ("enabled", True),
            ("manual_override", False),
            ("rivian_compatible", False),
            ("rivian_write_blocked", False),
            ("journal", None),
            ("commission", {}),
        ):
            self._runtime.setdefault(key, value)
        self._fault = self._runtime.get("fault")
        if (
            self._fault
            or self._runtime.get("journal")
            or self._runtime.get("probe_journal")
        ):
            self._runtime["enabled"] = False
            self._runtime["manual_override"] = False
            await self.store.async_save(self._runtime)
        if not self.options.get(CONF_SOLAR_ENABLED, True):
            self._runtime["enabled"] = False
        self._rivian_vehicle_id = (
            self._resolve_rivian_vehicle_id() if self.rivian_enabled else None
        )
        async with self._lock:
            if self._runtime.get("probe_journal"):
                try:
                    await self._async_restore_probe_journal("startup_recovery")
                except HomeAssistantError as err:
                    await self._async_latch_fault(
                        f"startup_probe_restore_failed: {err}", cleanup=False
                    )
            if self._runtime.get("journal"):
                await self._async_safe_release_locked("startup_recovery")
            await self.store.async_save(self._runtime)
            self._initialized = True

    async def async_set_enabled(self, enabled: bool) -> None:
        async with self._lock:
            if not enabled:
                await self._async_safe_release_locked("disabled")
                self._engine.reset()
            self._runtime["enabled"] = enabled
            await self.store.async_save(self._runtime)
        await self.async_request_refresh()

    async def async_charge_now(self) -> None:
        async with self._lock:
            reason = self._command_readiness_reason(
                self._connected_vehicle(), commissioning=False
            )
            if reason:
                raise HomeAssistantError(f"Manual charge is blocked: {reason}")
            if self._fault or self._runtime.get("journal"):
                raise HomeAssistantError(
                    "Manual charge is blocked until fault cleanup completes"
                )
            if int(self.options.get(CONF_MAX_SOLAR_AMPS, DEFAULT_MAX_SOLAR_AMPS)) < 8:
                raise HomeAssistantError(
                    "Manual charge requires a valid configured current limit"
                )
            self._runtime["enabled"] = True
            self._runtime["manual_override"] = True
            await self.store.async_save(self._runtime)
        await self.async_request_refresh()

    async def async_resume(self) -> None:
        async with self._lock:
            self._runtime["enabled"] = True
            self._runtime["manual_override"] = False
            await self._async_safe_release_locked("manual_resume")
            await self.store.async_save(self._runtime)
        await self.async_request_refresh()

    async def async_safe_release(self, reason: str = "manual_safe_release") -> None:
        async with self._lock:
            self._runtime["enabled"] = False
            self._runtime["manual_override"] = False
            await self.store.async_save(self._runtime)
            await self._async_safe_release_locked(reason)
        await self.async_request_refresh()

    async def async_clear_fault(self) -> None:
        async with self._lock:
            self._runtime["enabled"] = False
            self._runtime["manual_override"] = False
            await self._async_safe_release_locked("clear_fault")
            if self._runtime.get("journal") or self._runtime.get(
                "rivian_write_blocked"
            ):
                raise HomeAssistantError(
                    "Fault cannot be cleared while cleanup or schedule conflict remains"
                )
            self._fault = None
            self._runtime.pop("fault", None)
            await self.store.async_save(self._runtime)
        await self.async_request_refresh()

    async def async_commission_stop(self, vehicle: str | None = None) -> None:
        """Require a live, at-home charging session and prove a physical stop twice."""
        async with self._lock:
            if self.dry_run:
                raise HomeAssistantError("Commissioning is unavailable in dry-run mode")
            if self.enabled:
                raise HomeAssistantError(
                    "Pause automation before commissioning a physical stop"
                )
            if (
                self._fault
                or self._runtime.get("journal")
                or self._runtime.get("probe_journal")
            ):
                raise HomeAssistantError(
                    "Resolve the existing fault or cleanup before commissioning"
                )
            vehicle = vehicle or self._connected_vehicle()
            reason = self._command_readiness_reason(vehicle, commissioning=True)
            if reason:
                raise HomeAssistantError(f"Commissioning is blocked: {reason}")
            if (
                self._power_state(self.config.get(CONF_WALL_POWER), WALL_AGE) or 0
            ) <= IDLE_POWER_W:
                raise HomeAssistantError(
                    "Commissioning requires observed wall draw above idle"
                )
            assert vehicle
            journal = await self._async_prepare_journal(vehicle, 0, "commission_stop")
            try:
                await self._async_stop_with_feedback(journal, "commission_stop")
            except Exception as err:
                await self._async_latch_fault(
                    f"commission_stop_failed: {err}", cleanup=False
                )
                raise
            self._runtime.setdefault("commission", {})[vehicle] = {
                "binding_fingerprint": journal["binding_fingerprint"],
                "switch_entity": journal["switch_entity"],
                "wall_power_entity": journal["wall_power_entity"],
                "physical_stop_proved": True,
                "stopped_at": datetime.now(UTC).isoformat(),
            }
            self._runtime["journal"] = None
            await self.store.async_save(self._runtime)
        await self.async_request_refresh()

    async def async_probe_rivian(self) -> None:
        """Do a schedule-only probe; it cannot commission physical vehicle control."""
        async with self._lock:
            if self.dry_run:
                raise HomeAssistantError(
                    "Rivian schedule probing is unavailable in dry-run mode"
                )
            if not self.rivian_enabled:
                raise HomeAssistantError("Rivian control is not enabled")
            for entity_id in (
                self.config.get(CONF_RIVIAN_CONNECTED),
                self.config.get(CONF_WALL_CONNECTED),
            ):
                if self._binary_state(entity_id, VEHICLE_AGE) is not False:
                    raise HomeAssistantError(
                        "Rivian and wall must be freshly confirmed off before probing"
                    )
            if reason := self._vehicle_identity_reason(VEHICLE_RIVIAN, live=False):
                raise HomeAssistantError(reason)
            vehicle_id, snapshot = (
                self._require_rivian_vehicle_id(),
                await self._async_rivian_get(self._require_rivian_vehicle_id()),
            )
            schedules, test = (
                deepcopy(snapshot["schedules"]),
                deepcopy(snapshot["schedules"]),
            )
            if test:
                test[0]["amperage"] = 10 if int(test[0]["amperage"]) == 8 else 8
            else:
                test = [self._rivian_lease_schedule(8, enabled=False)]
            self._runtime["probe_journal"] = {
                "vehicle_id": vehicle_id,
                "base_schedules": schedules,
                "base_hash": snapshot["hash"],
                "test_hash": canonical_schedule_hash(test),
                "probe_hash": None,
            }
            await self.store.async_save(self._runtime)
            try:
                changed = await self._async_rivian_set(
                    vehicle_id, test, snapshot["hash"], "compatibility_probe"
                )
                self._runtime["probe_journal"]["probe_hash"] = changed["hash"]
                await self.store.async_save(self._runtime)
                await self._async_restore_probe_journal("compatibility_probe")
            except Exception:
                self._runtime["rivian_compatible"] = False
                try:
                    await self._async_restore_probe_journal("probe_failure")
                except Exception as restore_error:  # noqa: BLE001 - preserve a recoverable probe journal
                    await self._async_latch_fault(
                        f"probe_restore_failed: {restore_error}", cleanup=False
                    )
                await self.store.async_save(self._runtime)
                raise
            (
                self._runtime["rivian_compatible"],
                self._runtime["rivian_write_blocked"],
            ) = True, False
            await self.store.async_save(self._runtime)
        await self.async_request_refresh()

    async def _async_restore_probe_journal(self, reason: str) -> None:
        journal = self._runtime.get("probe_journal")
        if not journal:
            return
        current = await self._async_rivian_get(journal["vehicle_id"])
        if current["hash"] == journal["base_hash"]:
            self._runtime.pop("probe_journal", None)
        elif current["hash"] not in {journal["test_hash"], journal.get("probe_hash")}:
            self._runtime["rivian_write_blocked"] = True
            self._runtime["rivian_compatible"] = False
            await self.store.async_save(self._runtime)
            raise HomeAssistantError(
                "Rivian schedule conflict during compatibility probe recovery"
            )
        else:
            restored = await self._async_rivian_set(
                journal["vehicle_id"],
                journal["base_schedules"],
                current["hash"],
                f"compatibility_probe_restore_{reason}",
            )
            if restored["hash"] != journal["base_hash"]:
                raise HomeAssistantError("Rivian probe restoration did not match")
            self._runtime.pop("probe_journal", None)
        await self.store.async_save(self._runtime)

    async def _async_update_data(self) -> dict[str, Any]:
        async with self._lock:
            try:
                return await self._async_evaluate()
            except Exception as err:
                await self._async_latch_fault(str(err), cleanup=True)
                _LOGGER.exception("Solar EV controller evaluation failed")
                return self._status(STATE_FAULT, self._last_vehicle, 0, 0, self._fault)

    async def _async_evaluate(self) -> dict[str, Any]:
        if not getattr(self, "_initialized", True):
            return self._status(STATE_NORMAL, None, 0, 0, "initializing")
        vehicle, self._last_vehicle = (
            self._connected_vehicle(),
            self._connected_vehicle(),
        )
        if self._fault:
            journal = self._runtime.get("journal") or {}
            if journal.get("stop_required") and time.monotonic() >= float(
                journal.get("retry_after", 0)
            ):
                await self._async_safe_release_locked("fault_retry")
            return self._status(STATE_FAULT, vehicle, 0, 0, self._fault)
        if not self.enabled:
            return self._status(STATE_NORMAL, vehicle, 0, 0, "disabled")
        if reason := self._connection_reason():
            await self._async_safe_release_locked(reason)
            return self._status(STATE_NORMAL, vehicle, 0, 0, reason)
        if self.manual_override:
            if reason := self._command_readiness_reason(vehicle, commissioning=False):
                await self._async_safe_release_locked(reason)
                return self._status(STATE_FAULT, vehicle, 0, 0, reason)
            manual_inputs = self._control_inputs(vehicle)
            invalid = self._engine._invalid_reason(
                manual_inputs, amp_step=2 if vehicle == VEHICLE_RIVIAN else 1
            )
            if (
                invalid
                or manual_inputs.blocker
                or manual_inputs.battery_soc
                < float(self.options.get(CONF_RESERVE_SOC, DEFAULT_RESERVE_SOC))
            ):
                reason = invalid or manual_inputs.blocker or "battery_reserve"
                await self._async_latch_fault(reason, cleanup=True)
                return self._status(STATE_FAULT, vehicle, 0, 0, reason)
            if manual_inputs.grid_power_w > float(
                self.options.get(CONF_HARD_GRID_IMPORT_W, 1500)
            ) or -manual_inputs.battery_power_w > float(
                self.options.get(CONF_HARD_BATTERY_DISCHARGE_W, 1000)
            ):
                await self._async_latch_fault("manual_hard_safety_limit", cleanup=True)
                return self._status(
                    STATE_FAULT, vehicle, 0, 0, "manual_hard_safety_limit"
                )
            amps = min(
                48, int(self.options.get(CONF_MAX_SOLAR_AMPS, DEFAULT_MAX_SOLAR_AMPS))
            )
            if self.dry_run:
                return self._status(
                    STATE_MANUAL, vehicle, amps, 0, "manual_override:dry_run"
                )
            assert vehicle
            await self._async_apply_vehicle(vehicle, amps, force=True)
            return self._status(STATE_MANUAL, vehicle, amps, 0, None)
        inputs = self._control_inputs(vehicle)
        decision = self._engine.evaluate(
            inputs, amp_step=2 if vehicle == VEHICLE_RIVIAN else 1
        )
        if self.dry_run:
            return self._status_from_decision(decision, vehicle, "dry_run")
        if decision.state == STATE_SOLAR and decision.should_charge:
            if reason := self._command_readiness_reason(vehicle, commissioning=False):
                await self._async_safe_release_locked(reason)
                return self._status(
                    STATE_FAULT, vehicle, 0, decision.available_watts, reason
                )
            assert vehicle
            await self._async_apply_vehicle(vehicle, decision.target_amps)
        else:
            await self._async_safe_release_locked(decision.reason or "solar_exit")
            # A configured normal schedule is reported as intent only.  The
            # integration cannot safely prove its cloud-start feedback here,
            # so it never becomes an automatic control fallback.
        return self._status_from_decision(decision, vehicle)

    def _control_inputs(self, vehicle: str | None) -> ControlInputs:
        grid, battery = (
            self._power_state(self.config.get(CONF_GRID_POWER), POWER_AGE),
            self._power_state(self.config.get(CONF_BATTERY_POWER), POWER_AGE),
        )
        soc, wall = (
            self._percent_state(self.config.get(CONF_BATTERY_SOC), VEHICLE_AGE),
            self._power_state(self.config.get(CONF_WALL_POWER), WALL_AGE),
        )
        voltage = self._voltage_state(self.config.get(CONF_WALL_VOLTAGE), POWER_AGE)
        self._observed_power_w = wall
        self._observed_amps = wall / voltage if wall is not None and voltage else None
        self._forecast = self._forecast_data(soc, voltage)
        blocker = self._active_blocker() or (
            "vehicle_charge_limit"
            if vehicle and self._vehicle_at_limit(vehicle)
            else None
        )
        return ControlInputs(
            time.monotonic(),
            grid or 0,
            battery or 0,
            soc or 0,
            wall or 0,
            voltage or 0,
            vehicle,
            all(v is not None for v in (grid, battery, soc, wall, voltage)),
            blocker,
            bool(self._forecast["allow_start"]),
            self._forecast["reason"],
        )

    def _forecast_data(
        self, soc: float | None, voltage: float | None
    ) -> dict[str, Any]:
        value_field = self.options.get(CONF_FORECAST_VALUE_FIELD, "state")
        remaining, next_hour = (
            self._energy_state(self.options.get(CONF_FORECAST_REMAINING), value_field),
            self._energy_state(self.options.get(CONF_FORECAST_NEXT_HOUR), value_field),
        )
        source_fresh, source_status, source_age_seconds = self._forecast_source_state()
        derived_fresh = all(
            self._state_fresh(self.options.get(key), 7200)
            for key in (CONF_FORECAST_REMAINING, CONF_FORECAST_NEXT_HOUR)
        )
        physical_valid = (
            all(
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value)
                for value in (soc, voltage)
            )
            and 0 <= soc <= 100
            and 100 <= voltage <= 260
        )
        parsed_valid = (
            remaining is not None
            and next_hour is not None
            and isinstance(value_field, str)
            and value_field in FORECAST_VALUE_FIELDS
            and source_status in {"fresh", "stale"}
            and physical_valid
        )
        decision = choose_forecast(
            ForecastConfig(
                mode=str(self.options.get(CONF_FORECAST_MODE, "off")),
                confidence=self.options.get(CONF_FORECAST_CONFIDENCE, 0.75),
                battery_capacity_kwh=self.options.get("battery_capacity_kwh", 0),
                battery_target_soc=self.options.get("battery_target_soc", 80),
                household_budget_kwh=self.options.get("household_budget_kwh", 5),
                minimum_session_minutes=self.options.get("minimum_session_minutes", 15),
            ),
            ForecastInputs(
                remaining if remaining is not None else 0,
                next_hour if next_hour is not None else 0,
                soc,
                voltage,
                source_fresh and derived_fresh,
                parsed_valid,
            ),
        )
        return {
            "status": str(self.options.get(CONF_FORECAST_MODE, "off")),
            "reason": decision.reason,
            "remaining_kwh": remaining,
            "next_hour_kwh": next_hour,
            "ev_budget_kwh": decision.ev_budget_kwh if parsed_valid else None,
            "battery_needed_kwh": decision.battery_needed_kwh if parsed_valid else None,
            "allow_start": decision.allow_start,
            "source_status": source_status,
            "derived_entities_fresh": derived_fresh,
            "source_age_seconds": source_age_seconds,
            "source_entity": self.options.get(CONF_FORECAST_SOURCE_UPDATED) or None,
            "source_max_age_hours": self._forecast_source_max_age_hours(),
            "value_field": value_field,
        }

    async def _async_apply_vehicle(
        self, vehicle: str, amps: int, *, force: bool = False
    ) -> None:
        journal = await self._async_prepare_journal(vehicle, amps, "start_prepared")
        last = int(journal.get("last_amps", 0))
        if journal.get("phase") == "charging_confirmed":
            if not self._active_feedback_valid(journal, last):
                raise HomeAssistantError(
                    "Active charging feedback is missing or exceeds its confirmed limit"
                )
            if last == amps:
                if vehicle == VEHICLE_RIVIAN:
                    await self._async_apply_rivian_lease(amps, journal)
                return
            # Persist desired separately from last confirmed before an in-place
            # current change. Never cycle a live charging switch to adjust amps.
            journal["target_amps"] = amps
            journal["phase"] = "current_change_pending"
            await self.store.async_save(self._runtime)
            command_at = datetime.now(UTC)
            if vehicle == VEHICLE_RIVIAN:
                await self._async_apply_rivian_lease(amps, journal)
            else:
                await self._async_set_tesla_amps(amps, journal["amps_entity"])
            await self._async_confirm_start(journal, command_at)
            journal.update(phase="charging_confirmed", last_amps=amps)
            await self.store.async_save(self._runtime)
            self._last_command = f"{vehicle}_charge_{amps}a"
            return
        if (
            not force
            and amps > last
            and time.monotonic() - float(journal.get("last_command_monotonic", 0))
            < max(60, int(self.options.get(CONF_COMMAND_INTERVAL_SECONDS, 60)))
        ):
            return
        if (
            self._switch_is_on(journal["switch_entity"])
            or (self._power_state(journal["wall_power_entity"], WALL_AGE) or 0)
            > IDLE_POWER_W
        ):
            await self._async_stop_with_feedback(journal, "pre_start_stop")
        if vehicle == VEHICLE_RIVIAN:
            await self._async_apply_rivian_lease(amps, journal)
        else:
            await self._async_set_tesla_amps(amps, journal["amps_entity"])
        journal.update(
            phase="start_commanded",
            target_amps=amps,
            stop_required=True,
            last_command_monotonic=time.monotonic(),
        )
        await self.store.async_save(self._runtime)
        command_at = datetime.now(UTC)
        await self._async_command_switch(journal["switch_entity"], True)
        await self._async_confirm_start(journal, command_at)
        journal.update(phase="charging_confirmed", last_amps=amps)
        await self.store.async_save(self._runtime)
        self._last_command = f"{vehicle}_charge_{amps}a"

    async def _async_prepare_journal(
        self, vehicle: str, target: int, phase: str
    ) -> dict[str, Any]:
        existing = self._runtime.get("journal")
        if existing and existing.get("vehicle") == vehicle:
            return existing
        if existing:
            await self._async_safe_release_locked("vehicle_changed")
            if self._runtime.get("journal"):
                raise HomeAssistantError(
                    "Previous actuator ownership could not be released"
                )
        binding = self._binding(vehicle)
        journal: dict[str, Any] = {
            "vehicle": vehicle,
            **binding,
            "phase": phase,
            "target_amps": target,
            "stop_required": True,
            "created_at": datetime.now(UTC).isoformat(),
        }
        if vehicle == VEHICLE_RIVIAN:
            snapshot = await self._async_rivian_get(self._require_rivian_vehicle_id())
            journal.update(
                vehicle_id=self._require_rivian_vehicle_id(),
                base_schedules=snapshot["schedules"],
                base_hash=snapshot["hash"],
                last_hash=snapshot["hash"],
            )
        else:
            journal["base_amps"] = self._numeric_state(
                binding["amps_entity"], VEHICLE_AGE, "A"
            )
        self._runtime["journal"] = journal
        await self.store.async_save(self._runtime)
        return journal

    async def _async_apply_rivian_lease(
        self, amps: int, journal: dict[str, Any]
    ) -> None:
        now = time.monotonic()
        if not (
            amps < int(journal.get("last_amps", 49))
            or now - self._last_rivian_renewal >= RIVIAN_RENEW_SECONDS
            or journal.get("last_amps") != amps
        ):
            return
        schedules = [self._rivian_lease_schedule(amps)]
        prior_phase = journal.get("phase")
        journal.update(
            phase="schedule_pending", pending_hash=canonical_schedule_hash(schedules)
        )
        await self.store.async_save(self._runtime)
        response = await self._async_rivian_set(
            journal["vehicle_id"], schedules, journal["last_hash"], "solar_lease"
        )
        if (await self._async_rivian_get(journal["vehicle_id"]))["hash"] != response[
            "hash"
        ]:
            raise HomeAssistantError(
                "Rivian schedule readback did not acknowledge the write"
            )
        journal.update(last_hash=response["hash"], pending_hash=None, phase=prior_phase)
        self._last_rivian_renewal = now
        await self.store.async_save(self._runtime)

    async def _async_safe_release_locked(self, reason: str) -> bool:
        journal = self._runtime.get("journal")
        if not journal:
            return True
        try:
            if not journal.get("switch_entity") or not journal.get("wall_power_entity"):
                raise HomeAssistantError("cleanup_binding_missing")
            await self._async_stop_with_feedback(journal, f"stop_{reason}")
            if journal.get("vehicle") == VEHICLE_RIVIAN and journal.get("base_hash"):
                current = await self._async_rivian_get(journal["vehicle_id"])
                if current["hash"] != journal["base_hash"]:
                    if current["hash"] not in {
                        journal.get("last_hash"),
                        journal.get("pending_hash"),
                    }:
                        self._runtime["rivian_write_blocked"] = True
                        raise HomeAssistantError("rivian_schedule_changed_externally")
                    restored = await self._async_rivian_set(
                        journal["vehicle_id"],
                        journal["base_schedules"],
                        current["hash"],
                        f"restore_{reason}",
                    )
                    if restored["hash"] != journal["base_hash"]:
                        raise HomeAssistantError(
                            "Rivian schedule restoration did not match"
                        )
                await self._async_stop_with_feedback(
                    journal, f"post_restore_stop_{reason}"
                )
            self._runtime["journal"] = None
            self._last_rivian_renewal = 0.0
            self._engine_stop()
            await self.store.async_save(self._runtime)
            return True
        except Exception as err:  # noqa: BLE001 - a failed actuator must remain journaled
            journal.update(
                phase="cleanup_pending",
                stop_required=True,
                last_cleanup_error=str(err),
                cleanup_attempts=int(journal.get("cleanup_attempts", 0)) + 1,
            )
            journal["retry_after"] = (
                time.monotonic()
                + STOP_BACKOFF[
                    min(journal["cleanup_attempts"] - 1, len(STOP_BACKOFF) - 1)
                ]
            )
            await self._async_latch_fault(f"cleanup_failed: {err}", cleanup=False)
            await self.store.async_save(self._runtime)
            return False

    async def _async_stop_with_feedback(
        self, journal: dict[str, Any], phase: str
    ) -> None:
        journal.update(phase=phase, stop_required=True)
        self._command_acknowledged = False
        await self.store.async_save(self._runtime)
        after = datetime.now(UTC)
        await self._async_command_switch(journal["switch_entity"], False)
        await self._async_confirm_stop(journal, after)
        self._command_acknowledged, self._last_feedback_at = True, datetime.now(UTC)

    async def _async_confirm_stop(
        self, journal: dict[str, Any], after: datetime
    ) -> None:
        deadline, observations = time.monotonic() + self._feedback_timeout(), set()
        while time.monotonic() < deadline:
            state, power = (
                self.hass.states.get(journal["wall_power_entity"]),
                self._power_state(journal["wall_power_entity"], WALL_AGE),
            )
            stamp = self._state_stamp(state) if state else None
            if (
                self._binary_state(journal["switch_entity"], VEHICLE_AGE) is False
                and power is not None
                and power < IDLE_POWER_W
                and stamp
                and stamp >= after
            ):
                observations.add(stamp)
                self._observed_power_w = power
                if len(observations) >= 2:
                    return
            await asyncio.sleep(1)
        raise HomeAssistantError("Physical stop feedback was not confirmed")

    async def _async_confirm_start(
        self, journal: dict[str, Any], command_at: datetime
    ) -> None:
        deadline = time.monotonic() + self._feedback_timeout()
        observations: set[datetime] = set()
        while time.monotonic() < deadline:
            if reason := self._feedback_safety_reason(journal):
                raise HomeAssistantError(
                    f"Charging feedback safety gate failed: {reason}"
                )
            state = self.hass.states.get(journal["wall_power_entity"])
            power = self._power_state(journal["wall_power_entity"], WALL_AGE)
            voltage = self._voltage_state(journal["wall_voltage_entity"], POWER_AGE)
            stamp = self._state_stamp(state) if state else None
            if (
                self._binary_state(journal["switch_entity"], VEHICLE_AGE) is True
                and power is not None
                and voltage is not None
                and power > IDLE_POWER_W
                and stamp
                and stamp >= command_at
            ):
                observed_amps = power / voltage
                if observed_amps > float(journal["target_amps"]) + 1:
                    raise HomeAssistantError(
                        "Observed charging current exceeds commanded limit"
                    )
                observations.add(stamp)
                self._observed_power_w, self._observed_amps = power, observed_amps
                if len(observations) >= 2:
                    self._last_feedback_at, self._command_acknowledged = (
                        datetime.now(UTC),
                        True,
                    )
                    return
            await asyncio.sleep(1)
        raise HomeAssistantError(
            "Physical start/current-limit feedback was not confirmed"
        )

    def _feedback_safety_reason(self, journal: dict[str, Any]) -> str | None:
        """Recheck all safety data during an acknowledgement wait, not just at command time."""
        vehicle = journal.get("vehicle")
        if vehicle not in (VEHICLE_TESLA, VEHICLE_RIVIAN):
            return "journal_vehicle_missing"
        if self._connection_reason() or self._connected_vehicle() != vehicle:
            return "vehicle_connection_changed_or_ambiguous"
        if self._vehicle_identity_reason(vehicle):
            return "vehicle_identity_changed"
        if self._binding(vehicle)["binding_fingerprint"] != journal.get(
            "binding_fingerprint"
        ):
            return "actuator_binding_changed"
        try:
            inputs = self._control_inputs(vehicle)
        except (KeyError, TypeError, ValueError):
            return "input_mapping_invalid"
        invalid = self._engine._invalid_reason(
            inputs, amp_step=2 if vehicle == VEHICLE_RIVIAN else 1
        )
        if invalid or inputs.blocker:
            return invalid or inputs.blocker
        if inputs.battery_soc < float(
            self.options.get(CONF_RESERVE_SOC, DEFAULT_RESERVE_SOC)
        ):
            return "battery_reserve"
        if inputs.grid_power_w > float(self.options.get(CONF_HARD_GRID_IMPORT_W, 1500)):
            return "grid_importing"
        if -inputs.battery_power_w > float(
            self.options.get(CONF_HARD_BATTERY_DISCHARGE_W, 1000)
        ):
            return "battery_discharging"
        if self._home_state(vehicle) is not True or self._vehicle_at_limit(vehicle):
            return "vehicle_not_ready"
        if not self.manual_override:
            now = time.monotonic()
            previous = float(journal.get("feedback_last_monotonic", now))
            elapsed = max(0.0, min(now - previous, 60.0))
            journal["feedback_last_monotonic"] = now
            deficit = max(0.0, inputs.grid_power_w) + max(0.0, -inputs.battery_power_w)
            journal["feedback_deficit_wh"] = (
                float(journal.get("feedback_deficit_wh", 0.0))
                + deficit * elapsed / 3600.0
            )
            soft_adverse = (
                inputs.grid_power_w
                > float(
                    self.options.get(
                        CONF_GRID_IMPORT_LIMIT_W, DEFAULT_GRID_IMPORT_LIMIT_W
                    )
                )
                or -inputs.battery_power_w
                > float(
                    self.options.get(
                        CONF_BATTERY_DISCHARGE_LIMIT_W,
                        DEFAULT_BATTERY_DISCHARGE_LIMIT_W,
                    )
                )
                or inputs.ev_power_w
                - inputs.grid_power_w
                - max(0.0, -inputs.battery_power_w)
                - float(self.options.get(CONF_SAFETY_MARGIN_W, DEFAULT_SAFETY_MARGIN_W))
                < 8 * inputs.wall_voltage_v
            )
            if soft_adverse:
                journal.setdefault("feedback_adverse_since", now)
            else:
                journal.pop("feedback_adverse_since", None)
            if journal["feedback_deficit_wh"] > float(
                self.options.get(CONF_MAX_TRANSIENT_DEFICIT_WH, 10)
            ):
                return "feedback_transient_deficit_exceeded"
            if soft_adverse and now - float(journal["feedback_adverse_since"]) >= float(
                self.options.get(CONF_STOP_HOLD_SECONDS, DEFAULT_STOP_HOLD_SECONDS)
            ):
                return "feedback_surplus_lost"
        return None

    def _active_feedback_valid(self, journal: dict[str, Any], amps: int) -> bool:
        """A no-op is safe only while the last confirmed physical state persists."""
        power = self._power_state(journal["wall_power_entity"], WALL_AGE)
        voltage = self._voltage_state(journal["wall_voltage_entity"], POWER_AGE)
        return bool(
            self._binary_state(journal["switch_entity"], VEHICLE_AGE) is True
            and power is not None
            and voltage is not None
            and IDLE_POWER_W < power <= (float(amps) + 1) * voltage
        )

    def _engine_stop(self) -> None:
        engine = getattr(self, "_engine", None)
        if engine is None:
            return
        stop = getattr(engine, "stop", None)
        if stop:
            stop(time.monotonic())
        else:
            engine.reset()

    async def _async_command_switch(self, entity_id: str, turn_on: bool) -> None:
        await self._async_service_call(
            "switch",
            "turn_on" if turn_on else "turn_off",
            {},
            target={"entity_id": entity_id},
        )

    async def _async_set_tesla_amps(self, amps: int, entity_id: str) -> None:
        after, deadline = datetime.now(UTC), time.monotonic() + self._feedback_timeout()
        await self._async_service_call(
            "number", "set_value", {"value": amps}, target={"entity_id": entity_id}
        )
        while time.monotonic() < deadline:
            if (journal := self._runtime.get("journal")) and (
                reason := self._feedback_safety_reason(journal)
            ):
                raise HomeAssistantError(
                    f"Current readback safety gate failed: {reason}"
                )
            state, value = (
                self.hass.states.get(entity_id),
                self._numeric_state(entity_id, VEHICLE_AGE, "A"),
            )
            if (
                value is not None
                and round(value) == amps
                and state
                and self._state_stamp(state) >= after
            ):
                self._observed_amps = value
                return
            await asyncio.sleep(1)
        raise HomeAssistantError("Tesla current readback did not acknowledge the write")

    async def _async_latch_fault(self, reason: str, *, cleanup: bool) -> None:
        self._fault, self._runtime["fault"] = reason, reason
        await self.store.async_save(self._runtime)
        if cleanup and self._runtime.get("journal"):
            await self._async_safe_release_locked("fault")

    async def _async_rivian_get(self, vehicle_id: str) -> dict[str, Any]:
        if not self.hass.services.has_service("rivian", "get_charging_schedules"):
            raise HomeAssistantError("Rivian schedule service is unavailable")
        response = await self._async_service_call(
            "rivian",
            "get_charging_schedules",
            {"vehicle_id": vehicle_id},
            response=True,
        )
        if not isinstance(response, dict):
            raise HomeAssistantError("Rivian schedule service returned no response")
        return response

    async def _async_rivian_set(
        self,
        vehicle_id: str,
        schedules: list[dict[str, Any]],
        expected_hash: str,
        reason: str,
    ) -> dict[str, Any]:
        if not self.hass.services.has_service("rivian", "set_charging_schedules"):
            raise HomeAssistantError("Rivian schedule service is unavailable")
        response = await self._async_service_call(
            "rivian",
            "set_charging_schedules",
            {
                "vehicle_id": vehicle_id,
                "schedules": schedules,
                "expected_hash": expected_hash,
                "reason": reason,
            },
            response=True,
        )
        if not isinstance(response, dict):
            raise HomeAssistantError("Rivian schedule write returned no response")
        return response

    async def _async_service_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        *,
        target: dict[str, Any] | None = None,
        response: bool = False,
    ) -> Any:
        return await asyncio.wait_for(
            self.hass.services.async_call(
                domain,
                service,
                data,
                target=target,
                blocking=True,
                return_response=response,
            ),
            timeout=SERVICE_TIMEOUT,
        )

    def _rivian_lease_schedule(
        self, amps: int, *, enabled: bool = True
    ) -> dict[str, Any]:
        zone = dt_util.get_time_zone(self.hass.config.time_zone) or UTC
        start = dt_util.now(zone) - timedelta(minutes=1)
        return {
            "weekDays": [WEEK_DAYS_ORDERED[start.weekday()]],
            "startTime": start.hour * 60 + start.minute,
            "duration": RIVIAN_LEASE_MINUTES,
            "location": {
                "latitude": self.hass.config.latitude,
                "longitude": self.hass.config.longitude,
            },
            "amperage": amps,
            "enabled": enabled,
        }

    def _command_readiness_reason(
        self, vehicle: str | None, *, commissioning: bool
    ) -> str | None:
        if not getattr(self, "_initialized", True):
            return "initializing"
        if self._connection_reason() or vehicle is None:
            return self._connection_reason() or "vehicle_identity_ambiguous"
        if self._vehicle_at_limit(vehicle):
            return "vehicle_charge_limit"
        if any(
            not self._state_fresh(x, VEHICLE_AGE)
            for x in self._vehicle_entities(vehicle)
        ):
            return "stale_vehicle_input"
        if reason := self._vehicle_identity_reason(vehicle):
            return reason
        if self._home_state(vehicle) is not True:
            return "vehicle_not_confirmed_home"
        return self._capability_reason(vehicle, commissioning=commissioning)

    def _connection_reason(self) -> str | None:
        wall = self._binary_state(self.config.get(CONF_WALL_CONNECTED), VEHICLE_AGE)
        states = (
            [self._binary_state(self.config.get(CONF_TESLA_CONNECTED), VEHICLE_AGE)]
            if self.tesla_enabled
            else []
        )
        if self.rivian_enabled:
            states.append(
                self._binary_state(self.config.get(CONF_RIVIAN_CONNECTED), VEHICLE_AGE)
            )
        if wall is None:
            return "wall_connection_unknown_or_stale"
        if any(state is None for state in states):
            return "vehicle_connection_unknown_or_stale"
        if wall is False and any(state is True for state in states):
            return "wall_vehicle_connection_mismatch"
        if wall is True and sum(state is True for state in states) != 1:
            return "vehicle_identity_ambiguous"
        return None

    def _connected_vehicle(self) -> str | None:
        if (
            self._connection_reason()
            or self._binary_state(self.config.get(CONF_WALL_CONNECTED), VEHICLE_AGE)
            is not True
        ):
            return None
        if (
            self.tesla_enabled
            and self._binary_state(self.config.get(CONF_TESLA_CONNECTED), VEHICLE_AGE)
            is True
        ):
            return VEHICLE_TESLA
        if (
            self.rivian_enabled
            and self._binary_state(self.config.get(CONF_RIVIAN_CONNECTED), VEHICLE_AGE)
            is True
        ):
            return VEHICLE_RIVIAN
        return None

    def _binding(self, vehicle: str) -> dict[str, Any]:
        switch, home = (
            self.config.get(
                CONF_RIVIAN_SWITCH if vehicle == VEHICLE_RIVIAN else CONF_TESLA_SWITCH
            ),
            self.config.get(
                CONF_RIVIAN_HOME if vehicle == VEHICLE_RIVIAN else CONF_TESLA_HOME
            ),
        )
        vehicle_fields = self._vehicle_entities(vehicle)
        vehicle_id = (
            getattr(self, "_rivian_vehicle_id", None)
            if vehicle == VEHICLE_RIVIAN
            else "tesla"
        )
        wall_voltage = self.config.get(CONF_WALL_VOLTAGE)
        parts = (
            vehicle,
            vehicle_id,
            switch,
            self.config.get(CONF_WALL_POWER),
            wall_voltage,
            self.config.get(CONF_WALL_CONNECTED),
            home,
            *vehicle_fields,
        )
        return {
            "switch_entity": switch,
            "wall_power_entity": self.config.get(CONF_WALL_POWER),
            "wall_voltage_entity": wall_voltage,
            "wall_connected_entity": self.config.get(CONF_WALL_CONNECTED),
            "home_entity": home,
            "amps_entity": self.config.get(CONF_TESLA_AMPS)
            if vehicle == VEHICLE_TESLA
            else None,
            "binding_fingerprint": "|".join(str(x or "") for x in parts),
        }

    def _capability_reason(self, vehicle: str, *, commissioning: bool) -> str | None:
        binding = self._binding(vehicle)
        if not binding["switch_entity"]:
            return "physical_stop_switch_not_mapped"
        if not str(binding["switch_entity"]).startswith("switch."):
            return "physical_stop_switch_invalid"
        if vehicle == VEHICLE_RIVIAN:
            if not self.options.get(CONF_RIVIAN_CONTROL_CONFIRMED, False):
                return "rivian_control_not_human_confirmed"
            if not commissioning and not self.rivian_compatible:
                return "rivian_schedule_probe_not_confirmed"
        elif not commissioning and not binding["amps_entity"]:
            return "tesla_current_control_not_mapped"
        proof = self._runtime.get("commission", {}).get(vehicle, {})
        if (
            not commissioning
            and proof.get("binding_fingerprint") != binding["binding_fingerprint"]
        ):
            return "physical_stop_not_commissioned"
        return None

    def _vehicle_entities(self, vehicle: str) -> tuple[str | None, ...]:
        return (
            (
                self.config.get(CONF_RIVIAN_CONNECTED),
                self.config.get(CONF_RIVIAN_SOC),
                self.config.get(CONF_RIVIAN_LIMIT),
                self.config.get(CONF_RIVIAN_HOME),
                self.config.get(CONF_RIVIAN_SWITCH),
            )
            if vehicle == VEHICLE_RIVIAN
            else (
                self.config.get(CONF_TESLA_CONNECTED),
                self.config.get(CONF_TESLA_SOC),
                self.config.get(CONF_TESLA_LIMIT),
                self.config.get(CONF_TESLA_HOME),
                self.config.get(CONF_TESLA_SWITCH),
                self.config.get(CONF_TESLA_AMPS),
            )
        )

    def _vehicle_identity_reason(
        self, vehicle: str, *, live: bool = True
    ) -> str | None:
        registry, device_ids = er.async_get(self.hass), set()
        entities = self._vehicle_entities(vehicle)
        if not live:
            entities = entities[:3]
        for entity_id in entities:
            if not entity_id:
                return "vehicle_identity_mapping_incomplete"
            entry = registry.async_get(entity_id)
            if not entry or not entry.device_id:
                return "vehicle_identity_mapping_missing"
            device_ids.add(entry.device_id)
        return None if len(device_ids) == 1 else "vehicle_identity_mapping_mismatch"

    def _resolve_rivian_vehicle_id(self) -> str | None:
        entry = (
            er.async_get(self.hass).async_get(self.config.get(CONF_RIVIAN_CONNECTED))
            if self.config.get(CONF_RIVIAN_CONNECTED)
            else None
        )
        device = (
            dr.async_get(self.hass).async_get(entry.device_id)
            if entry and entry.device_id
            else None
        )
        return (
            next(
                (
                    identifier
                    for domain, identifier in device.identifiers
                    if domain == "rivian"
                ),
                None,
            )
            if device
            else None
        )

    def _require_rivian_vehicle_id(self) -> str:
        self._rivian_vehicle_id = (
            self._rivian_vehicle_id or self._resolve_rivian_vehicle_id()
        )
        if not self._rivian_vehicle_id:
            raise HomeAssistantError(
                "Could not resolve the mapped Rivian vehicle identifier"
            )
        return self._rivian_vehicle_id

    def _vehicle_at_limit(self, vehicle: str) -> bool:
        soc = self._percent_state(
            self.config.get(
                CONF_TESLA_SOC if vehicle == VEHICLE_TESLA else CONF_RIVIAN_SOC
            ),
            VEHICLE_AGE,
        )
        limit = self._percent_state(
            self.config.get(
                CONF_TESLA_LIMIT if vehicle == VEHICLE_TESLA else CONF_RIVIAN_LIMIT
            ),
            VEHICLE_AGE,
        )
        return soc is None or limit is None or soc >= limit

    def _home_state(self, vehicle: str) -> bool | None:
        entity = self.config.get(
            CONF_RIVIAN_HOME if vehicle == VEHICLE_RIVIAN else CONF_TESLA_HOME
        )
        state = self.hass.states.get(entity) if entity else None
        if (
            not state
            or not self._state_fresh(entity, VEHICLE_AGE)
            or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, "")
        ):
            return None
        return state.state in (STATE_ON, "home")

    def _active_blocker(self) -> str | None:
        for entity in self.config.get(CONF_BLOCKERS, []):
            state = self._binary_state(entity, VEHICLE_AGE)
            if state is None:
                return "blocker_unknown_or_stale"
            if state:
                return entity.replace("input_boolean.", "")
        return None

    @staticmethod
    def _state_stamp(state: Any) -> datetime:
        return getattr(state, "last_reported", None) or state.last_updated

    def _state_fresh(self, entity: str | None, age: int) -> bool:
        state = self.hass.states.get(entity) if entity else None
        elapsed = (
            (datetime.now(UTC) - self._state_stamp(state)).total_seconds()
            if state
            else -1
        )
        return bool(state and 0 <= elapsed <= age)

    def _binary_state(self, entity: str | None, age: int) -> bool | None:
        state = self.hass.states.get(entity) if entity else None
        if (
            not state
            or not self._state_fresh(entity, age)
            or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, "")
        ):
            return None
        return (
            True
            if state.state == STATE_ON
            else False
            if state.state == STATE_OFF
            else None
        )

    def _switch_is_on(self, entity: str) -> bool:
        return self._binary_state(entity, VEHICLE_AGE) is True

    def _numeric_state(
        self, entity: str | None, age: int, kind: str | None = None
    ) -> float | None:
        state = self.hass.states.get(entity) if entity else None
        if (
            not state
            or not self._state_fresh(entity, age)
            or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, "")
        ):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        if not math.isfinite(value):
            return None
        unit = str(
            getattr(state, "attributes", {}).get("unit_of_measurement", "")
        ).strip()
        return value if not kind or unit in ("", kind) else None

    def _power_state(self, entity: str | None, age: int) -> float | None:
        value = self._numeric_state(entity, age)
        state = self.hass.states.get(entity) if entity else None
        unit = (
            str(getattr(state, "attributes", {}).get("unit_of_measurement", "")).strip()
            if state
            else ""
        )
        return (
            value * 1000
            if value is not None and unit == "kW"
            else value
            if value is not None and unit in ("", "W")
            else None
        )

    def _voltage_state(self, entity: str | None, age: int) -> float | None:
        value = self._numeric_state(entity, age)
        state = self.hass.states.get(entity) if entity else None
        unit = (
            str(getattr(state, "attributes", {}).get("unit_of_measurement", "")).strip()
            if state
            else ""
        )
        value = (
            value * 1000
            if value is not None and unit == "kV"
            else value
            if value is not None and unit in ("", "V")
            else None
        )
        return value if value is not None and 100 <= value <= 260 else None

    def _percent_state(self, entity: str | None, age: int) -> float | None:
        value = self._numeric_state(entity, age)
        state = self.hass.states.get(entity) if entity else None
        unit = (
            str(getattr(state, "attributes", {}).get("unit_of_measurement", "")).strip()
            if state
            else ""
        )
        return (
            value
            if value is not None and unit in ("", "%") and 0 <= value <= 100
            else None
        )

    def _energy_state(self, entity: str | None, value_field: str) -> float | None:
        state = self.hass.states.get(entity) if entity else None
        if not state or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            return None
        return forecast_energy_kwh(
            forecast_attribute_value(state, value_field),
            state.attributes.get("unit_of_measurement"),
        )

    def _forecast_source_max_age_hours(self) -> float | None:
        value = self.options.get(
            CONF_FORECAST_SOURCE_MAX_AGE_HOURS,
            DEFAULT_FORECAST_SOURCE_MAX_AGE_HOURS,
        )
        if isinstance(value, bool):
            return None
        try:
            hours = float(value)
        except (TypeError, ValueError):
            return None
        return hours if math.isfinite(hours) and hours > 0 else None

    def _forecast_source_state(self) -> tuple[bool, str, float | None]:
        entity = self.options.get(CONF_FORECAST_SOURCE_UPDATED)
        if not entity:
            return False, "unconfigured", None
        state = self.hass.states.get(entity)
        if not state or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            return False, "unavailable", None
        age_seconds = forecast_source_age_seconds(state.state)
        maximum_age_hours = self._forecast_source_max_age_hours()
        if age_seconds is None or maximum_age_hours is None:
            return False, "invalid", None
        if age_seconds > maximum_age_hours * 3600:
            return False, "stale", age_seconds
        return True, "fresh", age_seconds

    def _feedback_timeout(self) -> int:
        return min(90, max(1, int(self.options.get(CONF_FEEDBACK_TIMEOUT_SECONDS, 90))))

    def _control_config(self) -> ControlConfig:
        return ControlConfig(
            reserve_soc=float(self.options.get(CONF_RESERVE_SOC, DEFAULT_RESERVE_SOC)),
            reserve_entry_margin=float(
                self.options.get(
                    CONF_RESERVE_ENTRY_MARGIN, DEFAULT_RESERVE_ENTRY_MARGIN
                )
            ),
            safety_margin_w=float(
                self.options.get(CONF_SAFETY_MARGIN_W, DEFAULT_SAFETY_MARGIN_W)
            ),
            start_hold_seconds=int(
                self.options.get(CONF_START_HOLD_SECONDS, DEFAULT_START_HOLD_SECONDS)
            ),
            stop_hold_seconds=int(
                self.options.get(CONF_STOP_HOLD_SECONDS, DEFAULT_STOP_HOLD_SECONDS)
            ),
            battery_discharge_limit_w=float(
                self.options.get(
                    CONF_BATTERY_DISCHARGE_LIMIT_W, DEFAULT_BATTERY_DISCHARGE_LIMIT_W
                )
            ),
            grid_import_limit_w=float(
                self.options.get(CONF_GRID_IMPORT_LIMIT_W, DEFAULT_GRID_IMPORT_LIMIT_W)
            ),
            max_solar_amps=int(
                self.options.get(CONF_MAX_SOLAR_AMPS, DEFAULT_MAX_SOLAR_AMPS)
            ),
            smoothing_seconds=float(self.options.get(CONF_SMOOTHING_SECONDS, 60)),
            min_off_seconds=float(self.options.get(CONF_MIN_OFF_SECONDS, 180)),
            max_transient_deficit_wh=float(
                self.options.get(CONF_MAX_TRANSIENT_DEFICIT_WH, 10)
            ),
            hard_grid_import_w=float(self.options.get(CONF_HARD_GRID_IMPORT_W, 1500)),
            hard_battery_discharge_w=float(
                self.options.get(CONF_HARD_BATTERY_DISCHARGE_W, 1000)
            ),
        )

    def _status_from_decision(
        self, decision: ControlDecision, vehicle: str | None, suffix: str | None = None
    ) -> dict[str, Any]:
        status = self._status(
            decision.state,
            vehicle,
            decision.target_amps,
            decision.available_watts,
            f"{decision.reason}:{suffix}"
            if decision.reason and suffix
            else suffix or decision.reason,
        )
        status.update(
            raw_watts=round(decision.available_watts),
            smoothed_watts=round(decision.smoothed_watts),
            restart_remaining_seconds=round(decision.restart_remaining_seconds),
            transient_deficit_wh=round(decision.transient_deficit_wh, 2),
        )
        return status

    def _status(
        self,
        state: str,
        vehicle: str | None,
        target: int,
        available: float,
        reason: str | None,
    ) -> dict[str, Any]:
        journal = self._runtime.get("journal") or {}
        feedback_at = getattr(self, "_last_feedback_at", None)
        cleanup_phases = ("cleanup_pending", "stop_")
        pending_cleanup = bool(journal) and (
            str(journal.get("phase", "")).startswith(cleanup_phases)
            or (
                bool(getattr(self, "_fault", None))
                and bool(journal.get("stop_required"))
            )
        )
        return {
            "state": state,
            "active_vehicle": vehicle,
            "target_amps": target,
            "available_watts": round(available),
            "reason": reason,
            "last_command": getattr(self, "_last_command", None),
            "enabled": self.enabled,
            "manual_override": self.manual_override,
            "dry_run": self.dry_run,
            "rivian_compatible": self.rivian_compatible,
            "vehicle_mode": self.options.get(CONF_VEHICLE_MODE, VEHICLE_MODE_BOTH),
            "fault": getattr(self, "_fault", None),
            "observed_power_w": getattr(self, "_observed_power_w", None),
            "observed_amps": getattr(self, "_observed_amps", None),
            "control_phase": journal.get("phase", "idle"),
            "capability_reason": self._capability_reason(vehicle, commissioning=False)
            if vehicle
            else "vehicle_identity_ambiguous",
            "pending_cleanup": pending_cleanup,
            "command_acknowledged": getattr(self, "_command_acknowledged", False),
            "last_feedback_at": feedback_at.isoformat() if feedback_at else None,
            "forecast": getattr(self, "_forecast", {}),
            "inputs": {
                "connection_reason": self._connection_reason(),
                "measurements": self._input_diagnostics(vehicle),
            },
        }

    def _input_diagnostics(self, vehicle: str | None) -> list[dict[str, Any]]:
        """Expose per-input readiness without changing control reads or gates."""
        now = datetime.now(UTC)

        def add(
            label: str, entity: str | None, max_age: int, unit: str, reader: Any
        ) -> dict[str, Any]:
            state = self.hass.states.get(entity) if entity else None
            age = state_age(state, now)
            if not entity:
                status, value = "not_configured", None
            elif state is None:
                status, value = "missing", None
            elif state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
                status, value = "unavailable", None
            elif age is not None and age < 0:
                status, value = "future", None
            else:
                value = reader(entity, max_age)
                status = (
                    "stale"
                    if age is not None and age > max_age
                    else ("invalid" if value is None else "ok")
                )
            return measurement(
                label=label,
                entity_id=entity,
                unit=unit,
                age_seconds=age,
                max_age_seconds=max_age,
                status=status,
                normalized_value=value,
            )

        items = [
            add(
                "Grid power",
                self.config.get(CONF_GRID_POWER),
                POWER_AGE,
                "W",
                self._power_state,
            ),
            add(
                "Battery power",
                self.config.get(CONF_BATTERY_POWER),
                POWER_AGE,
                "W",
                self._power_state,
            ),
            add(
                "Battery SOC",
                self.config.get(CONF_BATTERY_SOC),
                VEHICLE_AGE,
                "%",
                self._percent_state,
            ),
            add(
                "Wall connection",
                self.config.get(CONF_WALL_CONNECTED),
                VEHICLE_AGE,
                "",
                self._binary_state,
            ),
            add(
                "Wall power",
                self.config.get(CONF_WALL_POWER),
                WALL_AGE,
                "W",
                self._power_state,
            ),
            add(
                "Wall voltage",
                self.config.get(CONF_WALL_VOLTAGE),
                POWER_AGE,
                "V",
                self._voltage_state,
            ),
        ]
        for kind in (VEHICLE_TESLA, VEHICLE_RIVIAN):
            enabled = (
                self.tesla_enabled if kind == VEHICLE_TESLA else self.rivian_enabled
            )
            if not enabled or (vehicle and vehicle != kind):
                continue
            labels = ("connected", "SOC", "charge limit", "home", "charge switch")
            readers = (
                self._binary_state,
                self._percent_state,
                self._percent_state,
                lambda _entity, _age, vehicle_kind=kind: self._home_state(vehicle_kind),
                self._binary_state,
            )
            for label, entity, reader in zip(
                labels, self._vehicle_entities(kind), readers
            ):
                unit = "%" if label in ("SOC", "charge limit") else ""
                items.append(
                    add(f"{kind.title()} {label}", entity, VEHICLE_AGE, unit, reader)
                )
            if kind == VEHICLE_TESLA:
                items.append(
                    add(
                        "Tesla charging current",
                        self.config.get(CONF_TESLA_AMPS),
                        VEHICLE_AGE,
                        "A",
                        self._numeric_state,
                    )
                )
        return items

    async def async_shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        try:
            async with self._lock:
                await self._async_safe_release_locked("shutdown")
        except Exception:
            _LOGGER.exception("Could not release EV control during shutdown")
        await super().async_shutdown()
