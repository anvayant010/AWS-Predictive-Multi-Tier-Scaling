import json
import os
import random
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import load_generator.scenario_player as sp
from load_generator.scenario_player import (
    MAX_FIRE_PROBABILITY,
    MIN_USERS,
    RatePlan,
    RequestLog,
    fire_probability,
    parse_marker,
    plan_from_scenario,
    read_request_log,
    should_fire,
    users_needed,
)

REPO = Path(__file__).resolve().parent.parent


def plan(*rpm: float, tail: float = 0.0) -> RatePlan:
    return RatePlan(rpm=np.array(rpm, dtype="float64"), tail_seconds=tail)


# --- the plan -----------------------------------------------------------------------------------------


def test_scenario_minute_k_is_played_during_real_minute_k() -> None:
    p = plan(10, 20, 30)

    assert [p.minute_at(t) for t in (0, 59.9, 60, 119.9, 120, 179.9)] == [0, 0, 1, 1, 2, 2]
    assert [p.rpm_at(t) for t in (0, 60, 120)] == [10.0, 20.0, 30.0]


def test_the_run_ends_exactly_when_the_scenario_does() -> None:
    p = plan(10, 20)

    assert p.duration_seconds() == 120.0
    assert p.minute_at(119.99) == 1 and p.minute_at(120.0) is None and p.rpm_at(500) is None


def test_the_tail_holds_the_last_minutes_rate_then_stops() -> None:
    p = plan(10, 20, tail=90)

    assert p.duration_seconds() == 210.0
    assert p.rpm_at(150) == 20.0 and p.rpm_at(209) == 20.0 and p.rpm_at(210) is None


def test_speedup_compresses_time_only_when_asked() -> None:
    p = plan(10, 20, 30)

    assert p.duration_seconds(speedup=30) == pytest.approx(6.0)
    assert p.minute_at(2.5, speedup=30) == 1


def test_negative_elapsed_time_is_refused() -> None:
    with pytest.raises(ValueError):
        plan(1, 2).minute_at(-1)


@pytest.mark.parametrize("bad", [[], [-1.0, 2.0], [float("nan")], [float("inf")]])
def test_bad_rate_plans_are_rejected(bad) -> None:
    with pytest.raises(ValueError):
        RatePlan(rpm=np.array(bad, dtype="float64"))


def test_negative_tail_is_rejected() -> None:
    with pytest.raises(ValueError):
        RatePlan(rpm=np.array([1.0]), tail_seconds=-1)


def test_plan_uses_the_scenarios_real_units() -> None:
    class Fake:
        offered_rpm = pd.Series([2.0, 4.0, 8.0]) * 1.0

    p = plan_from_scenario(Fake(), tail_seconds=5)

    assert list(p.rpm) == [2.0, 4.0, 8.0] and p.tail_seconds == 5 and p.minutes == 3


def test_plan_from_a_real_scenario_applies_load_scale(tmp_path) -> None:
    from comparison.scenario import ServiceScenario

    series = pd.Series([1.0, 2.0], index=pd.date_range("2026-06-01", periods=2, freq="min"))
    scenario = ServiceScenario("catalog_service", series.iloc[:0], series, pd.Timestamp("2026-06-01"), load_scale=30.0)

    assert list(plan_from_scenario(scenario).rpm) == [30.0, 60.0]


# --- pacing -------------------------------------------------------------------------------------------


def test_fire_probability_is_the_per_user_share_of_the_rate() -> None:
    assert fire_probability(rpm=600, active_users=100) == pytest.approx(0.1)  # 10 rps over 100 users
    assert fire_probability(0, 100) == 0.0


def test_fire_probability_never_exceeds_one_and_handles_no_users() -> None:
    assert fire_probability(6000, 5) == 1.0
    assert fire_probability(600, 0) == 0.0


def test_the_user_pool_keeps_every_users_probability_within_the_ceiling() -> None:
    for rpm in (0, 30, 400, 6000, 60000):
        p = plan(rpm)
        users = users_needed(p)
        assert users >= MIN_USERS
        assert fire_probability(rpm, users) <= MAX_FIRE_PROBABILITY + 1e-12


def test_the_user_pool_grows_with_the_peak_rate() -> None:
    assert users_needed(plan(60000)) > users_needed(plan(6000)) > users_needed(plan(60))


def test_thinning_reproduces_the_offered_rate_on_average() -> None:
    rng = random.Random(7)
    users, rpm, seconds = 200, 3000.0, 300  # 50 rps
    sent = sum(sum(should_fire(rpm, users, rng) for _ in range(users)) for _ in range(seconds))

    assert sent == pytest.approx(rpm / 60 * seconds, rel=0.03)


def test_thinning_is_poisson_like_not_lockstep() -> None:
    """Per-second counts should vary like a Poisson process (variance ~ mean), not be fixed."""
    rng = random.Random(11)
    users, rpm = 300, 1200.0  # 20 rps
    counts = [sum(should_fire(rpm, users, rng) for _ in range(users)) for _ in range(2000)]

    assert np.mean(counts) == pytest.approx(20, rel=0.03)
    assert 0.8 <= np.var(counts) / np.mean(counts) <= 1.2


def test_a_quiet_scenario_sends_almost_nothing() -> None:
    rng = random.Random(3)
    sent = sum(should_fire(2.0, 20, rng) for _ in range(20) for _ in range(60))  # 2 rpm for one minute

    assert sent <= 10


# --- reading the cold-start marker -------------------------------------------------------------------------


def test_marker_is_read_from_a_thumbnail_response() -> None:
    m = parse_marker(json.dumps({"image_id": "x", "cold_start": True, "container_id": "abc", "init_type": "on-demand"}))

    assert (m.cold_start, m.container_id, m.init_type) == (True, "abc", "on-demand")


@pytest.mark.parametrize(
    "body", [None, "", "not json", "[1, 2]", json.dumps({"image_id": "x"}), json.dumps({"cold_start": "yes"})]
)
def test_unreadable_or_missing_markers_are_unknown_not_guessed(body) -> None:
    m = parse_marker(body)

    assert m.cold_start is None


def test_a_warm_response_is_distinguished_from_an_unknown_one() -> None:
    assert parse_marker(json.dumps({"cold_start": False})).cold_start is False
    assert parse_marker("garbage").cold_start is None


# --- the request log ---------------------------------------------------------------------------------------


def test_request_log_round_trips_rows_in_order(tmp_path) -> None:
    log = RequestLog(tmp_path / "sub" / "r.jsonl")
    rows = [{"t": 1.0, "ms": 12.5, "ok": True, "cold": None}, {"t": 2.0, "ms": 900.0, "ok": False, "cold": True}]
    for row in rows:
        log.write(row)
    log.close()

    assert read_request_log(tmp_path / "sub" / "r.jsonl") == rows


def test_request_log_appends_rather_than_overwrites(tmp_path) -> None:
    for i in range(2):
        log = RequestLog(tmp_path / "r.jsonl")
        log.write({"i": i})
        log.close()

    assert [r["i"] for r in read_request_log(tmp_path / "r.jsonl")] == [0, 1]


def test_pacing_module_has_no_locust_aws_or_network_imports() -> None:
    source = Path(sp.__file__).read_text(encoding="utf-8")

    assert "import locust" not in source and "from locust" not in source
    for forbidden in ("import boto3", "import requests", "import socket", "import psycopg2"):
        assert forbidden not in source


# --- the real thing: Locust against a local fake server (in a subprocess) ----------------------------------


def run_driver(tmp_path: Path, **config) -> list[dict]:
    log = tmp_path / "requests.jsonl"
    config = {"log": str(log), "seed": 5, **config}
    subprocess.run(
        [sys.executable, str(REPO / "tests" / "locust_replay_driver.py"), json.dumps(config)],
        check=True,
        capture_output=True,
        timeout=120,
        cwd=REPO,
    )
    return read_request_log(log)


def test_real_locust_offers_the_scenario_rate_minute_by_minute(tmp_path) -> None:
    rpm = [0.0, 3000.0, 600.0, 0.0]  # 0, 50, 10, 0 requests/second
    rows = run_driver(tmp_path, service="thumbnail_service", rpm=rpm, speedup=20.0)  # 3 s per scenario minute

    per_minute = [sum(1 for r in rows if r["minute"] == m) for m in range(4)]
    assert per_minute[0] == 0 and per_minute[3] == 0  # quiet minutes send nothing
    assert per_minute[1] == pytest.approx(50 * 3, rel=0.35)
    assert per_minute[2] == pytest.approx(10 * 3, rel=0.5)
    assert per_minute[1] > 3 * per_minute[2]  # the burst is clearly bigger than the aftermath
    assert all(r["offered_rpm"] == rpm[r["minute"]] for r in rows)


def test_real_locust_records_status_latency_and_the_cold_start_marker(tmp_path) -> None:
    rows = run_driver(tmp_path, service="thumbnail_service", rpm=[1800.0, 1800.0], speedup=20.0)

    assert rows and all(r["service"] == "thumbnail_service" and r["name"] == "/thumbnail" for r in rows)
    assert all(r["ok"] and r["status"] == 200 and r["ms"] > 0 for r in rows)
    assert sum(1 for r in rows if r["cold"] is True) == 1  # the fake server marks only its first reply cold
    assert all(r["cold"] is False or r["cold"] is True for r in rows)
    assert {r["container"] for r in rows} == {"c-1"} and {r["init_type"] for r in rows} == {"on-demand"}


def test_real_locust_records_failures(tmp_path) -> None:
    rows = run_driver(tmp_path, service="thumbnail_service", rpm=[1800.0, 1800.0], speedup=20.0, mode="fail")

    failed = [r for r in rows if not r["ok"]]
    assert failed and all(r["status"] == 500 and r["error"] for r in failed)
    assert any(r["ok"] for r in rows)


def test_real_locust_catalog_requests_use_the_catalog_paths_and_carry_no_marker(tmp_path) -> None:
    rows = run_driver(tmp_path, service="catalog_service", rpm=[1800.0, 1800.0], speedup=20.0)

    assert {r["name"] for r in rows} <= {"/health", "/products", "/products/[id]"}
    assert len({r["name"] for r in rows}) >= 3
    assert all(r["cold"] is None and r["container"] is None for r in rows)


def test_importing_the_locustfile_without_a_scenario_does_nothing_dangerous() -> None:
    code = "import load_generator.scenario_locustfile as m; print(hasattr(m, 'ScenarioUser'))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO, check=True, timeout=60)

    assert out.stdout.strip() == "False"  # nothing is wired up (and nothing opens a log file) unless SCENARIO_DIR is set


def test_a_sped_up_real_run_is_refused(tmp_path) -> None:
    code = "import load_generator.scenario_locustfile"
    env = {
        **os.environ,
        "SCENARIO_DIR": str(tmp_path),
        "SCENARIO_SERVICE": "thumbnail_service",
        "SCENARIO_LOG": str(tmp_path / "l"),
        "SCENARIO_SPEEDUP": "30",
    }
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO, env=env, timeout=60)

    assert out.returncode != 0 and "never compressed" in (out.stderr + out.stdout)
