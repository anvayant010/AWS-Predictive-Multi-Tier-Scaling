"""Shared types for the actuators (the code that changes real AWS capacity)."""

from __future__ import annotations

from dataclasses import dataclass


class ActuationRefused(Exception):
    """The actuator declined to act (bad input, cap violation, unknown resource).

    Raised instead of clamping: a request outside the caps means something upstream is wrong, and
    that bug should be loud, not silently turned into "scale to the maximum".
    """


@dataclass(frozen=True)
class ActuationResult:
    target: str  # "ec2_asg" or "lambda_provisioned_concurrency"
    resource: str  # ASG name / function:alias
    status: str  # "applied" | "noop" | "dry_run"
    previous: int  # capacity read from AWS before acting
    requested: int  # capacity asked for
    detail: str = ""
