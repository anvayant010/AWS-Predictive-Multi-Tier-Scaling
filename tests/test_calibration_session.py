import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import comparison.calibration_session as sess
import comparison.calibration_stages as cs
from comparison.calibration import IdleProbe, LoadStep
from comparison.calibration_session import (
    SessionConfig,
    SessionDeps,
    analyze_session,
    expected_minutes,
    format_plan,
    format_result,
    make_file_saver,
    run_session,
    spend_estimate,
)

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

    def make_env(outputs):
        return cs.StageEnv(catalog_url="http://c", thumbnail_url="http://t", http_get=lambda u: cs.HttpResult(200, 1.0, None))

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
        preflight=lambda: list(problems or []), apply=apply, make_env=make_env, wait_healthy=wait_healthy,
        aws=SimpleNamespace(autoscaling=None, elbv2=None, lambda_=None, logs=None, cloudwatch=None),
        outputs_to_names=lambda o: NAMES, destroy=destroy, verify=verify,
        save=lambda name, obj: rec.saved.__setitem__(name, obj), log=rec.logs.append,
    )


def fast_cfg(tmp_path: Path, **over) -> SessionConfig:
    base = dict(out_dir=tmp_path, max_minutes=5.0, rates=(200, 400), idle_gaps=(3, 6), boot_repeats=1, provisioned_repeats=1)
    base.update(over)
    return SessionConfig(**base)


@pytest.fixture
def stub_stages(monkeypatch):
    """Replace every stage with an instant canned one; individual tests override what they need."""
    steps = [LoadStep(200, 200, 0, 50.0, 100.0, 150.0, 60.0), LoadStep(400, 400, 0, 55.0, 150.0, 200.0, 60.0), LoadStep(800, 800, 0, 60.0, 500.0, 900.0, 60.0)]
    monkeypatch.setattr(cs, "run_knee", lambda env, rates, **k: {"steps": steps, "windows": [], "raw": []})
    monkeypatch.setattr(cs, "run_boot_timing", lambda *a, **k: {"seconds": [95.0, 97.0], "timeouts": 0})
    monkeypatch.setattr(cs, "fetch_asg_cpu", lambda *a, **k: [{"time": "t", "average": 30.0, "maximum": 60.0}])
    monkeypatch.setattr(cs, "run_cold_start_probe", lambda env, **k: [{"ms": 800.0, "ok": True, "cold": True}, {"ms": 60.0, "ok": True, "cold": False}])
    monkeypatch.setattr(
        cs, "run_idle_staircase",
        lambda env, gaps, **k: {"probes": [IdleProbe(3, frozenset("ab"), frozenset("ab")), IdleProbe(6, frozenset("ab"), frozenset("xy"))], "rows": []},
    )
    monkeypatch.setattr(cs, "fetch_report_messages", lambda *a, **k: [
        "REPORT RequestId: 1\tDuration: 60.0 ms\tBilled Duration: 61 ms\tMemory Size: 128 MB\tMax Memory Used: 50 MB\tInit Duration: 200.0 ms",
        "REPORT RequestId: 2\tDuration: 55.0 ms\tBilled Duration: 56 ms\tMemory Size: 128 MB\tMax Memory Used: 50 MB",
    ])
    monkeypatch.setattr(cs, "run_provisioned_timing", lambda *a, **k: {"seconds": [160.0, 170.0], "timeouts": 0,
                        "serve_checks": [{"init_type": "provisioned-concurrency", "cold": False, "ok": True}]})
    monkeypatch.setattr(cs, "remove_provisioned_concurrency", lambda *a, **k: True)
    monkeypatch.setattr(sess, "LOG_DELIVERY_WAIT_S", 0.0)


# --- configuration and the plan ------------------------------------------------------------------------------------


@pytest.mark.parametrize("over", [dict(max_minutes=0), dict(rates=()), dict(measure_s=0), dict(boot_repeats=-1), dict(idle_parallel=0)])
def test_nonsense_configuration_is_refused(tmp_path, over) -> None:
    with pytest.raises(ValueError):
        fast_cfg(tmp_path, **over)


def test_expected_durations_follow_from_the_configuration(tmp_path) -> None:
    m = expected_minutes(SessionConfig(out_dir=tmp_path))

    assert m["group_a_catalog_minutes"] == pytest.approx(11 * 3.0 + 3 * 5.0)  # 11 steps of 3 min + 3 boots
    assert m["group_b_lambda_minutes"] == pytest.approx(1 + 84 + 13.5 + 1.0)
    assert m["measuring_minutes"] == m["group_b_lambda_minutes"] and m["total_stack_minutes"] == m["measuring_minutes"] + 12.0


def test_the_spend_estimate_uses_the_small_calibration_caps_and_the_time_limit(tmp_path) -> None:
    cfg = SessionConfig(out_dir=tmp_path)

    est = spend_estimate(cfg)

    assert est["stack_hours"] == pytest.approx((cfg.max_minutes + cfg.setup_minutes) / 60)
    assert 0 < est["total_usd"] < 1.5  # 2 instances / 1 unit at most: far below the programme-wide bound


def test_the_plan_states_the_caps_the_limit_and_that_the_kill_switch_is_not_involved(tmp_path) -> None:
    text = format_plan(SessionConfig(out_dir=tmp_path))

    for needle in ("nothing has been applied", "never above 2", "never above 1", "HARD LIMIT", "kill switch", "upper bound"):
        assert needle in text


# --- the safety properties --------------------------------------------------------------------------------------------


def test_a_preflight_problem_means_nothing_is_applied_and_nothing_is_torn_down(tmp_path) -> None:
    rec = Recorder()

    result = run_session(fast_cfg(tmp_path), make_deps(rec, problems=["state is not empty"]))

    assert result["status"] == "aborted_preflight" and rec.events == []  # no apply, no destroy


def test_a_normal_session_applies_measures_then_destroys_and_verifies(tmp_path, stub_stages) -> None:
    rec = Recorder()

    result = run_session(fast_cfg(tmp_path), make_deps(rec))

    assert rec.events == ["apply", "healthy", "destroy", "verify"]
    assert result["status"] == "measured" and result["errors"] == {} and result["teardown"]["clean"]


@pytest.mark.parametrize("failing", ["apply_raises", "wait_raises"])
def test_teardown_runs_even_if_apply_or_the_health_wait_blows_up(tmp_path, stub_stages, failing) -> None:
    rec = Recorder()

    result = run_session(fast_cfg(tmp_path), make_deps(rec, **{failing: RuntimeError("boom")}))

    assert result["status"] == "crashed" and rec.events[-2:] == ["destroy", "verify"]


def test_a_crashing_stage_is_recorded_and_the_other_stages_and_the_teardown_still_happen(tmp_path, stub_stages, monkeypatch) -> None:
    monkeypatch.setattr(cs, "run_knee", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("load generator died")))
    rec = Recorder()

    result = run_session(fast_cfg(tmp_path), make_deps(rec))

    assert "knee" in result["errors"] and "load generator died" in result["errors"]["knee"]
    assert "boot" in rec.saved and "idle" in rec.saved and "provisioned" in rec.saved  # the other stages ran
    assert rec.events[-2:] == ["destroy", "verify"]


def test_a_keyboard_interrupt_still_tears_the_stack_down(tmp_path, stub_stages, monkeypatch) -> None:
    def interrupted(env):
        raise KeyboardInterrupt

    rec = Recorder()
    deps = make_deps(rec)
    deps.wait_healthy = interrupted

    result = run_session(fast_cfg(tmp_path), deps)

    assert result["status"] == "crashed" and rec.events == ["apply", "destroy", "verify"]


def test_a_provisioned_concurrency_config_is_removed_before_destroy(tmp_path, stub_stages, monkeypatch) -> None:
    order = []
    rec = Recorder()
    deps = make_deps(rec)
    monkeypatch.setattr(cs, "remove_provisioned_concurrency", lambda *a, **k: order.append("remove_pc") or True)
    original_destroy = deps.destroy
    deps.destroy = lambda: order.append("destroy") or original_destroy()

    run_session(fast_cfg(tmp_path), deps)

    assert order == ["remove_pc", "destroy"]


def test_a_failed_destroy_or_verifier_is_flagged_loudly(tmp_path, stub_stages) -> None:
    for kwargs in (dict(destroy_ok=False), dict(verify_code=1), dict(verify_code=2)):
        rec = Recorder()

        result = run_session(fast_cfg(tmp_path), make_deps(rec, **kwargs))

        assert not result["teardown"]["clean"]
        assert any("make destroy" in line for line in rec.logs)


def test_an_exception_inside_teardown_does_not_stop_the_remaining_cleanup(tmp_path, stub_stages, monkeypatch) -> None:
    rec = Recorder()
    deps = make_deps(rec)
    monkeypatch.setattr(cs, "remove_provisioned_concurrency", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("aws down")))

    result = run_session(fast_cfg(tmp_path), deps)

    assert rec.events[-2:] == ["destroy", "verify"] and any("clean-up failed" in e for e in result["teardown"]["errors"])


def test_the_time_limit_stops_the_measuring_and_the_teardown_still_runs(tmp_path, stub_stages, monkeypatch) -> None:
    seen_stop = []

    def blocking_idle(env, gaps, **k):
        env.wait(3600)  # would take an hour unless stopped
        seen_stop.append(env.stop.is_set())
        return {"probes": [], "rows": []}

    monkeypatch.setattr(cs, "run_idle_staircase", blocking_idle)
    rec = Recorder()
    started = time.monotonic()

    result = run_session(fast_cfg(tmp_path, max_minutes=0.02), make_deps(rec))  # 1.2 seconds

    assert time.monotonic() - started < 30 and seen_stop == [True]
    assert result["status"] == "stopped_early" and rec.events[-2:] == ["destroy", "verify"]


def test_stages_that_have_not_started_when_the_limit_hits_are_skipped(tmp_path, stub_stages, monkeypatch) -> None:
    def slow_knee(env, rates, **k):
        env.wait(3600)
        return {"steps": [], "windows": [], "raw": []}

    monkeypatch.setattr(cs, "run_knee", slow_knee)
    rec = Recorder()

    result = run_session(fast_cfg(tmp_path, max_minutes=0.02), make_deps(rec))

    assert "skipped" in result["errors"].get("boot", "")


def test_the_two_stage_groups_really_run_at_the_same_time(tmp_path, stub_stages, monkeypatch) -> None:
    barrier = threading.Barrier(2, timeout=10)  # both groups must be inside a stage simultaneously to pass
    monkeypatch.setattr(cs, "run_knee", lambda env, rates, **k: (barrier.wait(), {"steps": [], "windows": [], "raw": []})[1])
    monkeypatch.setattr(cs, "run_cold_start_probe", lambda env, **k: (barrier.wait(), [])[1])
    rec = Recorder()

    result = run_session(fast_cfg(tmp_path), make_deps(rec))

    assert "knee" not in result["errors"] and "cold_probe" not in result["errors"]


def test_raw_data_is_saved_as_each_stage_finishes_not_only_at_the_end(tmp_path, stub_stages) -> None:
    rec = Recorder()

    run_session(fast_cfg(tmp_path), make_deps(rec))

    for name in ("knee", "boot", "cold_probe", "idle", "report_lines", "provisioned", "calibration", "session"):
        assert name in rec.saved, name


# --- analysis -------------------------------------------------------------------------------------------------------


def good_raw() -> dict:
    steps = [LoadStep(200, 200, 0, 50.0, 100.0, 150.0, 60.0), LoadStep(400, 400, 0, 55.0, 150.0, 200.0, 60.0), LoadStep(800, 800, 0, 60.0, 500.0, 900.0, 60.0)]
    return {
        "knee": {"steps": [s.__dict__ for s in steps]},
        "boot": {"seconds": [95.0, 97.0, 96.0], "timeouts": 0},
        "cpu": [{"time": "t", "average": 30.0, "maximum": 80.0}],
        "cold_probe": [{"ms": 800.0, "ok": True, "cold": True}, {"ms": 60.0, "ok": True, "cold": False}],
        "idle": {"probes": [{"gap_minutes": 3, "known_before": ["a", "b"], "seen_after": ["a", "b"]},
                            {"gap_minutes": 6, "known_before": ["a", "b"], "seen_after": ["x", "y"]}], "rows": []},
        "report_lines": [
            "REPORT RequestId: 1\tDuration: 60.0 ms\tBilled Duration: 61 ms\tMemory Size: 128 MB\tMax Memory Used: 50 MB\tInit Duration: 200.0 ms",
            "REPORT RequestId: 2\tDuration: 55.0 ms\tBilled Duration: 56 ms\tMemory Size: 128 MB\tMax Memory Used: 50 MB",
        ],
        "provisioned": {"seconds": [160.0, 170.0], "timeouts": 0, "serve_checks": [{"init_type": "provisioned-concurrency", "cold": False, "ok": True}]},
    }


def test_complete_raw_data_gives_trusted_parameters() -> None:
    a = analyze_session(good_raw())
    p = a["parameters"]

    assert p["trustworthy"], p["warnings"]
    assert p["capacity_rpm_per_instance"] == 400 and p["slo_ms"] == 200.0 and p["scenario_gap_minutes"] == 6
    assert p["lambda_billed_ms_per_request"] == 56.0 and p["cpu_max_percent_at_knee"] == 80.0
    assert a["provisioned_serves_requests"] and a["crosscheck"]["agree"]


def test_missing_stages_become_warnings_not_crashes() -> None:
    a = analyze_session({})
    p = a["parameters"]

    assert not p["trustworthy"] and p["capacity_rpm_per_instance"] is None
    text = " | ".join(p["warnings"])
    assert "knee" in text and "REPORT" in text and "never confirmed" in text


def test_a_provisioned_container_that_reports_cold_breaks_trust_in_the_marker() -> None:
    raw = good_raw()
    raw["provisioned"]["serve_checks"] = [{"init_type": "on-demand", "cold": True, "ok": True}]

    a = analyze_session(raw)

    assert not a["provisioned_serves_requests"] and any("marker assumption" in w for w in a["parameters"]["warnings"])


def test_markers_that_disagree_with_aws_init_lines_break_trust() -> None:
    raw = good_raw()
    raw["cold_probe"] = [{"ms": 800.0, "ok": True, "cold": True}] * 3 + [{"ms": 60.0, "ok": True, "cold": False}]

    a = analyze_session(raw)

    assert not a["crosscheck"]["agree"] and not a["parameters"]["trustworthy"]


def test_the_result_text_shows_the_numbers_and_every_warning(tmp_path) -> None:
    text = format_result({"status": "measured", "minutes": 100.0, "errors": {"boot": "x"}, "teardown": {"destroyed": True, "verifier_exit": 0, "clean": True},
                          "analysis": analyze_session(good_raw())})

    assert "400" in text and "stage error - boot" in text and "clean=True" in text and "trustworthy" in text


def test_the_file_saver_writes_json_files(tmp_path) -> None:
    save = make_file_saver(tmp_path / "out")

    save("knee", {"a": 1})

    assert json.loads((tmp_path / "out" / "knee.json").read_text(encoding="utf-8")) == {"a": 1}
