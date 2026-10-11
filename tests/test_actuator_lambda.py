"""Tests for the Lambda actuator.

moto emulates Lambda functions and aliases, but NOT the provisioned-concurrency calls (put/get/delete
all answer "Not yet implemented"). So those three calls go to `StatefulLambda`, a small stand-in that
keeps one configuration in memory and uses AWS's real error codes; function/alias lookups still go to
moto. This proves the actuator's own logic (caps, refusals, no-op, dry-run, 0 => delete) — it does
NOT prove AWS's real API contract (e.g. async allocation, quota behaviour). That is verified for real
in the build-verification session (Phase 6f).
"""

import io
import random
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import decision_engine.actuator_lambda as actuator
import scripts.verify_teardown as verifier
from decision_engine.actuation import ActuationRefused
from decision_engine.actuator_lambda import apply_lambda_decision, provision_lambda, provisioned_status
from decision_engine.models import Aggressiveness, Forecast, ScalingDecision
from decision_engine.policy import ABSOLUTE_MAX_LAMBDA_CONCURRENCY, LambdaPolicyConfig, lambda_provisioned_concurrency
from tests.fakes import StatefulLambda

FUNCTION = "predictive-scaling-thumbnail-service"
ALIAS = "live"


def _zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("h.py", "def handler(event, context):\n    return 1\n")
    return buffer.getvalue()


@pytest.fixture(scope="module")
def _fake_aws():
    patch = pytest.MonkeyPatch()
    patch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        session = boto3.Session(region_name="us-east-1")
        role = session.client("iam").create_role(RoleName="test-role", AssumeRolePolicyDocument="{}")["Role"]
        client = session.client("lambda")
        client.create_function(
            FunctionName=FUNCTION, Runtime="python3.11", Role=role["Arn"], Handler="h.handler",
            Code={"ZipFile": _zip()}, Publish=True,
        )
        client.create_alias(FunctionName=FUNCTION, Name=ALIAS, FunctionVersion="1")
        yield client
    patch.undo()


@pytest.fixture
def lam(_fake_aws) -> StatefulLambda:
    return StatefulLambda(_fake_aws)  # fresh state every test


CAPS = {"min_concurrency": 0, "max_concurrency": 5}


def requested(lam: StatefulLambda) -> int:
    return lam.config["RequestedProvisionedConcurrentExecutions"] if lam.config else 0


# --- normal behaviour --------------------------------------------------------------------------


def test_requests_provisioned_concurrency_when_not_a_dry_run(lam) -> None:
    result = provision_lambda(lam, FUNCTION, ALIAS, 3, dry_run=False, **CAPS)

    assert (result.status, result.previous, result.requested) == ("applied", 0, 3)
    assert requested(lam) == 3
    assert "asynchronously" in result.detail  # the caller is told it is not READY yet


def test_dry_run_is_the_default_and_writes_nothing(lam) -> None:
    result = provision_lambda(lam, FUNCTION, ALIAS, 3, **CAPS)  # no dry_run argument

    assert result.status == "dry_run" and "would set 0 -> 3" in result.detail
    assert lam.calls == [] and lam.config is None


def test_dry_run_still_validates_against_the_real_function_and_alias(lam) -> None:
    with pytest.raises(ActuationRefused, match="does not exist"):
        provision_lambda(lam, FUNCTION, "no-such-alias", 2, dry_run=True, **CAPS)


def test_noop_when_already_at_the_requested_concurrency(lam) -> None:
    provision_lambda(lam, FUNCTION, ALIAS, 2, dry_run=False, **CAPS)
    lam.calls.clear()

    result = provision_lambda(lam, FUNCTION, ALIAS, 2, dry_run=False, **CAPS)

    assert result.status == "noop" and lam.calls == []


def test_noop_when_nothing_is_configured_and_zero_is_requested(lam) -> None:
    result = provision_lambda(lam, FUNCTION, ALIAS, 0, dry_run=False, **CAPS)

    assert result.status == "noop" and lam.calls == []


def test_zero_removes_the_configuration_instead_of_putting_zero(lam) -> None:
    provision_lambda(lam, FUNCTION, ALIAS, 2, dry_run=False, **CAPS)

    result = provision_lambda(lam, FUNCTION, ALIAS, 0, dry_run=False, **CAPS)

    assert result.status == "applied" and "removed" in result.detail
    assert lam.config is None and lam.calls[-1] == ("delete", None)


def test_can_scale_up_then_down(lam) -> None:
    provision_lambda(lam, FUNCTION, ALIAS, 4, dry_run=False, **CAPS)
    provision_lambda(lam, FUNCTION, ALIAS, 1, dry_run=False, **CAPS)

    assert requested(lam) == 1


def test_a_failed_configuration_is_re_requested_not_treated_as_done(lam) -> None:
    provision_lambda(lam, FUNCTION, ALIAS, 2, dry_run=False, **CAPS)
    lam.config["Status"] = "FAILED"
    lam.calls.clear()

    result = provision_lambda(lam, FUNCTION, ALIAS, 2, dry_run=False, **CAPS)

    assert result.status == "applied" and "retrying a FAILED" in result.detail
    assert lam.calls == [("put", 2)]


# --- the caps: refuse, never exceed, never clamp silently -----------------------------------


def test_refuses_above_the_configured_cap(lam) -> None:
    with pytest.raises(ActuationRefused, match="outside the configured caps"):
        provision_lambda(lam, FUNCTION, ALIAS, 6, dry_run=False, min_concurrency=0, max_concurrency=5)

    assert lam.calls == []


def test_refuses_below_the_configured_minimum(lam) -> None:
    with pytest.raises(ActuationRefused):
        provision_lambda(lam, FUNCTION, ALIAS, 0, dry_run=False, min_concurrency=1, max_concurrency=5)

    assert lam.calls == []


def test_a_cap_above_the_absolute_ceiling_is_rejected(lam) -> None:
    with pytest.raises(ActuationRefused, match="absolute ceiling"):
        provision_lambda(
            lam, FUNCTION, ALIAS, 3, dry_run=False, min_concurrency=0, max_concurrency=ABSOLUTE_MAX_LAMBDA_CONCURRENCY + 1
        )


@pytest.mark.parametrize("bad", [True, False, 2.0, "2", None])
def test_non_integer_requests_are_refused(lam, bad) -> None:
    with pytest.raises(ActuationRefused, match="integer"):
        provision_lambda(lam, FUNCTION, ALIAS, bad, dry_run=False, **CAPS)


@pytest.mark.parametrize("alias", ["$LATEST", ""])
def test_latest_and_empty_qualifiers_are_refused(lam, alias) -> None:
    with pytest.raises(ActuationRefused, match="named alias"):
        provision_lambda(lam, FUNCTION, alias, 2, dry_run=False, **CAPS)


def test_invalid_cap_configuration_is_refused(lam) -> None:
    with pytest.raises(ActuationRefused, match="invalid caps"):
        provision_lambda(lam, FUNCTION, ALIAS, 2, dry_run=False, min_concurrency=4, max_concurrency=2)


def test_unknown_alias_is_refused_not_created(lam) -> None:
    with pytest.raises(ActuationRefused, match="does not exist"):
        provision_lambda(lam, FUNCTION, "typo-alias", 2, dry_run=False, **CAPS)

    assert lam.calls == [] and lam.config is None


def test_a_refused_request_never_reaches_a_write(lam) -> None:
    for bad in (-1, 6, 10_000):
        with pytest.raises(ActuationRefused):
            provision_lambda(lam, FUNCTION, ALIAS, bad, dry_run=False, **CAPS)

    assert lam.calls == []


def test_the_cap_holds_for_any_request_and_any_caps(lam) -> None:
    rng = random.Random(11)
    for _ in range(150):
        caps = {"min_concurrency": rng.randint(0, 2), "max_concurrency": rng.randint(2, ABSOLUTE_MAX_LAMBDA_CONCURRENCY)}
        desired = rng.randint(-3, 15)
        before = requested(lam)
        try:
            provision_lambda(lam, FUNCTION, ALIAS, desired, dry_run=False, **caps)
        except ActuationRefused:
            assert requested(lam) == before  # a refusal never changes anything
        else:
            assert requested(lam) == desired
            assert caps["min_concurrency"] <= requested(lam) <= caps["max_concurrency"]


# --- policy -> actuator ------------------------------------------------------------------------


def policy_decision(p90_level: float, config: LambdaPolicyConfig) -> ScalingDecision:
    n = 15
    forecast = Forecast(
        "thumbnail_service", datetime(2026, 6, 1, tzinfo=timezone.utc),
        tuple([0.0] * n), tuple([p90_level] * n), tuple([p90_level] * n),
    )
    return lambda_provisioned_concurrency(forecast, 0, config, Aggressiveness.BALANCED)


def test_a_policy_decision_is_applied_with_the_configs_caps(lam) -> None:
    config = LambdaPolicyConfig(avg_duration_seconds=1.0, max_concurrency=10)
    decision = policy_decision(600.0, config)  # 600/60 * 1.0 * 1.5 = 15 -> capped to 10

    result = apply_lambda_decision(lam, FUNCTION, ALIAS, decision, config, dry_run=False)

    assert result.status == "applied" and requested(lam) == 10


def test_even_an_absurd_forecast_cannot_push_past_the_cap(lam) -> None:
    config = LambdaPolicyConfig(max_concurrency=3)

    apply_lambda_decision(lam, FUNCTION, ALIAS, policy_decision(1e12, config), config, dry_run=False)

    assert requested(lam) == 3


def test_a_decision_over_the_cap_from_a_buggy_caller_is_refused(lam) -> None:
    config = LambdaPolicyConfig(max_concurrency=3)
    forged = ScalingDecision(
        "thumbnail_service", "lambda_provisioned_concurrency", desired=9, uncapped=9, cap_applied=False,
        current=0, quantile="p90", demand=1.0, reason="bug",
    )

    with pytest.raises(ActuationRefused):
        apply_lambda_decision(lam, FUNCTION, ALIAS, forged, config, dry_run=False)

    assert lam.calls == []


def test_an_ec2_decision_is_not_applied_to_a_lambda(lam) -> None:
    wrong = ScalingDecision(
        "catalog_service", "ec2_asg", desired=2, uncapped=2, cap_applied=False,
        current=1, quantile="p90", demand=1.0, reason="x",
    )

    with pytest.raises(ActuationRefused, match="not 'ec2_asg'"):
        apply_lambda_decision(lam, FUNCTION, ALIAS, wrong, LambdaPolicyConfig(), dry_run=False)


def test_apply_lambda_decision_defaults_to_dry_run(lam) -> None:
    config = LambdaPolicyConfig(avg_duration_seconds=1.0, max_concurrency=10)

    result = apply_lambda_decision(lam, FUNCTION, ALIAS, policy_decision(600.0, config), config)

    assert result.status == "dry_run" and lam.calls == []


# --- reading status ----------------------------------------------------------------------------


def test_provisioned_status_is_none_when_nothing_is_configured(lam) -> None:
    assert provisioned_status(lam, FUNCTION, ALIAS) is None


def test_provisioned_status_reports_the_asynchronous_allocation(lam) -> None:
    provision_lambda(lam, FUNCTION, ALIAS, 2, dry_run=False, **CAPS)

    status = provisioned_status(lam, FUNCTION, ALIAS)

    assert (status.requested, status.status, status.available) == (2, "IN_PROGRESS", 0)
    lam.config.update({"Status": "READY", "AvailableProvisionedConcurrentExecutions": 2})
    assert provisioned_status(lam, FUNCTION, ALIAS).status == "READY"


def test_other_errors_are_not_mistaken_for_no_configuration(lam, monkeypatch) -> None:
    def denied(**_kw):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetProvisionedConcurrencyConfig")

    monkeypatch.setattr(lam, "get_provisioned_concurrency_config", denied)

    with pytest.raises(ClientError):
        provisioned_status(lam, FUNCTION, ALIAS)


# --- consistency and hygiene -------------------------------------------------------------------


def test_the_no_config_error_code_matches_what_the_teardown_verifier_expects() -> None:
    # Two modules must agree on AWS's "no configuration" code, or one would misread the other's state.
    assert actuator.NO_CONFIG_CODE in verifier._NO_PC_CODES


def test_the_module_creates_no_clients_of_its_own() -> None:
    source = Path(actuator.__file__).read_text(encoding="utf-8")

    assert "import boto3" not in source and "boto3." not in source
