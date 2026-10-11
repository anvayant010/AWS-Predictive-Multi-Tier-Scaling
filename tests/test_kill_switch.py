import shutil
import subprocess
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from moto import mock_aws

import decision_engine.kill_switch as ks
from decision_engine.kill_switch import (
    DEFAULT_PARAMETER,
    ENGAGED,
    RELEASED,
    engage,
    main,
    read_kill_switch,
    release,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def ssm(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        yield boto3.Session(region_name="us-east-1").client("ssm")


def put(ssm, value: str, name: str = DEFAULT_PARAMETER) -> None:
    ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)


# --- fail closed: only an explicit RELEASED allows actuation -----------------------------------


def test_released_is_the_only_state_that_allows_actuation(ssm) -> None:
    put(ssm, RELEASED)

    status = read_kill_switch(ssm)

    assert status.engaged is False and status.value == RELEASED
    assert status.last_modified is not None


def test_engaged_value_blocks_actuation(ssm) -> None:
    put(ssm, ENGAGED)

    assert read_kill_switch(ssm).engaged is True


def test_a_missing_parameter_blocks_actuation(ssm) -> None:
    status = read_kill_switch(ssm)

    assert status.engaged is True
    assert "does not exist" in status.reason and "fail closed" in status.reason


@pytest.mark.parametrize("value", ["off", "false", "0", "no", "disabled", "released!", "RELEASE", "x"])
def test_anything_other_than_released_blocks_actuation(ssm, value) -> None:
    put(ssm, value)

    assert read_kill_switch(ssm).engaged is True


@pytest.mark.parametrize("value", ["RELEASED", "released", "  Released  "])
def test_case_and_whitespace_do_not_matter_for_released(ssm, value) -> None:
    put(ssm, value)

    assert read_kill_switch(ssm).engaged is False


def test_a_read_error_blocks_actuation(ssm, monkeypatch) -> None:
    def denied(**_kw):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetParameter")

    monkeypatch.setattr(ssm, "get_parameter", denied)

    status = read_kill_switch(ssm)

    assert status.engaged is True and "AccessDeniedException" in status.reason


def test_missing_credentials_block_actuation(ssm, monkeypatch) -> None:
    monkeypatch.setattr(ssm, "get_parameter", lambda **_kw: (_ for _ in ()).throw(NoCredentialsError()))

    status = read_kill_switch(ssm)

    assert status.engaged is True and "NoCredentialsError" in status.reason


def test_programming_errors_are_not_swallowed_by_the_safe_default(ssm, monkeypatch) -> None:
    monkeypatch.setattr(ssm, "get_parameter", lambda **_kw: (_ for _ in ()).throw(TypeError("bug")))

    with pytest.raises(TypeError):
        read_kill_switch(ssm)


def test_the_switch_is_read_fresh_every_time(ssm) -> None:
    put(ssm, RELEASED)
    assert read_kill_switch(ssm).engaged is False

    put(ssm, ENGAGED)

    assert read_kill_switch(ssm).engaged is True


def test_a_custom_parameter_name_is_used(ssm) -> None:
    put(ssm, RELEASED, name="/predictive-scaling/other")
    put(ssm, ENGAGED)

    assert read_kill_switch(ssm, "/predictive-scaling/other").engaged is False
    assert read_kill_switch(ssm).engaged is True


# --- flipping it -------------------------------------------------------------------------------


def test_engage_creates_the_parameter_in_the_safe_state(ssm) -> None:
    engage(ssm)

    assert ssm.get_parameter(Name=DEFAULT_PARAMETER)["Parameter"]["Value"] == ENGAGED


def test_release_then_engage_round_trips(ssm) -> None:
    release(ssm)
    assert read_kill_switch(ssm).engaged is False

    engage(ssm)

    assert read_kill_switch(ssm).engaged is True


def test_the_parameter_lives_where_the_scoped_iam_user_may_write() -> None:
    # bootstrap IAM allows ssm parameter access only under /predictive-scaling/*
    assert DEFAULT_PARAMETER.startswith("/predictive-scaling/")


# --- CLI ---------------------------------------------------------------------------------------


def test_cli_engage_and_status(ssm, capsys) -> None:
    assert main(["engage"], ssm=ssm) == 0
    assert "ENGAGED (actuation blocked)" in capsys.readouterr().out

    assert main(["status"], ssm=ssm) == 0
    assert "ENGAGED" in capsys.readouterr().out


def test_cli_refuses_to_release_without_yes_and_changes_nothing(ssm, capsys) -> None:
    engage(ssm)

    code = main(["release"], ssm=ssm)

    assert code == 2 and "Refusing to release without --yes" in capsys.readouterr().out
    assert read_kill_switch(ssm).engaged is True


def test_cli_release_with_yes_allows_actuation(ssm, capsys) -> None:
    assert main(["release", "--yes"], ssm=ssm) == 0

    assert "RELEASED (actuation allowed)" in capsys.readouterr().out
    assert read_kill_switch(ssm).engaged is False


def test_cli_rejects_unknown_commands(ssm) -> None:
    with pytest.raises(SystemExit):
        main(["explode"], ssm=ssm)


def test_cli_status_on_a_fresh_account_reports_engaged(ssm, capsys) -> None:
    main(["status"], ssm=ssm)

    assert "ENGAGED" in capsys.readouterr().out


def test_the_library_functions_create_no_clients_of_their_own() -> None:
    source = Path(ks.__file__).read_text(encoding="utf-8")
    top_level = source.split("def main(")[0]

    assert "import boto3" not in top_level and "boto3." not in top_level


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_has_kill_switch_shortcuts() -> None:
    kill = subprocess.run(["make", "-n", "kill"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    status = subprocess.run(["make", "-n", "kill-status"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout

    assert "decision_engine.kill_switch engage" in kill
    assert "decision_engine.kill_switch status" in status
    assert "release" not in kill  # the panic button can only ever engage
