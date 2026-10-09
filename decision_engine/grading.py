"""Ground truth for grading decisions: what capacity the real traffic actually required.

Pure functions with no database, model or AWS imports, so both the Phase 5c replay (dry_run) and the live
loop's outcome logging (scheduler.outcomes) can share ONE definition of "needed".
"""

from __future__ import annotations

import math

from decision_engine.engine import Route

# Traffic level (invocations/min) at which provisioned concurrency counts as "needed" when grading
# Lambda decisions. Matches the original policy trigger so results stay comparable with the first
# dry-run; held fixed so tuning the policy threshold can't move the goalposts.
LAMBDA_TRUTH_THRESHOLD = 20.0


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def needed_capacity(
    route: Route, actual_peak: float, *, truth_threshold: float = LAMBDA_TRUTH_THRESHOLD
) -> int:
    """Capacity the real peak required: no safety margin, no multiplier, but within the same caps.

    For Lambda, `truth_threshold` is the traffic level at which provisioned concurrency is judged
    worthwhile. It is deliberately independent of the policy's own trigger
    (`provision_threshold_per_min`): if the ground truth moved with the threshold being tuned, every
    threshold would grade itself on its own terms and look better than it is.
    """
    if route.ec2 is not None:
        cfg = route.ec2
        return _clamp(math.ceil(actual_peak / cfg.capacity_per_instance_rpm), cfg.min_instances, cfg.max_instances)
    assert route.lambda_ is not None
    cfg_l = route.lambda_
    if actual_peak < truth_threshold:
        return _clamp(0, cfg_l.min_concurrency, cfg_l.max_concurrency)
    return _clamp(
        math.ceil(actual_peak / 60.0 * cfg_l.avg_duration_seconds), cfg_l.min_concurrency, cfg_l.max_concurrency
    )
