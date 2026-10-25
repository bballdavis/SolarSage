"""The planner's inputs and outputs: plain, typed, immutable values.

Units everywhere: power kW (5-min or 15-min means), energy kWh, battery and car levels %,
current A, prices $/kWh, local wall-clock datetimes (naive local, or aware local; do not mix).
Sign conventions follow the Sol-Ark: grid + = import, - = export; battery + = charging.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from statistics import NormalDist
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .load_profile import LoadProfile, LoadSample
    from .tariff import TariffLike

SLOT = timedelta(minutes=30)

# Caution labels on the dashboard -> probability that the predicted floor guarantee holds
CAUTION_LEVELS: dict[str, float] = {
    "near-certain": 0.97,
    "conservative": 0.90,
    "balanced": 0.75,
    "typical day": 0.50,
}


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class Settings:
    """Owner settings (dashboard helpers). Defaults are the PLAN.md defaults. Prices and the
    free hours live in the ``Tariff``; each car's charge limit and plans in its ``CarState``."""

    enabled: bool = True  # off: the planner leaves both cars alone
    shadow: bool = True  # plan and explain, but the integration sends nothing
    daytime_solar: bool = True  # off: a car plugged in by day waits for the free hours
    caution: str | float = "conservative"  # a CAUTION_LEVELS label or a probability 0.5-0.999
    floor_pct: float = 30.0  # the PREDICTED battery must stay above this
    wanted_at_free_start_pct: float = 30.0  # battery wanted when free hours start (or sunset)
    min_change_min: float = 15.0  # minimum time between current changes
    max_step_a: int = 6  # largest single change
    paid_tolerance_min: float = 5.0  # paid import accepted this long before the safety rule acts

    def caution_p(self) -> float:
        c = self.caution
        p = CAUTION_LEVELS[c] if isinstance(c, str) else float(c)
        return min(0.999, max(0.5, p))

    def caution_z(self) -> float:
        """One-sided z of the caution level (0.90 -> 1.28)."""
        return NormalDist().inv_cdf(self.caution_p())


# --------------------------------------------------------------------------- the site


@dataclass(frozen=True)
class BatterySpec:
    """The home battery and the inverter's dispatch rules (Sol-Ark self-consumption by day;
    in free hours its own settings charge it from the grid to full)."""

    capacity_kwh: float = 31.0  # usable; sensor.sol_ark_battery_capacity_estimate
    max_charge_kw: float = 9.0  # AC side
    max_discharge_kw: float = 9.0
    efficiency: float = 0.92  # one way
    reserve_pct: float = 10.0  # the inverter stops discharging here

    def kwh(self, pct: float) -> float:
        return self.capacity_kwh * pct / 100.0

    def pct(self, kwh: float) -> float:
        return 100.0 * kwh / self.capacity_kwh if self.capacity_kwh > 0 else 0.0


@dataclass(frozen=True)
class ForecastSlot:
    """One Solcast half hour: mean kW from ``start`` for 30 min (detailedForecast's
    period_start, pv_estimate10, pv_estimate, pv_estimate90)."""

    start: datetime
    p10: float
    p50: float
    p90: float

    @property
    def end(self) -> datetime:
        return self.start + SLOT


@dataclass(frozen=True)
class Measured:
    """The latest 5-min means from the Sol-Ark and the Wall Connector (kW)."""

    pv_kw: float  # sensor.sol_ark_inverter_pv_power / 1000
    grid_kw: float  # sensor.sol_ark_inverter_grid_power / 1000; + import, - export
    battery_kw: float  # sensor.sol_ark_inverter_battery_power / 1000; + charging
    car_kw: float  # sensor.tesla_wall_connector_total_power
    soc_pct: float  # sensor.sol_ark_inverter_battery_state_of_charge
    house_kw: float | None = None  # None = pv + grid - battery - car

    @property
    def house(self) -> float:
        if self.house_kw is not None:
            return self.house_kw
        return max(0.0, self.pv_kw + self.grid_kw - self.battery_kw - self.car_kw)


@dataclass(frozen=True)
class PvDay:
    """One past day's solar against the forecast that was in force (the planner's own store;
    HA does not record Solcast's half hours). Feeds the slow multi-day forecast bias."""

    day: date
    actual_kwh: float
    forecast_kwh: float  # sum of the p50 values in force over the day's daylight


# --------------------------------------------------------------------------- cars


@dataclass(frozen=True)
class CarSpec:
    name: str
    capacity_kwh: float  # usable pack energy
    min_a: int  # Rivian 8, Tesla 6 (positive requests below it are raised to it)
    max_a: int  # 48
    step_a: int = 1  # Rivian applies even amps (2), Tesla whole amps (1)
    charge_eff: float = 0.90  # wall -> pack
    billed_commands: bool = False  # Tesla Fleet bills every command

    def kw(self, amps: float, volts: float = 240.0) -> float:
        return amps * volts / 1000.0


RIVIAN_R1S = CarSpec("R1S", 135.0, 8, 48, 2)
TESLA = CarSpec("Tesla", 75.0, 6, 48, 1, billed_commands=True)


@dataclass(frozen=True)
class CarPlan:
    """A concrete plan: the car at ``target_pct`` or more by ``deadline``."""

    target_pct: float
    deadline: datetime


@dataclass(frozen=True)
class RepeatingPlan:
    """ "X % by HH:MM", on the given weekdays (Monday = 0). ``deadlines`` makes concrete plans."""

    target_pct: float
    at: str = "07:30"
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)

    def deadlines(self, now: datetime, days: int = 3) -> tuple[CarPlan, ...]:
        h, m = (int(x) for x in self.at.split(":")[:2])
        out = []
        for k in range(days + 1):
            d = (now + timedelta(days=k)).date()
            t = datetime.combine(d, time(h % 24, m), tzinfo=now.tzinfo)
            if d.weekday() in self.weekdays and t > now:
                out.append(CarPlan(self.target_pct, t))
        return tuple(out)


@dataclass(frozen=True)
class CarState:
    """One car as the integration sees it now."""

    car_id: str  # "rivian" / "tesla" (any stable id)
    spec: CarSpec
    soc_pct: float  # sensor.r1s_battery_state_of_charge / sensor.roxxo_battery_level
    limit_pct: float  # the car's own charge limit
    plugged: bool  # on the Wall Connector now (connected + this car's cable / location)
    plugged_since: datetime | None = None  # start of this plug session (a new value = plug-in)
    commanded_a: int = 0  # last current the integration commanded, 0 = paused / off
    pending: bool = False  # a command is still on its way (Rivian schedule write ~1-2 min)
    stale: bool = False  # the car's data is unavailable or its integration has gone quiet
    plans: tuple[CarPlan, ...] = ()  # future plans (see RepeatingPlan.deadlines)

    @property
    def need_kwh(self) -> float:
        """Wall energy to the charge limit."""
        return max(0.0, self.limit_pct - self.soc_pct) / 100.0 * self.capacity_wall

    @property
    def capacity_wall(self) -> float:
        """Wall kWh per 100 % of the pack."""
        return self.spec.capacity_kwh / self.spec.charge_eff

    def need_for(self, target_pct: float) -> float:
        t = min(target_pct, self.limit_pct)
        return max(0.0, t - self.soc_pct) / 100.0 * self.capacity_wall


# --------------------------------------------------------------------------- the snapshot


@dataclass(frozen=True)
class Snapshot:
    """Everything the planner reads on one call (every 1-5 min)."""

    now: datetime
    soc_pct: float  # home battery now
    battery: BatterySpec
    tariff: TariffLike  # planner_core.Tariff, or anything with the same three methods
    forecast: Sequence[ForecastSlot]  # today 00:00 .. tomorrow 24:00 (parse_detailed_forecast)
    load: LoadProfile  # build_load_profile(): 14 days, recency-weighted, weekday / weekend
    house_recent: Sequence[LoadSample] = ()  # house load over the last 24 h (5-30 min samples)
    pv_today_kwh: float = 0.0  # actual solar since local midnight
    pv_recent_kw: float = 0.0  # actual solar, mean of the last 15 min
    measured: Measured | None = None  # the latest 5-min means (safety rules)
    cars: Sequence[CarState] = ()
    forecast_issued_at: datetime | None = None  # sensor.solcast_pv_forecast_api_last_polled
    pv_days: Sequence[PvDay] = ()  # past days for the multi-day bias (optional)
    volts: float = 240.0
    charger_max_a: int = 48  # Wall Connector setting (read-only device)


# --------------------------------------------------------------------------- outputs


@dataclass(frozen=True)
class CarCommand:
    """What to send to one car. ``amps``: None = send nothing (leave the car alone),
    0 = pause (charge switch off), > 0 = charge at that current (switch on + current)."""

    car_id: str
    amps: int | None
    reason: str


@dataclass(frozen=True)
class TrajectoryPoint:
    """Predicted state at the END of a 15-min step."""

    t: datetime
    soc_pct: float  # battery at the caution level, with the planned car current
    soc_expected_pct: float  # battery on the expected (median) day, with the planned current
    soc_no_car_pct: float  # battery at the caution level without the car
    pv_kw: float  # solar at the caution level
    pv_expected_kw: float  # solar on the expected day (corrected by today's production)
    house_kw: float  # house load at the caution level
    car_kw: float  # the planned car power (caution path)
    price: float  # import price in this step


@dataclass(frozen=True)
class CarOutlook:
    """Where a car's energy is expected to come from (dashboard)."""

    car_id: str
    plugged: bool
    need_kwh: float  # wall kWh to the plan target (or the charge limit without a plan)
    target_pct: float
    by: datetime | None  # the plan deadline (None = no plan)
    solar_kwh: float  # expected from the sun / battery (within the floor rule) by day
    free_kwh: float  # in the next free hours, at full current
    paid_kwh: float  # through paid hours (a plan only)
    short_kwh: float  # not covered before the deadline
    summary: str  # "covered by sun", "about 7 kWh in free hours", "about 3 kWh paid", ...


@dataclass(frozen=True)
class PlannedChange:
    at: datetime
    amps: int
    why: str


@dataclass(frozen=True)
class Decision:
    """The planner's answer for one call."""

    now: datetime
    mode: str  # off | empty | stale | full | day | night (night = any free hours)
    car_id: str | None  # the car on the charger
    commands: tuple[CarCommand, ...]  # one per car in the snapshot
    reason: str  # one sentence for the dashboard
    shadow: bool = False  # True: show, do not send
    trajectory: tuple[TrajectoryPoint, ...] = ()
    outlooks: tuple[CarOutlook, ...] = ()
    next_change: PlannedChange | None = None
    safety: str | None = None  # the safety rule that acted, if any
    debug: Mapping[str, Any] = field(default_factory=dict)

    @property
    def amps(self) -> int | None:
        """The command for the car on the charger (None = send nothing)."""
        c = self.command_for(self.car_id) if self.car_id else None
        return c.amps if c else None

    def command_for(self, car_id: str) -> CarCommand | None:
        return next((c for c in self.commands if c.car_id == car_id), None)
