import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from moto import mock_aws

import scripts.verify_teardown as vt
from scripts.verify_teardown import (
    CheckError,
    Finding,
    exit_code,
    format_report,
    run_checks,
)

PROJECT = "predictive-scaling"
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # moto does not implement Get/ListProvisionedConcurrencyConfig(s) ("Not yet implemented"), so by
    # default report no provisioned concurrency; tests below cover that logic with fake clients.
    monkeypatch.setattr(vt, "_provisioned_units", lambda *_a, **_k: 0)
    with mock_aws():
        yield boto3.Session(region_name="us-east-1")


def _lambda_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("h.py", "def handler(event, context):\n    return 1\n")
    return buffer.getvalue()


def create_stack(session: boto3.Session, prefix: str = PROJECT) -> None:
    """A miniature copy of the deployed project, all named with `prefix`."""
    ec2 = session.client("ec2")
    ami = ec2.describe_images()["Images"][0]["ImageId"]
    vpc = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
    subnets = [s["SubnetId"] for s in ec2.describe_subnets()["Subnets"][:2]]

    ec2.run_instances(
        ImageId=ami,
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": f"{prefix}-catalog"}]}],
    )
    ec2.create_security_group(GroupName=f"{prefix}-catalog-alb", Description="test", VpcId=vpc)

    elbv2 = session.client("elbv2")
    elbv2.create_load_balancer(Name=f"{prefix}-catalog", Subnets=subnets)
    elbv2.create_target_group(Name=f"{prefix}-catalog", Protocol="HTTP", Port=8000, VpcId=vpc)

    ec2.create_launch_template(LaunchTemplateName=f"{prefix}-lt", LaunchTemplateData={"ImageId": ami})
    session.client("autoscaling").create_auto_scaling_group(
        AutoScalingGroupName=f"{prefix}-catalog-asg",
        MinSize=0,
        MaxSize=1,
        DesiredCapacity=0,
        LaunchTemplate={"LaunchTemplateName": f"{prefix}-lt", "Version": "$Latest"},
        AvailabilityZones=["us-east-1a"],
    )

    role = session.client("iam").create_role(RoleName=f"{prefix}-role", AssumeRolePolicyDocument="{}")["Role"]
    session.client("lambda").create_function(
        FunctionName=f"{prefix}-thumbnail-service",
        Runtime="python3.11",
        Role=role["Arn"],
        Handler="h.handler",
        Code={"ZipFile": _lambda_zip()},
    )
    session.client("apigatewayv2").create_api(Name=f"{prefix}-thumbnail-api", ProtocolType="HTTP")
    session.client("logs").create_log_group(logGroupName=f"/aws/lambda/{prefix}-thumbnail-service")


def kinds(findings: list[Finding], billable: bool) -> set[str]:
    return {f.kind for f in findings if f.billable is billable}


def test_empty_account_is_verified_clean(session) -> None:
    findings, errors = run_checks(session, PROJECT)

    assert findings == [] and errors == []
    assert exit_code(findings, errors) == 0
    assert format_report(findings, errors).startswith("OK:")


def test_a_deployed_stack_is_reported_as_billable_and_leftovers(session) -> None:
    create_stack(session)

    findings, errors = run_checks(session, PROJECT)

    assert errors == []
    assert {"EC2 instance", "Auto Scaling group", "Load balancer"} <= kinds(findings, billable=True)
    assert {"Target group", "Lambda function", "API Gateway HTTP API", "Security group", "CloudWatch log group"} <= kinds(
        findings, billable=False
    )
    assert exit_code(findings, errors) == 1
    assert "STILL RUNNING AND BILLABLE" in format_report(findings, errors)


def test_resources_belonging_to_other_projects_are_ignored(session) -> None:
    create_stack(session, prefix="some-other-project")

    findings, errors = run_checks(session, PROJECT)

    assert findings == [] and errors == []


def test_a_name_that_merely_starts_with_the_project_name_is_not_matched(session) -> None:
    # "predictive-scalingX-..." must not match the "predictive-scaling-" prefix.
    create_stack(session, prefix="predictive-scalingX")

    findings, _errors = run_checks(session, PROJECT)

    assert findings == []


def test_terminated_instances_do_not_count(session) -> None:
    create_stack(session)
    ec2 = session.client("ec2")
    instance_id = ec2.describe_instances()["Reservations"][0]["Instances"][0]["InstanceId"]
    ec2.terminate_instances(InstanceIds=[instance_id])

    findings, _errors = run_checks(session, PROJECT)

    assert "EC2 instance" not in kinds(findings, billable=True)


def test_leftovers_alone_are_not_billable_but_are_reported(session) -> None:
    ec2 = session.client("ec2")
    vpc = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
    ec2.create_security_group(GroupName=f"{PROJECT}-catalog-instance", Description="test", VpcId=vpc)

    findings, errors = run_checks(session, PROJECT)

    assert exit_code(findings, errors) == 0
    assert "Leftovers" in format_report(findings, errors)


def test_provisioned_concurrency_counts_as_billable(session, monkeypatch) -> None:
    create_stack(session)
    monkeypatch.setattr(vt, "_provisioned_units", lambda _c, _fn, qualifier: 2 if qualifier == "live" else 0)

    findings = vt.find_lambdas(session.client("lambda"), [f"{PROJECT}-thumbnail-service"])

    pc = [f for f in findings if f.kind == "Lambda provisioned concurrency"]
    assert [(f.identifier, f.billable, f.detail) for f in pc] == [(f"{PROJECT}-thumbnail-service:live", True, "2 units")]


def test_qualifiers_cover_published_versions_and_the_alias_but_not_latest(session) -> None:
    create_stack(session)
    lambda_client = session.client("lambda")
    lambda_client.publish_version(FunctionName=f"{PROJECT}-thumbnail-service")

    assert vt._qualifiers(lambda_client, f"{PROJECT}-thumbnail-service", "live") == ["1", "live"]


def test_a_missing_function_and_log_group_are_simply_absent(session) -> None:
    assert vt.find_lambdas(session.client("lambda"), [f"{PROJECT}-thumbnail-service"]) == []
    assert vt.find_log_groups(session.client("logs"), [f"{PROJECT}-thumbnail-service"]) == []


def test_an_existing_function_and_log_group_are_found_by_exact_name(session) -> None:
    create_stack(session)

    functions = vt.find_lambdas(session.client("lambda"), [f"{PROJECT}-thumbnail-service"])
    groups = vt.find_log_groups(session.client("logs"), [f"{PROJECT}-thumbnail-service"])

    assert [(f.kind, f.billable) for f in functions] == [("Lambda function", False)]
    assert [f.identifier for f in groups] == [f"/aws/lambda/{PROJECT}-thumbnail-service"]


class DeniedLambda:
    def get_function(self, **_kwargs):
        raise _access_denied()


def test_denied_function_lookup_raises_instead_of_looking_empty() -> None:
    # run_checks turns this into a "could not verify" entry (see the fail-closed tests above).
    with pytest.raises(ClientError):
        vt.find_lambdas(DeniedLambda(), [f"{PROJECT}-thumbnail-service"])


class FakeLambda:
    def __init__(self, response=None, error_code=None) -> None:
        self.response, self.error_code = response, error_code

    def get_provisioned_concurrency_config(self, **_kwargs):
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code, "Message": "x"}}, "GetProvisionedConcurrencyConfig")
        return self.response


def test_provisioned_units_reads_the_requested_amount() -> None:
    client = FakeLambda(response={"RequestedProvisionedConcurrentExecutions": 3})

    assert vt._provisioned_units(client, "fn", "live") == 3


@pytest.mark.parametrize("code", ["ProvisionedConcurrencyConfigNotFoundException", "ResourceNotFoundException"])
def test_no_config_or_no_such_alias_means_zero_units(code: str) -> None:
    assert vt._provisioned_units(FakeLambda(error_code=code), "fn", "live") == 0


def test_access_denied_while_checking_concurrency_is_not_treated_as_none() -> None:
    with pytest.raises(ClientError):
        vt._provisioned_units(FakeLambda(error_code="AccessDeniedException"), "fn", "live")


# --- fail closed -------------------------------------------------------------------------------


def _access_denied() -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "not allowed"}}, "DescribeLoadBalancers")


def test_a_failed_check_is_reported_as_unverified_never_clean(session, monkeypatch) -> None:
    def deny(*_a, **_k):
        raise _access_denied()

    monkeypatch.setattr(vt, "find_load_balancers", deny)

    findings, errors = run_checks(session, PROJECT)

    assert [e.check for e in errors] == ["Load balancers"]
    assert exit_code(findings, errors) == 2
    report = format_report(findings, errors)
    assert "COULD NOT VERIFY" in report and report.rstrip().endswith("some checks could not run.")


def test_other_checks_still_run_when_one_fails(session, monkeypatch) -> None:
    create_stack(session)
    monkeypatch.setattr(vt, "find_load_balancers", lambda *_a: (_ for _ in ()).throw(_access_denied()))

    findings, errors = run_checks(session, PROJECT)

    assert len(errors) == 1
    assert "EC2 instance" in kinds(findings, billable=True)  # the instance was still found


def test_missing_credentials_are_reported_not_swallowed(session, monkeypatch) -> None:
    monkeypatch.setattr(vt, "find_http_apis", lambda *_a: (_ for _ in ()).throw(NoCredentialsError()))

    _findings, errors = run_checks(session, PROJECT)

    assert [e.check for e in errors] == ["API Gateway APIs"]


def test_billable_resources_outrank_unverified_in_the_exit_code() -> None:
    billable = [Finding("EC2 instance", "i-1", True)]
    errors = [CheckError("Load balancers", "denied")]

    assert exit_code(billable, errors) == 1
    assert exit_code([], errors) == 2
    assert exit_code([Finding("Security group", "sg-1", False)], []) == 0


def test_main_exits_unverified_when_the_profile_does_not_exist(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AWS_PROFILE", "profile-that-does-not-exist")

    code = vt.main()

    assert code == 2
    assert "UNVERIFIED" in capsys.readouterr().out


# --- the script is read-only and the Makefile does what it claims -------------------------------


def test_verifier_only_makes_read_calls() -> None:
    source = Path(vt.__file__).read_text()
    calls = [word for word in source.replace("(", " ").split() if word.startswith(("delete_", "terminate_", "create_", "put_", "update_", "modify_"))]

    assert calls == []


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_destroy_dry_run_destroys_only_infra_and_then_verifies() -> None:
    plain = subprocess.run(["make", "-n", "destroy"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    scripted = subprocess.run(
        ["make", "-n", "destroy", "AUTO_APPROVE=yes"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout

    assert "cd infra && terraform destroy" in plain
    assert "-auto-approve" not in plain  # interactive by default
    assert "-auto-approve" in scripted
    assert "bootstrap" not in plain  # the persistent bootstrap stack is never destroyed
    assert "scripts.verify_teardown" in plain  # verification always follows
