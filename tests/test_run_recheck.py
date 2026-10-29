import boto3
import pytest

import scripts.run_recheck as rr
from comparison.calibration_session import SessionDeps


def fake_deps(**over):
    base = dict(preflight=lambda: [], apply=lambda: {}, make_env=lambda o: None, wait_healthy=lambda e: None, aws=None,
                outputs_to_names=lambda o: {}, destroy=lambda: True, verify=lambda: 0)
    base.update(over)
    return SessionDeps(**base)


def test_without_confirm_it_only_prints_the_plan_and_never_touches_aws(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(boto3, "Session", lambda *a, **k: pytest.fail("the plan-only path must not create an AWS session"))
    monkeypatch.setattr(rr, "run_recheck", lambda *a, **k: pytest.fail("the plan-only path must not run the re-test"))

    code = rr.main(["--out", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0 and "CONNECTION RE-TEST PLAN" in out and "Plan only" in out and "--confirm" in out
    assert not list(tmp_path.iterdir())  # not even an output directory is created by a plan


def test_the_defaults_match_the_agreed_plan() -> None:
    cfg = rr.config_from_args(rr.parse_args([]))

    assert cfg.max_minutes == 60.0 and cfg.measure_s == 150.0 and cfg.settle_s == 30.0
    assert cfg.out_dir.parent.name == "calibration_output" and cfg.out_dir.name.startswith("recheck-")


def test_the_plan_option_selects_the_knee_hunt_and_the_default_stays_the_connection_retest() -> None:
    from comparison.recheck import DEFAULT_PLAN, KNEE_PLAN

    default = rr.config_from_args(rr.parse_args([]))
    knee = rr.config_from_args(rr.parse_args(["--plan", "knee"]))

    assert default.plan == DEFAULT_PLAN and knee.plan == KNEE_PLAN
    assert knee.out_dir.name.startswith("knee-") and default.out_dir.name.startswith("recheck-")
    with pytest.raises(SystemExit):
        rr.parse_args(["--plan", "bogus"])


def test_command_line_overrides_flow_into_the_configuration(tmp_path) -> None:
    cfg = rr.config_from_args(rr.parse_args(["--out", str(tmp_path), "--max-minutes", "30", "--measure-s", "60"]))

    assert cfg.max_minutes == 30 and cfg.measure_s == 60 and cfg.out_dir == tmp_path


def test_confirm_is_off_unless_explicitly_given() -> None:
    assert rr.parse_args([]).confirm is False and rr.parse_args(["--confirm"]).confirm is True


def test_the_confirmed_path_uses_the_scoped_profile_and_exits_nonzero_if_teardown_is_not_clean(monkeypatch, tmp_path) -> None:
    seen = {}

    class FakeSession:
        def __init__(self, profile_name=None, region_name=None):
            seen["profile"], seen["region"] = profile_name, region_name

    monkeypatch.setattr(boto3, "Session", FakeSession)
    monkeypatch.setattr(rr, "build_deps", lambda session: fake_deps())
    monkeypatch.setattr(rr, "run_recheck", lambda cfg, deps: {"status": "measured", "errors": {}, "teardown": {"clean": False, "destroyed": False, "verifier_exit": 1}, "minutes": 1.0})

    code = rr.main(["--confirm", "--out", str(tmp_path)])

    assert seen == {"profile": "predictive-scaling", "region": "us-east-1"} and code == 4


def test_a_preflight_abort_has_its_own_exit_code(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(boto3, "Session", lambda *a, **k: object())
    monkeypatch.setattr(rr, "build_deps", lambda session: fake_deps(preflight=lambda: ["x"]))
    monkeypatch.setattr(rr, "run_recheck", lambda cfg, deps: {"status": "aborted_preflight", "errors": {}, "teardown": None})

    assert rr.main(["--confirm", "--out", str(tmp_path)]) == 3


def test_a_clean_run_exits_zero_and_gets_a_file_saver(monkeypatch, tmp_path) -> None:
    seen = {}
    monkeypatch.setattr(boto3, "Session", lambda *a, **k: object())
    monkeypatch.setattr(rr, "build_deps", lambda session: fake_deps())

    def fake_run(cfg, deps):
        deps.save("probe", {"ok": 1})
        seen["saved"] = (tmp_path / "probe.json").exists()
        return {"status": "measured", "errors": {}, "teardown": {"clean": True, "destroyed": True, "verifier_exit": 0}, "minutes": 1.0}

    monkeypatch.setattr(rr, "run_recheck", fake_run)

    assert rr.main(["--confirm", "--out", str(tmp_path)]) == 0 and seen["saved"] is True


def test_the_re_test_reuses_the_calibration_preflight_and_teardown_wiring() -> None:
    import scripts.run_calibration as rc

    assert rr.build_deps is rc.build_deps and rr.PROFILE == rc.PROFILE == "predictive-scaling"
