"""Check that no billable project resources are left running after a teardown.

Read-only: it only calls List/Describe/Get APIs. EC2, Auto Scaling, load balancers, target groups,
HTTP APIs and security groups are discovered by the project name prefix (`predictive-scaling-` by
default). The Lambda function and its log group are checked by their exact Terraform-assigned names,
because the project's IAM user cannot list them (see find_lambdas) — so a stray Lambda with some
other name would not be found. Widening IAM to allow listing would close that gap.

Fails closed: if a check cannot run (AccessDenied, no credentials, network), that is reported as
"could not verify" and the script does NOT exit clean — silence from a failed check must never look
like an empty account.

Exit codes: 0 = verified clean, 1 = billable resources found, 2 = could not verify everything.

Run: python -m scripts.verify_teardown
Env: AWS_PROFILE (default predictive-scaling), AWS_REGION (default us-east-1),
     PROJECT_NAME (default predictive-scaling), LAMBDA_ALIAS (default live)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_PROJECT = "predictive-scaling"
DEFAULT_ALIAS = "live"  # the Lambda alias Terraform publishes (infra/lambda.tf)
LIVE_INSTANCE_STATES = ["pending", "running", "stopping", "stopped"]  # i.e. anything not terminated


@dataclass(frozen=True)
class Finding:
    kind: str
    identifier: str
    billable: bool  # True: costs money while it exists. False: leftover, effectively free.
    detail: str = ""


@dataclass(frozen=True)
class CheckError:
    check: str
    message: str


def _starts_with(name: str, prefix: str) -> bool:
    return name.startswith(prefix)


def find_instances(ec2, prefix: str) -> list[Finding]:
    paginator = ec2.get_paginator("describe_instances")
    findings = []
    for page in paginator.paginate(
        Filters=[
            {"Name": "tag:Name", "Values": [f"{prefix}*"]},
            {"Name": "instance-state-name", "Values": LIVE_INSTANCE_STATES},
        ]
    ):
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                findings.append(
                    Finding("EC2 instance", instance["InstanceId"], True, instance["State"]["Name"])
                )
    return findings


def find_autoscaling_groups(autoscaling, prefix: str) -> list[Finding]:
    paginator = autoscaling.get_paginator("describe_auto_scaling_groups")
    return [
        Finding("Auto Scaling group", g["AutoScalingGroupName"], True, f"desired={g['DesiredCapacity']}")
        for page in paginator.paginate()
        for g in page["AutoScalingGroups"]
        if _starts_with(g["AutoScalingGroupName"], prefix)
    ]


def find_load_balancers(elbv2, prefix: str) -> list[Finding]:
    paginator = elbv2.get_paginator("describe_load_balancers")
    return [
        Finding("Load balancer", lb["LoadBalancerName"], True, lb.get("Type", ""))
        for page in paginator.paginate()
        for lb in page["LoadBalancers"]
        if _starts_with(lb["LoadBalancerName"], prefix)
    ]


def find_target_groups(elbv2, prefix: str) -> list[Finding]:
    paginator = elbv2.get_paginator("describe_target_groups")
    return [
        Finding("Target group", tg["TargetGroupName"], False)
        for page in paginator.paginate()
        for tg in page["TargetGroups"]
        if _starts_with(tg["TargetGroupName"], prefix)
    ]


_NO_PC_CODES = {"ProvisionedConcurrencyConfigNotFoundException", "ResourceNotFoundException"}


def _provisioned_units(lambda_client, function_name: str, qualifier: str) -> int:
    """Provisioned concurrency requested for one version/alias (0 if none is configured).

    Uses GetProvisionedConcurrencyConfig because that is what the project's scoped IAM user is
    granted (the List* variants are not, and the policy is deliberately not widened for this).
    """
    try:
        config = lambda_client.get_provisioned_concurrency_config(FunctionName=function_name, Qualifier=qualifier)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in _NO_PC_CODES:
            return 0
        raise
    return int(config["RequestedProvisionedConcurrentExecutions"])


def _qualifiers(lambda_client, function_name: str, alias: str) -> list[str]:
    """Every published version (PC cannot sit on $LATEST) plus the project's known alias."""
    paginator = lambda_client.get_paginator("list_versions_by_function")
    versions = [
        v["Version"]
        for page in paginator.paginate(FunctionName=function_name)
        for v in page["Versions"]
        if v["Version"] != "$LATEST"
    ]
    return versions + [alias]


def _is_missing(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ResourceNotFoundException"


def find_lambdas(lambda_client, function_names: list[str], alias: str = DEFAULT_ALIAS) -> list[Finding]:
    """Look up each known function by exact name. Functions are free to keep; provisioned
    concurrency on them is not.

    By name rather than by listing: the project's IAM user can only GetFunction on project-prefixed
    ARNs; lambda:ListFunctions does not support resource-level scoping, so it is denied.
    """
    findings = []
    for name in function_names:
        try:
            lambda_client.get_function(FunctionName=name)
        except ClientError as exc:
            if _is_missing(exc):
                continue
            raise
        findings.append(Finding("Lambda function", name, False))
        for qualifier in _qualifiers(lambda_client, name, alias):
            units = _provisioned_units(lambda_client, name, qualifier)
            if units:
                findings.append(Finding("Lambda provisioned concurrency", f"{name}:{qualifier}", True, f"{units} units"))
    return findings


def find_http_apis(apigw, prefix: str) -> list[Finding]:
    """API Gateway v2 has no paginator, so follow NextToken by hand."""
    findings, token = [], None
    while True:
        page = apigw.get_apis(**({"NextToken": token} if token else {}))
        findings += [
            Finding("API Gateway HTTP API", api["ApiId"], False, api["Name"])
            for api in page.get("Items", [])
            if _starts_with(api["Name"], prefix)
        ]
        token = page.get("NextToken")
        if not token:
            return findings


def find_security_groups(ec2, prefix: str) -> list[Finding]:
    paginator = ec2.get_paginator("describe_security_groups")
    return [
        Finding("Security group", sg["GroupName"], False, sg["GroupId"])
        for page in paginator.paginate()
        for sg in page["SecurityGroups"]
        if _starts_with(sg["GroupName"], prefix)
    ]


def find_log_groups(logs, function_names: list[str]) -> list[Finding]:
    """Lambda's log groups, by exact name (DescribeLogStreams is granted; DescribeLogGroups is not)."""
    findings = []
    for name in function_names:
        group = f"/aws/lambda/{name}"
        try:
            logs.describe_log_streams(logGroupName=group, limit=1)
        except ClientError as exc:
            if _is_missing(exc):
                continue
            raise
        findings.append(Finding("CloudWatch log group", group, False))
    return findings


def project_function_names(project: str) -> list[str]:
    """Lambda functions Terraform creates (infra/lambda.tf: ${project_name}-thumbnail-service)."""
    return [f"{project}-thumbnail-service"]


def run_checks(
    session: boto3.Session, project: str = DEFAULT_PROJECT, alias: str = DEFAULT_ALIAS
) -> tuple[list[Finding], list[CheckError]]:
    prefix = f"{project}-"
    functions = project_function_names(project)
    ec2, elbv2 = session.client("ec2"), session.client("elbv2")
    checks: list[tuple[str, Callable[[], list[Finding]]]] = [
        ("EC2 instances", lambda: find_instances(ec2, prefix)),
        ("Auto Scaling groups", lambda: find_autoscaling_groups(session.client("autoscaling"), prefix)),
        ("Load balancers", lambda: find_load_balancers(elbv2, prefix)),
        ("Target groups", lambda: find_target_groups(elbv2, prefix)),
        ("Lambda functions", lambda: find_lambdas(session.client("lambda"), functions, alias)),
        ("API Gateway APIs", lambda: find_http_apis(session.client("apigatewayv2"), prefix)),
        ("Security groups", lambda: find_security_groups(ec2, prefix)),
        ("CloudWatch log groups", lambda: find_log_groups(session.client("logs"), functions)),
    ]
    findings: list[Finding] = []
    errors: list[CheckError] = []
    for name, check in checks:
        try:
            findings += check()
        except (ClientError, BotoCoreError) as exc:
            errors.append(CheckError(name, str(exc)))
    return findings, errors


def exit_code(findings: list[Finding], errors: list[CheckError]) -> int:
    if any(f.billable for f in findings):
        return 1
    if errors:
        return 2
    return 0


def format_report(findings: list[Finding], errors: list[CheckError]) -> str:
    lines = []
    billable = [f for f in findings if f.billable]
    leftovers = [f for f in findings if not f.billable]
    if billable:
        lines.append("STILL RUNNING AND BILLABLE:")
        lines += [f"  - {f.kind}: {f.identifier} {f.detail}".rstrip() for f in billable]
    if leftovers:
        lines.append("Leftovers (effectively free, but the teardown was not complete):")
        lines += [f"  - {f.kind}: {f.identifier} {f.detail}".rstrip() for f in leftovers]
    if errors:
        lines.append("COULD NOT VERIFY (do not treat these as clean):")
        lines += [f"  - {e.check}: {e.message}" for e in errors]
    code = exit_code(findings, errors)
    verdict = {
        0: "OK: nothing billable found, all checks completed.",
        1: "NOT CLEAN: billable resources are still running — run the destroy again or remove them by hand.",
        2: "UNVERIFIED: no billable resources seen, but some checks could not run.",
    }[code]
    return "\n".join(lines + [verdict])


def main() -> int:
    project = os.environ.get("PROJECT_NAME", DEFAULT_PROJECT)
    try:
        session = boto3.Session(
            profile_name=os.environ.get("AWS_PROFILE", "predictive-scaling"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        findings, errors = run_checks(session, project, os.environ.get("LAMBDA_ALIAS", DEFAULT_ALIAS))
    except (ClientError, BotoCoreError) as exc:  # e.g. profile missing: nothing could be checked
        findings, errors = [], [CheckError("session setup", str(exc))]
    print(format_report(findings, errors))
    return exit_code(findings, errors)


if __name__ == "__main__":
    sys.exit(main())
