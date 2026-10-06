"""Kill switch: an SSM parameter the loop must check before every actuation cycle.

FAILS CLOSED. Actuation is allowed only when the parameter exists and its value is exactly RELEASED
(case/whitespace-insensitive). Everything else blocks actuation: the value ENGAGED, a missing
parameter, an unexpected value ("off", "false", ""), an AWS error, missing credentials. An unreadable
kill switch must never be mistaken for "go".

The parameter lives under /predictive-scaling/ because that is the only SSM path the project's scoped
IAM user may touch. It is created and flipped by the small CLI at the bottom (engage is the default
safe state) rather than by Terraform, so it is independent of the ephemeral infra lifecycle: it keeps
its value across `make destroy` / re-apply.

CLI:  python -m decision_engine.kill_switch status | engage | release --yes
Env:  AWS_PROFILE (default predictive-scaling), AWS_REGION (default us-east-1), KILL_SWITCH_PARAMETER
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_PARAMETER = "/predictive-scaling/kill-switch"
ENGAGED = "ENGAGED"
RELEASED = "RELEASED"


@dataclass(frozen=True)
class KillSwitchStatus:
    engaged: bool  # True => do NOT actuate
    reason: str
    value: str | None = None
    last_modified: datetime | None = None


def _engaged(reason: str, value: str | None = None, last_modified: datetime | None = None) -> KillSwitchStatus:
    return KillSwitchStatus(True, reason, value, last_modified)


def read_kill_switch(ssm, parameter_name: str = DEFAULT_PARAMETER) -> KillSwitchStatus:
    """Read the switch fresh from SSM (never cached). Only AWS/connection problems are absorbed into
    an 'engaged' result; programming errors propagate so they cannot hide behind the safe default."""
    try:
        parameter = ssm.get_parameter(Name=parameter_name)["Parameter"]
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ParameterNotFound":
            return _engaged(f"parameter {parameter_name} does not exist; treating as engaged (fail closed)")
        return _engaged(f"could not read {parameter_name} ({code}); treating as engaged (fail closed)")
    except BotoCoreError as exc:
        return _engaged(f"could not read {parameter_name} ({type(exc).__name__}); treating as engaged (fail closed)")

    value = str(parameter.get("Value", "")).strip().upper()
    modified = parameter.get("LastModifiedDate")
    if value == RELEASED:
        return KillSwitchStatus(False, "kill switch released by an operator", value, modified)
    return _engaged(f"value {value!r} is not {RELEASED}", value, modified)


def _put(ssm, parameter_name: str, value: str) -> None:
    ssm.put_parameter(
        Name=parameter_name,
        Value=value,
        Type="String",
        Overwrite=True,
        Description="Predictive scaling kill switch: actuation is allowed only when RELEASED.",
    )


def engage(ssm, parameter_name: str = DEFAULT_PARAMETER) -> None:
    """Block all actuation. Also creates the parameter if it does not exist yet."""
    _put(ssm, parameter_name, ENGAGED)


def release(ssm, parameter_name: str = DEFAULT_PARAMETER) -> None:
    """Allow actuation (the loop must ALSO have DRY_RUN=false before anything is changed)."""
    _put(ssm, parameter_name, RELEASED)


def _describe(status: KillSwitchStatus) -> str:
    state = "ENGAGED (actuation blocked)" if status.engaged else "RELEASED (actuation allowed)"
    when = f" | last modified {status.last_modified:%Y-%m-%d %H:%M UTC}" if status.last_modified else ""
    return f"{state} | {status.reason}{when}"


def main(argv: list[str] | None = None, ssm=None) -> int:
    parser = argparse.ArgumentParser(description="Operate the predictive-scaling kill switch.")
    parser.add_argument("command", choices=["status", "engage", "release"])
    parser.add_argument("--yes", action="store_true", help="required for 'release'")
    args = parser.parse_args(argv)
    name = os.environ.get("KILL_SWITCH_PARAMETER", DEFAULT_PARAMETER)

    if args.command == "release" and not args.yes:
        print("Refusing to release without --yes: releasing allows real scaling actions "
              "(the loop must also have DRY_RUN=false).")
        return 2

    if ssm is None:
        import boto3  # imported here so the library functions above stay client-agnostic

        session = boto3.Session(
            profile_name=os.environ.get("AWS_PROFILE", "predictive-scaling"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        ssm = session.client("ssm")

    if args.command == "engage":
        engage(ssm, name)
    elif args.command == "release":
        release(ssm, name)
    print(_describe(read_kill_switch(ssm, name)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
