"""EC2 actuator: set an Auto Scaling group's desired capacity, within hard caps.

Safety properties (each has a test in tests/test_actuator_ec2.py):
- dry_run defaults to True. A dry run does everything except the final set_desired_capacity call:
  the same validation and the same read-only look at the real group, so it exercises the real path.
- Caps are enforced here as well as in decision_engine.policy. A request outside [min, max] is
  REFUSED (raises), never silently clamped.
- The caps themselves are bounded by ABSOLUTE_MAX_INSTANCES, and a request is also checked against
  the group's own MinSize/MaxSize in AWS, so a mis-set config cannot widen the limit.
- No hidden state: the boto3 client is passed in, so it is trivially mockable.
"""

from __future__ import annotations

from decision_engine.actuation import ActuationRefused, ActuationResult
from decision_engine.models import ScalingDecision
from decision_engine.policy import ABSOLUTE_MAX_INSTANCES, Ec2PolicyConfig

TARGET = "ec2_asg"


def _validate_request(desired: object, min_capacity: int, max_capacity: int) -> None:
    if isinstance(desired, bool) or not isinstance(desired, int):
        raise ActuationRefused(f"desired capacity must be an integer, got {desired!r}")
    if min_capacity < 0 or max_capacity < min_capacity:
        raise ActuationRefused(f"invalid caps: min={min_capacity}, max={max_capacity}")
    if max_capacity > ABSOLUTE_MAX_INSTANCES:
        raise ActuationRefused(f"max_capacity {max_capacity} exceeds the absolute ceiling {ABSOLUTE_MAX_INSTANCES}")
    if not min_capacity <= desired <= max_capacity:
        raise ActuationRefused(f"desired {desired} is outside the configured caps [{min_capacity}, {max_capacity}]")


def _describe_group(autoscaling, asg_name: str) -> dict:
    groups = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])["AutoScalingGroups"]
    if not groups:
        raise ActuationRefused(f"Auto Scaling group {asg_name!r} does not exist")
    return groups[0]


def scale_ec2(
    autoscaling,
    asg_name: str,
    desired: int,
    *,
    min_capacity: int,
    max_capacity: int,
    dry_run: bool = True,
    honor_cooldown: bool = False,
) -> ActuationResult:
    """Set the ASG's desired capacity to `desired`, if and only if it is within every cap."""
    _validate_request(desired, min_capacity, max_capacity)

    group = _describe_group(autoscaling, asg_name)
    if not group["MinSize"] <= desired <= group["MaxSize"]:
        raise ActuationRefused(
            f"desired {desired} is outside the group's own limits in AWS "
            f"[{group['MinSize']}, {group['MaxSize']}]"
        )

    current = group["DesiredCapacity"]
    if desired == current:
        return ActuationResult(TARGET, asg_name, "noop", current, desired, "already at the requested capacity")
    if dry_run:
        return ActuationResult(TARGET, asg_name, "dry_run", current, desired, f"would set {current} -> {desired}")

    autoscaling.set_desired_capacity(
        AutoScalingGroupName=asg_name, DesiredCapacity=desired, HonorCooldown=honor_cooldown
    )
    return ActuationResult(TARGET, asg_name, "applied", current, desired, f"set {current} -> {desired}")


def apply_ec2_decision(
    autoscaling,
    asg_name: str,
    decision: ScalingDecision,
    config: Ec2PolicyConfig,
    *,
    dry_run: bool = True,
) -> ActuationResult:
    """Apply a policy decision, taking the caps from the same config the policy used."""
    if decision.target != TARGET:
        raise ActuationRefused(f"this actuator handles {TARGET!r} decisions, not {decision.target!r}")
    return scale_ec2(
        autoscaling,
        asg_name,
        decision.desired,
        min_capacity=config.min_instances,
        max_capacity=config.max_instances,
        dry_run=dry_run,
    )
