"""Plain data types passed between the forecaster, classifier and policy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

QUANTILE_NAMES = ("p10", "p50", "p90")


class Aggressiveness(str, Enum):
    """Cost-vs-latency dial for the scaling policy (see decision_engine.policy.PROFILES)."""

    COST_OPTIMIZED = "cost_optimized"
    BALANCED = "balanced"
    LATENCY_OPTIMIZED = "latency_optimized"


@dataclass(frozen=True)
class Forecast:
    """Demand forecast for one service: one value per minute, starting at origin + 1 minute.

    Validated on construction so nothing downstream ever acts on NaN, negative or inconsistent
    numbers — a bad forecast must fail loudly, not turn into a scaling action.
    """

    service: str
    origin: datetime
    p10: tuple[float, ...]
    p50: tuple[float, ...]
    p90: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.p50:
            raise ValueError("forecast is empty")
        if not (len(self.p10) == len(self.p50) == len(self.p90)):
            raise ValueError("p10/p50/p90 must have the same length")
        for name in QUANTILE_NAMES:
            for value in getattr(self, name):
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{name} contains an invalid value: {value!r}")
        if any(not (lo <= mid <= hi) for lo, mid, hi in zip(self.p10, self.p50, self.p90)):
            raise ValueError("quantiles must satisfy p10 <= p50 <= p90 at every step")

    @property
    def horizon_minutes(self) -> int:
        return len(self.p50)

    def quantile(self, name: str) -> tuple[float, ...]:
        if name not in QUANTILE_NAMES:
            raise ValueError(f"unknown quantile {name!r}")
        return getattr(self, name)


@dataclass(frozen=True)
class ScalingDecision:
    """What the policy wants for one service. Nothing here touches AWS.

    `uncapped` is what the forecast alone asked for; `desired` is what is allowed after caps.
    cap_applied=True means a hard cap changed the outcome — worth surfacing in logs/review.
    """

    service: str
    target: str  # "ec2_asg" or "lambda_provisioned_concurrency"
    desired: int
    uncapped: int
    cap_applied: bool
    current: int | None
    quantile: str
    demand: float  # demand the number was sized for: the window peak, or the median if a spike was ignored
    reason: str


class LoadShape(str, Enum):
    """Shape of the forecast over its window (see decision_engine.classifier)."""

    QUIET = "quiet"
    SUSTAINED = "sustained"
    BURSTY = "bursty"


@dataclass(frozen=True)
class Classification:
    shape: LoadShape
    level: float  # typical (median) demand across the window
    peak: float  # highest demand in the window
    elevated_steps: int  # how many steps sit in the upper half between level and peak
    reason: str
