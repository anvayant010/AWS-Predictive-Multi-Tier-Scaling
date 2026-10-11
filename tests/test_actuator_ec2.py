import random
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

import decision_engine.actuator_ec2 as actuator
from decision_engine.actuation import ActuationRefused
from decision_engine.actuator_ec2 import apply_ec2_decision, scale_ec2
from decision_engine.models import Aggressiveness, Forecast, ScalingDecision
from decision_engine.policy import ABSOLUTE_MAX_INSTANCES, Ec2PolicyConfig, ec2_desired_capacity

ASG = "predictive-scaling-catalog-asg"


@pytest.fixture(scope="module")
def _fake_aws():
    """One fake AWS for the whole module (building it per test is what made these tests slow)."""
    patch = pytest.MonkeyPatch()
    patch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        session = boto3.Session(region_name="us-east-1")
        ec2 = session.client("ec2")
        ami = ec2.describe_images()["Images"][0]["ImageId"]
        ec2.create_launch_template(LaunchTemplateName="test-lt", LaunchTemplateData={"ImageId": ami})
        client = session.client("autoscaling")
        client.create_auto_scaling_group(
            AutoScalingGroupName=ASG,
            MinSize=1,
            MaxSize=10,
            DesiredCapacity=1,
            LaunchTemplate={"LaunchTemplateName": "test-lt", "Version": "$Latest"},
            AvailabilityZones=["us-east-1a"],
        )
        yield client
    patch.undo()


@pytest.fixture
def autoscaling(_fake_aws):
    """The shared client, with the group reset to a known state so every test is independent."""
    _fake_aws.update_auto_scaling_group(AutoScalingGroupName=ASG, MinSize=1, MaxSize=10, DesiredCapacity=1)
    return _fake_aws


def desired_capacity(autoscaling) -> int:
    return autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])["AutoScalingGroups"][0]["DesiredCapacity"]


CAPS = {"min_capacity": 1, "max_capacity": 10}


# --- normal behaviour --------------------------------------------------------------------------


def test_scales_the_group_when_not_a_dry_run(autoscaling) -> None:
    result = scale_ec2(autoscaling, ASG, 4, dry_run=False, **CAPS)

    assert (result.status, result.previous, result.requested) == ("applied", 1, 4)
    assert desired_capacity(autoscaling) == 4


def test_dry_run_is_the_default_and_changes_nothing(autoscaling) -> None:
    result = scale_ec2(autoscaling, ASG, 4, **CAPS)  # no dry_run argument

    assert result.status == "dry_run"
    assert "would set 1 -> 4" in result.detail
    assert desired_capacity(autoscaling) == 1


def test_noop_when_already_at_the_requested_capacity(autoscaling, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(autoscaling, "set_desired_capacity", lambda **kw: calls.append(kw))

    result = scale_ec2(autoscaling, ASG, 1, dry_run=False, **CAPS)

    assert result.status == "noop"
    assert calls == []  # no needless AWS write


def test_scales_down_as_well_as_up(autoscaling) -> None:
    scale_ec2(autoscaling, ASG, 5, dry_run=False, **CAPS)
    scale_ec2(autoscaling, ASG, 2, dry_run=False, **CAPS)

    assert desired_capacity(autoscaling) == 2


# --- the caps: refuse, never exceed, never clamp silently -----------------------------------


def test_refuses_above_the_configured_cap_and_leaves_the_group_unchanged(autoscaling) -> None:
    with pytest.raises(ActuationRefused, match="outside the configured caps"):
        scale_ec2(autoscaling, ASG, 6, dry_run=False, min_capacity=1, max_capacity=5)

    assert desired_capacity(autoscaling) == 1


def test_refuses_below_the_configured_minimum(autoscaling) -> None:
    with pytest.raises(ActuationRefused):
        scale_ec2(autoscaling, ASG, 0, dry_run=False, min_capacity=1, max_capacity=5)

    assert desired_capacity(autoscaling) == 1


def test_a_cap_above_the_absolute_ceiling_is_rejected(autoscaling) -> None:
    with pytest.raises(ActuationRefused, match="absolute ceiling"):
        scale_ec2(autoscaling, ASG, 3, dry_run=False, min_capacity=1, max_capacity=ABSOLUTE_MAX_INSTANCES + 1)


def test_refuses_when_the_group_in_aws_has_a_lower_max_than_the_config(autoscaling, monkeypatch) -> None:
    autoscaling.update_auto_scaling_group(AutoScalingGroupName=ASG, MaxSize=4)
    calls = []
    monkeypatch.setattr(autoscaling, "set_desired_capacity", lambda **kw: calls.append(kw))

    with pytest.raises(ActuationRefused, match="group's own limits"):
        scale_ec2(autoscaling, ASG, 6, dry_run=False, **CAPS)  # config allows 10, AWS group allows 4

    assert calls == []


def test_a_refused_request_never_reaches_the_aws_write(autoscaling, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(autoscaling, "set_desired_capacity", lambda **kw: calls.append(kw))

    for bad in (-1, 11, 10_000):
        with pytest.raises(ActuationRefused):
            scale_ec2(autoscaling, ASG, bad, dry_run=False, **CAPS)

    assert calls == []


@pytest.mark.parametrize("bad", [True, False, 3.0, "3", None])
def test_non_integer_requests_are_refused(autoscaling, bad) -> None:
    with pytest.raises(ActuationRefused, match="integer"):
        scale_ec2(autoscaling, ASG, bad, dry_run=False, **CAPS)


def test_invalid_cap_configuration_is_refused(autoscaling) -> None:
    with pytest.raises(ActuationRefused, match="invalid caps"):
        scale_ec2(autoscaling, ASG, 3, dry_run=False, min_capacity=5, max_capacity=2)


def test_unknown_group_is_refused(autoscaling) -> None:
    with pytest.raises(ActuationRefused, match="does not exist"):
        scale_ec2(autoscaling, "predictive-scaling-nope", 2, dry_run=False, **CAPS)


def test_the_dry_run_path_applies_the_same_checks(autoscaling) -> None:
    with pytest.raises(ActuationRefused):
        scale_ec2(autoscaling, ASG, 99, dry_run=True, **CAPS)
    with pytest.raises(ActuationRefused):
        scale_ec2(autoscaling, "predictive-scaling-nope", 2, dry_run=True, **CAPS)


def test_the_cap_holds_for_any_request_and_any_caps(autoscaling) -> None:
    rng = random.Random(7)
    for _ in range(80):
        caps = {"min_capacity": rng.randint(0, 2), "max_capacity": rng.randint(3, ABSOLUTE_MAX_INSTANCES)}
        desired = rng.randint(-3, 15)
        before = desired_capacity(autoscaling)
        try:
            scale_ec2(autoscaling, ASG, desired, dry_run=False, **caps)
        except ActuationRefused:
            assert desired_capacity(autoscaling) == before  # refusal never changes anything
        else:
            after = desired_capacity(autoscaling)
            assert caps["min_capacity"] <= after <= caps["max_capacity"]
            assert after == desired


# --- policy -> actuator ------------------------------------------------------------------------


def policy_decision(p90_level: float, config: Ec2PolicyConfig, current: int = 1) -> ScalingDecision:
    n = 15
    forecast = Forecast(
        "catalog_service", datetime(2026, 6, 1, tzinfo=timezone.utc), tuple([0.0] * n), tuple([p90_level / 2] * n), tuple([p90_level] * n)
    )
    return ec2_desired_capacity(forecast, current, config, Aggressiveness.BALANCED)


def test_a_policy_decision_is_applied_with_the_configs_caps(autoscaling) -> None:
    config = Ec2PolicyConfig(capacity_per_instance_rpm=50)
    decision = policy_decision(150.0, config)  # ceil(150/50) = 3

    result = apply_ec2_decision(autoscaling, ASG, decision, config, dry_run=False)

    assert result.status == "applied" and desired_capacity(autoscaling) == 3


def test_even_an_absurd_forecast_cannot_push_the_group_past_the_cap(autoscaling) -> None:
    config = Ec2PolicyConfig(max_instances=6)
    decision = policy_decision(1e12, config)

    apply_ec2_decision(autoscaling, ASG, decision, config, dry_run=False)

    assert desired_capacity(autoscaling) == 6


def test_a_decision_over_the_cap_from_a_buggy_caller_is_refused(autoscaling) -> None:
    config = Ec2PolicyConfig(max_instances=5)
    forged = ScalingDecision("catalog_service", "ec2_asg", desired=9, uncapped=9, cap_applied=False, current=1, quantile="p90", demand=1.0, reason="bug")

    with pytest.raises(ActuationRefused):
        apply_ec2_decision(autoscaling, ASG, forged, config, dry_run=False)

    assert desired_capacity(autoscaling) == 1


def test_a_lambda_decision_is_not_applied_to_an_asg(autoscaling) -> None:
    wrong = ScalingDecision("thumbnail_service", "lambda_provisioned_concurrency", desired=1, uncapped=1, cap_applied=False, current=0, quantile="p90", demand=1.0, reason="x")

    with pytest.raises(ActuationRefused, match="not 'lambda_provisioned_concurrency'"):
        apply_ec2_decision(autoscaling, ASG, wrong, Ec2PolicyConfig(), dry_run=False)


def test_apply_ec2_decision_defaults_to_dry_run(autoscaling) -> None:
    config = Ec2PolicyConfig(capacity_per_instance_rpm=50)

    result = apply_ec2_decision(autoscaling, ASG, policy_decision(150.0, config), config)

    assert result.status == "dry_run" and desired_capacity(autoscaling) == 1


def test_the_module_creates_no_clients_of_its_own() -> None:
    # Explicit parameters, no hidden global state: nothing here builds a boto3 client or session.
    source = Path(actuator.__file__).read_text()

    assert "import boto3" not in source and "boto3." not in source
