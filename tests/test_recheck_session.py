from pathlib import Path
from types import SimpleNamespace

import pytest

import comparison.calibration_stages as cs
import comparison.recheck as rc
import comparison.recheck_session as sess
import comparison.recheck_stages as rs
from comparison.calibration_session import SessionDeps
from comparison.recheck import RecheckCell, cell_record
from comparison.recheck_session import RecheckConfig, analyze_recheck, expected_minutes, format_plan, format_result, run_recheck, spend_estimate

NAMES = {"asg_name": "asg", "target_group_arn": "tg", "function_name": "fn", "alias": "live", "log_group": "/aws/lambda/fn"}


class Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.saved: dict = {}
        self.logs: list[str] = []


def make_deps(rec: Recorder, *, problems=None, destroy_ok=True, verify_code=0, apply_raises=None, wait_raises=None) -> SessionDeps:
    def apply():
        rec.events.append("apply")
        if apply_raises:
            raise apply_raises
        return {"x": 1}

    def wait_healthy(env):
        rec.events.append("healthy")
        if wait_raises:
            raise wait_raises

    def destroy():
        rec.events.append("destroy")
        return destroy_ok

    def verify():
        rec.events.append("verify")
        return verify_code

    return SessionDeps(
        preflight=lambda: list(problems or []), apply=apply,
        make_env=lambda o: cs.StageEnv(catalog_url="http://c", thumbnail_url="http://t", http_get=lambda u: cs.HttpResult(200, 1.0, None)),
        wait_healthy=wait_healthy, aws=SimpleNamespace(autoscaling=None, elbv2=None, lambda_=None, logs=None, cloudwatch=None),
        outputs_to_names=lambda o: NAMES, destroy=destroy, verify=verify,
        save=lambda name, obj: rec.saved.__setitem__(name, obj), log=rec.logs.append,
    )


def good_rows(slow: int = 0, n: int = 500) -> list[dict]:
    """n requests in a 6 s window = 5000 rpm delivered; `slow` of them took 3.5 s."""
    return [{"ms": 500.0, "ok": True} for _ in range(n - slow)] + [{"ms": 3500.0, "ok": True} for _ in range(slow)]


def canned_stage_result() -> dict:
    return {
        "cells": [
            cell_record("per_request", 1, good_rows(40), 5000.0, 6.0, connects_per_request=1.0),
            cell_record("keep_alive", 1, good_rows(0), 5000.0, 6.0, connects_per_request=0.01),
            cell_record("per_request", 2, good_rows(35), 5000.0, 6.0, connects_per_request=1.0),
        ],
        "raw": [], "skipped": [],
    }


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(rs, "run_connection_recheck", lambda env, *a, **k: canned_stage_result())
    monkeypatch.setattr(sess, "fetch_asg_cpu", lambda *a, **k: [{"time": "t", "average": 10.0, "maximum": 33.0}])
    monkeypatch.setattr(cs, "remove_provisioned_concurrency", lambda *a, **k: True)


def cfg(tmp_path: Path, **over) -> RecheckConfig:
    base = dict(out_dir=tmp_path, max_minutes=5.0)
    base.update(over)
    return RecheckConfig(**base)


# --- configuration and plan ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("over", [dict(max_minutes=0), dict(plan=()), dict(measure_s=0), dict(settle_s=-1), dict(setup_minutes=-1)])
def test_nonsense_configuration_is_refused(tmp_path, over) -> None:
    with pytest.raises(ValueError):
        cfg(tmp_path, **over)


def test_expected_duration_counts_the_steps_and_the_one_boot_of_the_default_plan(tmp_path) -> None:
    m = expected_minutes(RecheckConfig(out_dir=tmp_path))

    assert m["steps"] == 10 and m["measuring_minutes"] == pytest.approx(10 * 3.0 + 3.0) and m["total_stack_minutes"] == pytest.approx(33.0 + 12.0)


def test_every_change_of_instance_count_costs_a_boot(tmp_path) -> None:
    plan = (RecheckCell("keep_alive", 1, (100.0,)), RecheckCell("keep_alive", 2, (100.0,)), RecheckCell("keep_alive", 1, (100.0,)))

    assert expected_minutes(cfg(tmp_path, plan=plan))["measuring_minutes"] == pytest.approx(3 * 3.0 + 2 * 3.0)


def test_the_spend_estimate_is_small_and_uses_the_time_limit(tmp_path) -> None:
    est = spend_estimate(RecheckConfig(out_dir=tmp_path))

    assert est["stack_hours"] == pytest.approx((60 + 12) / 60) and 0 < est["total_usd"] < 1.0


def test_the_plan_lists_every_block_and_states_the_caps_the_limit_and_the_kill_switch(tmp_path) -> None:
    text = format_plan(RecheckConfig(out_dir=tmp_path))

    for needle in ("nothing has been applied", "never above 2", "HARD LIMIT", "kill switch", "upper bound", "reused connections", "new connection per request",
                   "1 instance(s)", "2 instance(s)", "400, 4000, 5000, 6500, 8000"):
        assert needle in text


# --- the safety properties ----------------------------------------------------------------------------------------------------


def test_a_preflight_problem_means_nothing_is_applied_or_torn_down(tmp_path, stub) -> None:
    rec = Recorder()

    result = run_recheck(cfg(tmp_path), make_deps(rec, problems=["state is not empty"]))

    assert result["status"] == "aborted_preflight" and rec.events == []


def test_a_normal_run_applies_measures_then_destroys_and_verifies_and_saves_everything(tmp_path, stub) -> None:
    rec = Recorder()

    result = run_recheck(cfg(tmp_path), make_deps(rec))

    assert rec.events == ["apply", "healthy", "destroy", "verify"]
    assert result["status"] == "measured" and result["errors"] == {} and result["teardown"]["clean"]
    assert set(rec.saved) == {"recheck", "cpu", "recheck_analysis", "session"}
    assert result["analysis"]["verdict"]["conclusion"] == "connection_setup"


@pytest.mark.parametrize("failing", ["apply_raises", "wait_raises"])
def test_teardown_runs_even_if_apply_or_the_health_wait_blows_up(tmp_path, stub, failing) -> None:
    rec = Recorder()

    result = run_recheck(cfg(tmp_path), make_deps(rec, **{failing: RuntimeError("boom")}))

    assert result["status"] == "crashed" and "boom" in result["errors"]["session"]
    assert rec.events[-2:] == ["destroy", "verify"]


def test_a_crashing_stage_is_recorded_and_the_stack_is_still_torn_down(tmp_path, monkeypatch) -> None:
    rec = Recorder()
    monkeypatch.setattr(rs, "run_connection_recheck", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stage blew up")))
    monkeypatch.setattr(sess, "fetch_asg_cpu", lambda *a, **k: [])
    monkeypatch.setattr(cs, "remove_provisioned_concurrency", lambda *a, **k: True)

    result = run_recheck(cfg(tmp_path), make_deps(rec))

    assert "stage blew up" in result["errors"]["recheck"] and "analysis" not in result
    assert rec.events[-2:] == ["destroy", "verify"] and result["teardown"]["clean"]


def test_ctrl_c_during_the_stage_still_tears_the_stack_down(tmp_path, monkeypatch) -> None:
    rec = Recorder()

    def interrupted(*a, **k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(rs, "run_connection_recheck", interrupted)
    monkeypatch.setattr(cs, "remove_provisioned_concurrency", lambda *a, **k: True)

    result = run_recheck(cfg(tmp_path), make_deps(rec))

    assert result["status"] == "crashed" and "KeyboardInterrupt" in result["errors"]["session"]
    assert rec.events[-2:] == ["destroy", "verify"]


def test_the_time_limit_stops_the_stage_and_the_teardown_still_runs(tmp_path, monkeypatch) -> None:
    rec = Recorder()
    seen = {}

    def waits_for_the_limit(env, *a, **k):
        seen["stopped"] = env.stop.wait(10)
        return {"cells": [], "raw": [], "skipped": []}

    monkeypatch.setattr(rs, "run_connection_recheck", waits_for_the_limit)
    monkeypatch.setattr(sess, "fetch_asg_cpu", lambda *a, **k: [])
    monkeypatch.setattr(cs, "remove_provisioned_concurrency", lambda *a, **k: True)

    result = run_recheck(cfg(tmp_path, max_minutes=0.001), make_deps(rec))  # 60 ms

    assert seen["stopped"] is True and result["status"] == "stopped_early"
    assert rec.events[-2:] == ["destroy", "verify"]


@pytest.mark.parametrize("kw", [dict(destroy_ok=False), dict(verify_code=1), dict(verify_code=2)])
def test_a_teardown_that_cannot_be_confirmed_clean_is_reported_loudly(tmp_path, stub, kw) -> None:
    rec = Recorder()

    result = run_recheck(cfg(tmp_path), make_deps(rec, **kw))

    assert result["teardown"]["clean"] is False and any("TEARDOWN NOT CONFIRMED CLEAN" in line for line in rec.logs)


def test_a_leftover_provisioned_concurrency_config_is_removed_before_destroy(tmp_path, monkeypatch) -> None:
    rec = Recorder()
    monkeypatch.setattr(rs, "run_connection_recheck", lambda env, *a, **k: canned_stage_result())
    monkeypatch.setattr(sess, "fetch_asg_cpu", lambda *a, **k: [])
    monkeypatch.setattr(cs, "remove_provisioned_concurrency", lambda *a, **k: rec.events.append("remove_pc") or True)

    run_recheck(cfg(tmp_path), make_deps(rec))

    assert rec.events == ["apply", "healthy", "remove_pc", "destroy", "verify"]


def test_the_stage_gets_the_plan_the_asg_and_the_step_lengths(tmp_path, monkeypatch) -> None:
    rec = Recorder()
    got = {}

    def capture(env, autoscaling, elbv2, asg_name, tg_arn, plan, **kw):
        got.update(asg=asg_name, tg=tg_arn, plan=plan, **kw)
        return {"cells": [], "raw": [], "skipped": []}

    monkeypatch.setattr(rs, "run_connection_recheck", capture)
    monkeypatch.setattr(sess, "fetch_asg_cpu", lambda *a, **k: [])
    monkeypatch.setattr(cs, "remove_provisioned_concurrency", lambda *a, **k: True)
    plan = (RecheckCell("keep_alive", 1, (123.0,)),)

    run_recheck(cfg(tmp_path, plan=plan, settle_s=7.0, measure_s=11.0), make_deps(rec))

    assert got == {"asg": "asg", "tg": "tg", "plan": plan, "settle_s": 7.0, "measure_s": 11.0}


# --- analysis and reporting ---------------------------------------------------------------------------------------------------


def test_the_analysis_carries_the_verdict_the_cpu_peak_and_any_steps_the_laptop_could_not_deliver() -> None:
    stage = canned_stage_result()
    stage["cells"].append(cell_record("keep_alive", 1, good_rows(0)[:10], 8000.0, 6.0, connects_per_request=0.01))  # 10 requests in 6 s of 8000 rpm

    a = analyze_recheck({"recheck": stage, "cpu": [{"maximum": 20.0}, {"maximum": 41.0}]})

    assert a["verdict"]["conclusion"] == "connection_setup" and a["cpu_max_percent"] == 41.0
    assert a["generator_limited_steps"] == [("keep_alive", 1, 8000.0)]
    assert a["keep_alive_knee"]["available"] is True


def test_the_analysis_copes_with_no_data_at_all() -> None:
    a = analyze_recheck({})

    assert a["cells"] == [] and a["verdict"]["conclusion"] == "incomplete" and a["cpu_max_percent"] is None


def test_the_result_text_shows_the_table_the_conclusion_and_the_teardown_state() -> None:
    stage = canned_stage_result()
    stage["cells"].insert(0, cell_record("keep_alive", 1, good_rows(0, n=40), 400.0, 6.0, connects_per_request=0.05))  # the baseline step
    result = {"status": "measured", "minutes": 44.0, "errors": {}, "teardown": {"destroyed": True, "verifier_exit": 0, "clean": True},
              "analysis": analyze_recheck({"recheck": stage, "cpu": []})}

    text = format_result(result)

    for needle in ("status: measured", "clean=True", "per_request", "keep_alive", "conclusion: connection_setup", "5000", "reused-connection knee"):
        assert needle in text


def test_a_plan_without_a_new_connection_step_has_no_verdict_but_still_reports_the_knee() -> None:
    stage = canned_stage_result()
    stage["cells"] = [c for c in stage["cells"] if c["mode"] == "keep_alive"]
    stage["cells"].insert(0, cell_record("keep_alive", 1, good_rows(0, n=40), 400.0, 6.0, connects_per_request=0.05))
    a = analyze_recheck({"recheck": stage, "cpu": []}, rc.KNEE_PLAN)

    assert a["verdict"] is None and a["keep_alive_knee"]["available"] is True
    text = format_result({"status": "measured", "errors": {}, "teardown": None, "analysis": a})
    assert "conclusion:" not in text and "reused-connection knee" in text


def test_the_default_plan_still_gets_a_verdict_even_with_no_data() -> None:
    assert analyze_recheck({}, rc.DEFAULT_PLAN)["verdict"]["conclusion"] == "incomplete"
    assert analyze_recheck({}, rc.KNEE_PLAN)["verdict"] is None


def test_the_knee_plan_is_one_instance_reused_connections_with_a_low_baseline_and_rising_rates() -> None:
    (block,) = rc.KNEE_PLAN

    assert block.mode == "keep_alive" and block.instances == 1
    assert block.rates[0] <= 500 and list(block.rates) == sorted(block.rates) and block.rates[-1] > 8000
    assert expected_minutes(RecheckConfig(out_dir=Path("x"), plan=rc.KNEE_PLAN))["steps"] == len(block.rates)


def test_the_plan_text_only_mentions_a_second_instance_when_the_plan_uses_one(tmp_path) -> None:
    assert "stays at 1 instance" in format_plan(cfg(tmp_path, plan=rc.KNEE_PLAN))
    assert "1 -> 2" in format_plan(cfg(tmp_path, plan=rc.DEFAULT_PLAN))


def test_the_session_passes_its_plan_to_the_analysis(tmp_path, monkeypatch) -> None:
    rec = Recorder()
    monkeypatch.setattr(rs, "run_connection_recheck", lambda *a, **k: {"cells": [], "raw": [], "skipped": []})
    monkeypatch.setattr(sess, "fetch_asg_cpu", lambda *a, **k: [])
    monkeypatch.setattr(cs, "remove_provisioned_concurrency", lambda *a, **k: True)

    result = run_recheck(cfg(tmp_path, plan=rc.KNEE_PLAN), make_deps(rec))

    assert result["analysis"]["verdict"] is None


def test_the_result_text_flags_skipped_blocks_and_stage_errors() -> None:
    analysis = analyze_recheck({"recheck": {"cells": [], "raw": [], "skipped": [{"mode": "keep_alive", "instances": 2, "reason": "x"}]}})

    text = format_result({"status": "measured", "errors": {"cpu": "no data"}, "teardown": None, "analysis": analysis})

    assert "stage error - cpu: no data" in text and "skipped block" in text
