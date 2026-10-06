"""Lambda actuator: set provisioned concurrency on a function alias, within hard caps.

Same safety properties as the EC2 actuator (each has a test in tests/test_actuator_lambda.py):
- dry_run defaults to True. A dry run does everything except the final put/delete call: the same
  validation and the same read-only lookups against the real function/alias.
- Caps are enforced here as well as in decision_engine.policy. A request outside [min, max] is
  REFUSED (raises), never silently clamped; the caps themselves are bounded by
  ABSOLUTE_MAX_LAMBDA_CONCURRENCY.
- No hidden state: the boto3 client is passed in.

Lambda-specific points:
- Provisioned concurrency can't be set on $LATEST, so this targets an alias (the project's `live`).
  The alias must already exist: a typo is refused, not turned into a stray configuration.
- AWS does not accept 0 units. A target of 0 means "remove the configuration".
- AWS allocates the capacity asynchronously: a successful put only means the request was accepted.
  The configuration reports IN_PROGRESS until it is READY; use `provisioned_status` to check.
"""

from __future__ import annotations

from dataclasses import dataclass

from botocore.exceptions import ClientError

from decision_engine.actuation import ActuationRefused, ActuationResult
from decision_engine.models import ScalingDecision
from decision_engine.policy import ABSOLUTE_MAX_LAMBDA_CONCURRENCY, LambdaPolicyConfig

TARGET = "lambda_provisioned_concurrency"
NO_CONFIG_CODE = "ProvisionedConcurrencyConfigNotFoundException"
STATUS_FAILED = "FAILED"


@dataclass(frozen=True)
class ProvisionedStatus:
    requested: int
    available: int
    allocated: int
    status: str  # IN_PROGRESS | READY | FAILED
    reason: str = ""


def _validate_request(desired: object, alias: str, min_concurrency: int, max_concurrency: int) -> None:
    if isinstance(desired, bool) or not isinstance(desired, int):
        raise ActuationRefused(f"desired concurrency must be an integer, got {desired!r}")
    if not alias or alias == "$LATEST":
        raise ActuationRefused("provisioned concurrency needs a named alias, not $LATEST or an empty qualifier")
    if min_concurrency < 0 or max_concurrency < min_concurrency:
        raise ActuationRefused(f"invalid caps: min={min_concurrency}, max={max_concurrency}")
    if max_concurrency > ABSOLUTE_MAX_LAMBDA_CONCURRENCY:
        raise ActuationRefused(
            f"max_concurrency {max_concurrency} exceeds the absolute ceiling {ABSOLUTE_MAX_LAMBDA_CONCURRENCY}"
        )
    if not min_concurrency <= desired <= max_concurrency:
        raise ActuationRefused(f"desired {desired} is outside the configured caps [{min_concurrency}, {max_concurrency}]")


def _require_alias(lambda_client, function_name: str, alias: str) -> None:
    try:
        lambda_client.get_alias(FunctionName=function_name, Name=alias)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            raise ActuationRefused(f"alias {alias!r} of function {function_name!r} does not exist") from exc
        raise


def provisioned_status(lambda_client, function_name: str, alias: str) -> ProvisionedStatus | None:
    """Current configuration on the alias, or None if no provisioned concurrency is configured."""
    try:
        config = lambda_client.get_provisioned_concurrency_config(FunctionName=function_name, Qualifier=alias)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == NO_CONFIG_CODE:
            return None
        raise
    return ProvisionedStatus(
        requested=int(config["RequestedProvisionedConcurrentExecutions"]),
        available=int(config.get("AvailableProvisionedConcurrentExecutions", 0)),
        allocated=int(config.get("AllocatedProvisionedConcurrentExecutions", 0)),
        status=config.get("Status", ""),
        reason=config.get("StatusReason", ""),
    )


def provision_lambda(
    lambda_client,
    function_name: str,
    alias: str,
    desired: int,
    *,
    min_concurrency: int,
    max_concurrency: int,
    dry_run: bool = True,
) -> ActuationResult:
    """Set provisioned concurrency on function:alias to `desired`, if and only if it is within every cap."""
    _validate_request(desired, alias, min_concurrency, max_concurrency)
    _require_alias(lambda_client, function_name, alias)

    resource = f"{function_name}:{alias}"
    existing = provisioned_status(lambda_client, function_name, alias)
    current = existing.requested if existing else 0

    already_there = desired == current and (existing is None or existing.status != STATUS_FAILED)
    if already_there:
        return ActuationResult(TARGET, resource, "noop", current, desired, "already at the requested concurrency")
    if dry_run:
        return ActuationResult(TARGET, resource, "dry_run", current, desired, f"would set {current} -> {desired}")

    if desired == 0:
        lambda_client.delete_provisioned_concurrency_config(FunctionName=function_name, Qualifier=alias)
        return ActuationResult(TARGET, resource, "applied", current, 0, f"removed provisioned concurrency ({current} -> 0)")

    lambda_client.put_provisioned_concurrency_config(
        FunctionName=function_name, Qualifier=alias, ProvisionedConcurrentExecutions=desired
    )
    retry = " (retrying a FAILED configuration)" if existing and existing.status == STATUS_FAILED else ""
    return ActuationResult(
        TARGET,
        resource,
        "applied",
        current,
        desired,
        f"requested {current} -> {desired}{retry}; AWS allocates asynchronously, check provisioned_status for READY",
    )


def apply_lambda_decision(
    lambda_client,
    function_name: str,
    alias: str,
    decision: ScalingDecision,
    config: LambdaPolicyConfig,
    *,
    dry_run: bool = True,
) -> ActuationResult:
    """Apply a policy decision, taking the caps from the same config the policy used."""
    if decision.target != TARGET:
        raise ActuationRefused(f"this actuator handles {TARGET!r} decisions, not {decision.target!r}")
    return provision_lambda(
        lambda_client,
        function_name,
        alias,
        decision.desired,
        min_concurrency=config.min_concurrency,
        max_concurrency=config.max_concurrency,
        dry_run=dry_run,
    )
