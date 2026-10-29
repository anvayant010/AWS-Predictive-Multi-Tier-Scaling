import boto3
import pytest

import scripts.run_calibration as rc


def test_without_confirm_it_only_prints_the_plan_and_never_touches_aws(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(boto3, "Session", lambda *a, **k: pytest.fail("the plan-only path must not create an AWS session"))
    monkeypatch.setattr(rc.subprocess, "run", lambda *a, **k: pytest.fail("the plan-only path must not run terraform"))

    code = rc.main(["--out", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0 and "CALIBRATION SESSION PLAN" in out and "Plan only" in out and "--confirm" in out
    assert not list(tmp_path.iterdir())  # not even an output directory is created by a plan


def test_the_defaults_match_the_agreed_plan() -> None:
    cfg = rc.config_from_args(rc.parse_args([]))

    assert cfg.max_minutes == 150.0 and cfg.boot_repeats == 3 and cfg.provisioned_repeats == 3
    assert cfg.idle_gaps == (3, 6, 10, 15, 20, 30) and cfg.rates[0] == 200 and cfg.rates[-1] == 5000


def test_command_line_overrides_flow_into_the_configuration(tmp_path) -> None:
    cfg = rc.config_from_args(rc.parse_args(["--out", str(tmp_path), "--rates", "100,200", "--idle-gaps", "2,4", "--max-minutes", "60", "--boot-repeats", "1"]))

    assert cfg.rates == (100.0, 200.0) and cfg.idle_gaps == (2.0, 4.0) and cfg.max_minutes == 60 and cfg.boot_repeats == 1 and cfg.out_dir == tmp_path


def test_confirm_is_off_unless_explicitly_given() -> None:
    assert rc.parse_args([]).confirm is False and rc.parse_args(["--confirm"]).confirm is True


def test_the_confirmed_path_uses_the_scoped_profile_and_exits_nonzero_if_teardown_is_not_clean(monkeypatch, tmp_path) -> None:
    seen = {}

    class FakeSession:
        def __init__(self, profile_name=None, region_name=None):
            seen["profile"] = profile_name

    monkeypatch.setattr(boto3, "Session", FakeSession)
    monkeypatch.setattr(rc, "build_deps", lambda session: rc.SessionDeps(
        preflight=lambda: [], apply=lambda: {}, make_env=lambda o: None, wait_healthy=lambda e: None, aws=None,
        outputs_to_names=lambda o: {}, destroy=lambda: True, verify=lambda: 0))
    monkeypatch.setattr(rc, "run_session", lambda cfg, deps: {"status": "measured", "errors": {}, "teardown": {"clean": False, "destroyed": False, "verifier_exit": 1}, "minutes": 1.0})

    code = rc.main(["--confirm", "--out", str(tmp_path)])

    assert seen["profile"] == "predictive-scaling" and code == 4


def test_a_preflight_abort_has_its_own_exit_code(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(boto3, "Session", lambda *a, **k: object())
    monkeypatch.setattr(rc, "build_deps", lambda session: rc.SessionDeps(
        preflight=lambda: ["x"], apply=lambda: {}, make_env=lambda o: None, wait_healthy=lambda e: None, aws=None,
        outputs_to_names=lambda o: {}, destroy=lambda: True, verify=lambda: 0))
    monkeypatch.setattr(rc, "run_session", lambda cfg, deps: {"status": "aborted_preflight", "errors": {}, "teardown": None})

    assert rc.main(["--confirm", "--out", str(tmp_path)]) == 3


def test_a_clean_session_exits_zero(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(boto3, "Session", lambda *a, **k: object())
    monkeypatch.setattr(rc, "build_deps", lambda session: rc.SessionDeps(
        preflight=lambda: [], apply=lambda: {}, make_env=lambda o: None, wait_healthy=lambda e: None, aws=None,
        outputs_to_names=lambda o: {}, destroy=lambda: True, verify=lambda: 0))
    monkeypatch.setattr(rc, "run_session", lambda cfg, deps: {"status": "measured", "errors": {}, "teardown": {"clean": True, "destroyed": True, "verifier_exit": 0}, "minutes": 1.0})

    assert rc.main(["--confirm", "--out", str(tmp_path)]) == 0
