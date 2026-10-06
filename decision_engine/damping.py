"""Damping: stop capacity from flapping on a noisy forecast.

Found in the first real loop replay (Phase 6d): the Lambda tier would create and delete its
provisioned-concurrency configuration 14 times each in 4 hours (median "on" period 2 minutes) because the
forecast hovers around the provisioning threshold. AWS needs ~1-3 minutes to make provisioned capacity
READY, so most of those periods ended before the capacity was usable; and an EC2 instance launched for a
2 -> 3 -> 2 blip is terminated before it is healthy.

Rules (pure function, explicit state, no clock of its own):
- INCREASES pass through immediately: capacity is wanted exactly when load is rising.
- DECREASES are held until BOTH (a) `hold_minutes` have passed since the last increase, and
  (b) `down_confirmations` consecutive cycles have proposed a lower target. The decrease then goes only to
  the HIGHEST target proposed during that streak, so it never drops further than the evidence supports.
- The hard caps always win: a current capacity above `max_capacity` is forced down at once, whatever the
  hold says. Damping can delay a release, never exceed a cap.

Defaults come from reasoning about AWS timings (readiness ~1-3 min, EC2 boot ~3-6 min), not from tuning
on the replay data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class DampingConfig:
    hold_minutes: int = 10
    down_confirmations: int = 1

    def __post_init__(self) -> None:
        if self.hold_minutes < 0:
            raise ValueError("hold_minutes must be >= 0")
        if self.down_confirmations < 1:
            raise ValueError("down_confirmations must be >= 1 (1 = release as soon as the hold is over)")


# Per service: EC2 is slower to react (boot ~3-6 min, cycle every 5 min) so it also needs confirmation;
# Lambda cycles every minute and mainly needs the hold.
DEFAULT_DAMPING = {
    "catalog_service": DampingConfig(hold_minutes=10, down_confirmations=3),
    "thumbnail_service": DampingConfig(hold_minutes=10, down_confirmations=1),
}
NO_DAMPING = DampingConfig(hold_minutes=0, down_confirmations=1)  # identity: damp(...) returns the proposal


@dataclass
class DampingState:
    last_increase: datetime | None = None
    lower_streak: int = 0
    lower_ceiling: int | None = None  # highest target proposed during the current streak of "go lower"


@dataclass(frozen=True)
class DampedDecision:
    desired: int
    proposed: int
    held: bool  # True when the proposal was NOT followed
    note: str


def _reset_streak(state: DampingState) -> None:
    state.lower_streak = 0
    state.lower_ceiling = None


def damp(
    proposed: int,
    current: int,
    now: datetime,
    state: DampingState,
    config: DampingConfig,
    *,
    min_capacity: int,
    max_capacity: int,
) -> DampedDecision:
    """Return the capacity to actually request, given the policy's `proposed` and what is `current`."""
    if current > max_capacity:  # caps beat damping
        _reset_streak(state)
        return DampedDecision(max_capacity, proposed, True, f"current {current} exceeds the cap {max_capacity}: forced down")

    if proposed > current:  # increases are immediate
        state.last_increase = now
        _reset_streak(state)
        return DampedDecision(proposed, proposed, False, "increase applied immediately")

    if proposed == current:
        _reset_streak(state)
        return DampedDecision(current, proposed, False, "unchanged")

    # proposed < current: the policy wants to release capacity
    if state.last_increase is not None and now - state.last_increase < timedelta(minutes=config.hold_minutes):
        _reset_streak(state)
        waited = (now - state.last_increase).total_seconds() / 60
        return DampedDecision(current, proposed, True, f"holding {current}: raised {waited:.0f} of {config.hold_minutes} min ago")

    state.lower_streak += 1
    state.lower_ceiling = proposed if state.lower_ceiling is None else max(state.lower_ceiling, proposed)
    if state.lower_streak < config.down_confirmations:
        return DampedDecision(
            current, proposed, True, f"holding {current}: lower target seen {state.lower_streak} of {config.down_confirmations} cycles"
        )

    target = max(min(state.lower_ceiling, current), min_capacity)
    _reset_streak(state)
    return DampedDecision(target, proposed, target != proposed, f"released {current} -> {target}")
