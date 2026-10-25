"""planner_core -- the solar EV charging planner (pure Python, standard library only).

No Home Assistant imports: the integration reads its entities, builds a ``Snapshot`` and
calls ``Planner.decide`` every 1-5 minutes; offline tools do the same with recorded data.

Stable API (everything else is internal):

    Planner(settings=None, params=None)
        .decide(snapshot) -> Decision
        .settings                         # replace between calls when the owner edits them
        .state_dict() / .load_state(d)    # persist across restarts (JSON-safe)

    Inputs   Settings, Snapshot, BatterySpec, Measured, ForecastSlot, PvDay, CarSpec,
             CarState, CarPlan, RepeatingPlan, RIVIAN_R1S, TESLA, CAUTION_LEVELS,
             FREE_REFILL, FREE_HOLD, FREE_NORMAL (BatterySpec.free_hours)
    Outputs  Decision, CarCommand, TrajectoryPoint, CarOutlook, PlannedChange
    Helpers  parse_detailed_forecast(*attrs)            Solcast detailedForecast -> slots
             Tariff, PriceWindow, WeeklySchedule        schedule helpers + prices -> $/kWh
             LoadSample, house_load_kw,
             samples_from_statistics(pv, grid, battery, wall)   recorder rows -> samples
             build_load_profile(samples, today)         -> LoadProfile (14 d, weekday/weekend)
"""

from .load_profile import (
    LoadProfile,
    LoadSample,
    build_load_profile,
    house_load_kw,
    samples_from_statistics,
)
from .planner import Params, Planner
from .solcast import forecast_energy, parse_detailed_forecast
from .tariff import PriceWindow, Tariff, TariffLike, WeeklySchedule
from .types import (
    CAUTION_LEVELS,
    FREE_HOLD,
    FREE_HOURS_BATTERY,
    FREE_NORMAL,
    FREE_REFILL,
    RIVIAN_R1S,
    TESLA,
    BatterySpec,
    CarCommand,
    CarOutlook,
    CarPlan,
    CarSpec,
    CarState,
    Decision,
    ForecastSlot,
    Measured,
    PlannedChange,
    PvDay,
    RepeatingPlan,
    Settings,
    Snapshot,
    TrajectoryPoint,
)

__version__ = "1.0.0"

__all__ = [
    "CAUTION_LEVELS",
    "FREE_HOLD",
    "FREE_HOURS_BATTERY",
    "FREE_NORMAL",
    "FREE_REFILL",
    "RIVIAN_R1S",
    "TESLA",
    "BatterySpec",
    "CarCommand",
    "CarOutlook",
    "CarPlan",
    "CarSpec",
    "CarState",
    "Decision",
    "ForecastSlot",
    "LoadProfile",
    "LoadSample",
    "Measured",
    "Params",
    "PlannedChange",
    "Planner",
    "PriceWindow",
    "PvDay",
    "RepeatingPlan",
    "Settings",
    "Snapshot",
    "Tariff",
    "TariffLike",
    "TrajectoryPoint",
    "WeeklySchedule",
    "build_load_profile",
    "forecast_energy",
    "house_load_kw",
    "parse_detailed_forecast",
    "samples_from_statistics",
]
