"""Scaling policy: turn a Forecast into a capacity number, with hard caps.

Pure functions only — no AWS calls, no globals — so every rule is unit-testable. This module is
the single place caps are enforced in code (not just in Terraform): whatever the model predicts,
the returned `desired` is always within [min, max] of the config, and the configs themselves
cannot be built with a max above the absolute ceilings below.

Assumed values (flagged, not measured): capacity_per_instance_rpm=50 is a deliberately
conservative *demo* number — a real instance handled far more in the Phase 2 load tests, but the
synthetic traffic peaks at ~150 req/min, so a realistic capacity would never need to scale.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from decision_engine.models import Aggressiveness, Forecast, LoadShape, ScalingDecision

# Absolute ceilings. A config with a larger max is rejected, so raising these is a deliberate,
# reviewed code change. EC2 matches the Terraform ASG max_size. Lambda's 10 is now a deliberate
# cost/safety cap, not a quota limit: the account's concurrency quota was raised to 1000 (2026-10-06),
# and provisioned concurrency can use up to (quota - 100) of it. Raise only on purpose.
ABSOLUTE_MAX_INSTANCES = 10
ABSOLUTE_MAX_LAMBDA_CONCURRENCY = 10


@dataclass(frozen=True)
class Profile:
    """What each aggressiveness setting means."""

    quantile: str  # which forecast quantile to size for
    ec2_margin: int  # extra instances on top of the computed need
    lambda_multiplier: float  # safety factor on computed concurrency


# Lambda's 1.5x/2x factors come from the burst-size check: p90 alone covered the real burst size
# only 46-67% of the time (docs/design.md, Phase 4).
PROFILES: dict[Aggressiveness, Profile] = {
    Aggressiveness.COST_OPTIMIZED: Profile("p50", ec2_margin=0, lambda_multiplier=1.0),
    Aggressiveness.BALANCED: Profile("p90", ec2_margin=0, lambda_multiplier=1.5),
    Aggressiveness.LATENCY_OPTIMIZED: Profile("p90", ec2_margin=1, lambda_multiplier=2.0),
}


@dataclass(frozen=True)
class Ec2PolicyConfig:
    capacity_per_instance_rpm: float = 50.0
    min_instances: int = 1
    max_instances: int = ABSOLUTE_MAX_INSTANCES
    # Look this far ahead: should cover instance boot + health check (~3-6 min observed) plus
    # the gap to the next loop cycle.
    lookahead_minutes: int = 10
    # Limit how fast capacity is released per cycle, to avoid flapping on a noisy forecast.
    max_scale_down_step: int = 1

    def __post_init__(self) -> None:
        if self.capacity_per_instance_rpm <= 0:
            raise ValueError("capacity_per_instance_rpm must be positive")
        if self.min_instances < 0 or self.max_instances < self.min_instances:
            raise ValueError("need 0 <= min_instances <= max_instances")
        if self.max_instances > ABSOLUTE_MAX_INSTANCES:
            raise ValueError(f"max_instances above the absolute ceiling of {ABSOLUTE_MAX_INSTANCES}")
        if self.lookahead_minutes < 1 or self.max_scale_down_step < 1:
            raise ValueError("lookahead_minutes and max_scale_down_step must be >= 1")


@dataclass(frozen=True)
class LambdaPolicyConfig:
    avg_duration_seconds: float = 0.1
    min_concurrency: int = 0
    max_concurrency: int = 5
    # Usable warning is only ~3-4 minutes (ramp length) and concurrency needs ~1-3 min to ready.
    lookahead_minutes: int = 3
    # Below this forecast load, don't hold provisioned concurrency at all (it costs money while
    # idle, and quiet traffic doesn't suffer cold starts badly enough to justify it).
    # 35 was chosen on a validation week by a rule fixed in advance (decision_engine/tune_lambda.py):
    # vs. 20 it cut provisioned minutes ~43% on the untouched test week for ~4 points of coverage.
    # Tuned on synthetic data only; revisit with real traffic.
    provision_threshold_per_min: float = 35.0
    # Hysteresis: once provisioned concurrency is already on (current > 0), keep it on until the forecast
    # falls below THIS lower threshold instead of the provisioning one. Stops a forecast that hovers
    # around the trigger from switching capacity on and off every few minutes. None = same as the
    # provisioning threshold, i.e. no hysteresis.
    release_threshold_per_min: float | None = None

    @property
    def effective_release_threshold(self) -> float:
        if self.release_threshold_per_min is None:
            return self.provision_threshold_per_min
        return self.release_threshold_per_min

    def __post_init__(self) -> None:
        if self.avg_duration_seconds <= 0:
            raise ValueError("avg_duration_seconds must be positive")
        if self.min_concurrency < 0 or self.max_concurrency < self.min_concurrency:
            raise ValueError("need 0 <= min_concurrency <= max_concurrency")
        if self.max_concurrency > ABSOLUTE_MAX_LAMBDA_CONCURRENCY:
            raise ValueError(
                f"max_concurrency above the absolute ceiling of {ABSOLUTE_MAX_LAMBDA_CONCURRENCY}"
            )
        if self.lookahead_minutes < 1 or self.provision_threshold_per_min < 0:
            raise ValueError("lookahead_minutes must be >= 1 and the threshold non-negative")
        if self.release_threshold_per_min is not None and not (
            0 <= self.release_threshold_per_min <= self.provision_threshold_per_min
        ):
            raise ValueError("release_threshold_per_min must be between 0 and provision_threshold_per_min")


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def ec2_desired_capacity(
    forecast: Forecast,
    current_capacity: int,
    config: Ec2PolicyConfig = Ec2PolicyConfig(),
    aggressiveness: Aggressiveness = Aggressiveness.BALANCED,
    shape: LoadShape = LoadShape.SUSTAINED,
) -> ScalingDecision:
    """desired = ceil(demand over the lookahead window / per-instance capacity) + margin.

    demand is the window's peak, except for a BURSTY shape: a short spike will be over before a
    new instance (~3-6 min to boot) can serve it, so we size to the typical (median) level instead
    of paying for capacity that arrives too late. Caps and the scale-down limit apply either way.
    """
    profile = PROFILES[aggressiveness]
    window = forecast.quantile(profile.quantile)[: config.lookahead_minutes]
    if shape is LoadShape.BURSTY:
        demand = statistics.median(window)
        basis = f"median {demand:.1f} (bursty: ignoring transient spike of {max(window):.1f})"
    else:
        demand = max(window)
        basis = f"peak {demand:.1f}"
    needed = math.ceil(demand / config.capacity_per_instance_rpm) + profile.ec2_margin

    desired = _clamp(needed, config.min_instances, config.max_instances)
    if desired < current_capacity:
        desired = max(desired, current_capacity - config.max_scale_down_step)
    # Final clamp so the cap holds even if the current capacity was already out of bounds.
    desired = _clamp(desired, config.min_instances, config.max_instances)

    return ScalingDecision(
        service=forecast.service,
        target="ec2_asg",
        desired=desired,
        uncapped=needed,
        cap_applied=needed > config.max_instances,
        current=current_capacity,
        quantile=profile.quantile,
        demand=demand,
        reason=(
            f"{profile.quantile} {basis} req/min over next {len(window)} min / "
            f"{config.capacity_per_instance_rpm:g} per instance + margin {profile.ec2_margin}"
        ),
    )


def lambda_provisioned_concurrency(
    forecast: Forecast,
    current_concurrency: int,
    config: LambdaPolicyConfig = LambdaPolicyConfig(),
    aggressiveness: Aggressiveness = Aggressiveness.BALANCED,
) -> ScalingDecision:
    """desired = ceil(peak invocations/min / 60 * avg duration * safety factor), 0 if quiet.

    "Quiet" has hysteresis: to switch provisioning ON the peak must reach the provisioning threshold, but
    once it is already on (current_concurrency > 0) it stays on until the peak drops below the lower
    release threshold.
    """
    profile = PROFILES[aggressiveness]
    window = forecast.quantile(profile.quantile)[: config.lookahead_minutes]
    peak = max(window)

    already_on = current_concurrency > 0
    threshold = config.effective_release_threshold if already_on else config.provision_threshold_per_min
    if peak < threshold:
        needed = 0
    else:
        needed = math.ceil(peak / 60.0 * config.avg_duration_seconds * profile.lambda_multiplier)

    desired = _clamp(needed, config.min_concurrency, config.max_concurrency)
    return ScalingDecision(
        service=forecast.service,
        target="lambda_provisioned_concurrency",
        desired=desired,
        uncapped=needed,
        cap_applied=needed > config.max_concurrency,
        current=current_concurrency,
        quantile=profile.quantile,
        demand=peak,
        reason=(
            f"{profile.quantile} peak {peak:.1f} inv/min over next {len(window)} min "
            f"(threshold {threshold:g}{' to stay on' if already_on and threshold != config.provision_threshold_per_min else ''}), "
            f"x{profile.lambda_multiplier:g} safety"
        ),
    )
