from pathlib import Path

import pytest

import comparison.calibration as cal
from comparison.calibration import (
    IdleProbe,
    LoadStep,
    cold_start_stats,
    crosscheck_cold_starts,
    derive_parameters,
    find_knee,
    handler_duration_stats,
    idle_lifetime,
    parse_report_lines,
    summarize_step,
    timing_stats,
)


def step(offered: float, p95: float | None = 100.0, errors: int = 0, sent: int | None = None, duration_s: float = 60.0) -> LoadStep:
    """A step whose delivered rate equals the offered one unless `sent` says otherwise."""
    sent = int(offered * duration_s / 60) if sent is None else sent
    return LoadStep(offered, sent, errors, p95 and p95 * 0.5, p95, p95 and p95 * 1.5, duration_s)


# --- one step ----------------------------------------------------------------------------------------------


def test_a_step_summarises_successful_latency_and_counts_failures() -> None:
    rows = [{"ms": float(m), "ok": True} for m in range(1, 101)] + [{"ms": 5.0, "ok": False}] * 4

    s = summarize_step(rows, offered_rpm=104.0, duration_s=60.0)

    assert (s.sent, s.errors) == (104, 4) and s.p95_ms == pytest.approx(95.05)
    assert s.error_rate == pytest.approx(4 / 104) and s.achieved_rpm == pytest.approx(104.0) and s.fidelity == pytest.approx(1.0)


def test_a_step_with_no_successful_requests_has_no_percentiles() -> None:
    s = summarize_step([{"ms": 1.0, "ok": False}] * 3, 3.0, 60.0)

    assert s.p95_ms is None and s.error_rate == 1.0


def test_an_empty_step_counts_as_fully_failed_not_perfect() -> None:
    s = summarize_step([], 100.0, 60.0)

    assert s.error_rate == 1.0 and s.fidelity == 0.0


# --- the knee ----------------------------------------------------------------------------------------------


def curve() -> list[LoadStep]:
    return [step(200, 100), step(400, 105), step(800, 150), step(1600, 190), step(2400, 260), step(3200, 900)]


def test_the_knee_is_the_highest_step_still_within_twice_the_unloaded_p95() -> None:
    k = find_knee(curve())

    assert k["knee_rpm"] == 1600 and k["limit_reached"] and not k["knee_is_lower_bound"]
    assert k["first_failing_rpm"] == 2400 and k["unloaded_p95_ms"] == 100 and k["slo_ms"] == 200


def test_exactly_twice_the_unloaded_p95_still_passes() -> None:
    assert find_knee([step(100, 50), step(200, 100), step(300, 101)])["knee_rpm"] == 200


def test_errors_at_one_percent_fail_a_step_even_if_latency_is_fine() -> None:
    steps = [step(200, 100), step(400, 100, errors=4)]  # 4 of 400 = 1.0%

    k = find_knee(steps)

    assert k["knee_rpm"] == 200 and "errors" in k["first_failing_reason"]


def test_just_under_one_percent_errors_passes() -> None:
    assert find_knee([step(200, 100), step(400, 100, errors=3)])["knee_rpm"] == 400


def test_a_later_good_looking_step_after_a_failure_does_not_count() -> None:
    steps = [step(200, 100), step(400, 900), step(800, 100)]

    assert find_knee(steps)["knee_rpm"] == 200


def test_when_no_step_fails_the_knee_is_reported_as_a_lower_bound_only() -> None:
    k = find_knee([step(200, 100), step(400, 110)])

    assert k["knee_rpm"] == 400 and k["knee_is_lower_bound"] and not k["limit_reached"]


def test_a_generator_that_could_not_deliver_the_load_is_flagged_not_blamed_on_the_server() -> None:
    steps = [step(200, 100), step(400, 100, sent=250)]  # only 62% of the offered load went out

    k = find_knee(steps)

    assert k["generator_limited"] and k["knee_rpm"] == 200


def test_steps_are_sorted_so_input_order_does_not_matter() -> None:
    assert find_knee(list(reversed(curve())))["knee_rpm"] == find_knee(curve())["knee_rpm"]


def test_an_unhealthy_baseline_step_is_refused() -> None:
    with pytest.raises(ValueError, match="unloaded baseline"):
        find_knee([step(200, None), step(400, 100)])
    with pytest.raises(ValueError, match="unloaded baseline"):
        find_knee([step(200, 100, errors=20), step(400, 100)])


def test_no_steps_is_refused() -> None:
    with pytest.raises(ValueError):
        find_knee([])


def test_the_rule_is_reported_with_the_result() -> None:
    assert find_knee(curve())["rule"] == {"latency_factor": 2.0, "max_error_rate": 0.01, "min_fidelity": 0.95}


def test_a_stricter_latency_factor_lowers_the_knee() -> None:
    assert find_knee(curve(), latency_factor=1.5)["knee_rpm"] == 800


# --- cold starts -------------------------------------------------------------------------------------------


def test_cold_vs_warm_latency() -> None:
    rows = [{"ms": 900.0, "ok": True, "cold": True}, {"ms": 700.0, "ok": True, "cold": True}] + [
        {"ms": 60.0, "ok": True, "cold": False}
    ] * 5 + [{"ms": 30.0, "ok": False, "cold": None}]

    s = cold_start_stats(rows)

    assert (s["cold_n"], s["warm_n"], s["unknown_n"]) == (2, 5, 1)
    assert s["cold_median_ms"] == 800.0 and s["warm_median_ms"] == 60.0 and s["cold_penalty_ms"] == 740.0


def test_no_cold_starts_leaves_the_penalty_unknown_not_zero() -> None:
    s = cold_start_stats([{"ms": 60.0, "ok": True, "cold": False}])

    assert s["cold_median_ms"] is None and s["cold_penalty_ms"] is None


REPORT_COLD = "REPORT RequestId: abc-1\tDuration: 62.41 ms\tBilled Duration: 63 ms\tMemory Size: 128 MB\tMax Memory Used: 52 MB\tInit Duration: 233.07 ms"
REPORT_WARM = "REPORT RequestId: abc-2\tDuration: 55.10 ms\tBilled Duration: 56 ms\tMemory Size: 128 MB\tMax Memory Used: 52 MB"


def test_report_lines_are_parsed_and_only_cold_ones_carry_an_init_duration() -> None:
    rows = parse_report_lines(["START RequestId: abc-1", REPORT_COLD, "garbage", REPORT_WARM, "END RequestId: abc-2"])

    assert [r["request_id"] for r in rows] == ["abc-1", "abc-2"]
    assert rows[0]["init_ms"] == 233.07 and rows[1]["init_ms"] is None
    assert (rows[0]["duration_ms"], rows[0]["billed_ms"], rows[0]["memory_mb"], rows[0]["max_memory_mb"]) == (62.41, 63.0, 128.0, 52.0)


def test_a_report_line_without_a_duration_is_ignored() -> None:
    assert parse_report_lines(["REPORT RequestId: x\tMemory Size: 128 MB"]) == []


def test_markers_that_match_aws_init_lines_agree() -> None:
    rows = parse_report_lines([REPORT_COLD, REPORT_COLD.replace("abc-1", "abc-3"), REPORT_WARM])

    c = crosscheck_cold_starts(2, rows)

    assert c["agree"] and c["difference"] == 0 and c["aws_init_ms_median"] == 233.07


def test_a_mismatch_between_markers_and_aws_is_reported_not_hidden() -> None:
    rows = parse_report_lines([REPORT_COLD, REPORT_WARM])

    c = crosscheck_cold_starts(3, rows)

    assert not c["agree"] and c["difference"] == 2
    assert crosscheck_cold_starts(3, rows, tolerance=2)["agree"]


def test_handler_duration_uses_warm_requests_only() -> None:
    rows = parse_report_lines([REPORT_COLD, REPORT_WARM, REPORT_WARM.replace("55.10", "57.0").replace("56 ms", "58 ms")])

    d = handler_duration_stats(rows)

    assert d["n"] == 2 and d["duration_ms_median"] == pytest.approx(56.05) and d["billed_ms_median"] == 57.0 and d["memory_mb"] == 128.0


def test_handler_duration_with_no_warm_requests_is_unknown() -> None:
    assert handler_duration_stats(parse_report_lines([REPORT_COLD]))["billed_ms_median"] is None


# --- idle-container lifetime -----------------------------------------------------------------------------------


def probe(gap: float, before: str, after: str) -> IdleProbe:
    return IdleProbe(gap, frozenset(before.split()), frozenset(after.split()))


def test_a_probe_counts_the_containers_that_answered_again() -> None:
    p = probe(10, "a b c d", "a b x y")

    assert p.survivors == 2 and p.survival == 0.5 and p.reliable


def test_the_lifetime_is_bracketed_between_the_last_good_gap_and_the_first_bad_one() -> None:
    probes = [probe(3, "a b c d", "a b c d"), probe(6, "a b c d", "a b c d"), probe(10, "a b c d", "a b c d"),
              probe(15, "a b c d", "w x y z"), probe(20, "w x y z", "p q r s")]

    r = idle_lifetime(probes)

    assert r["lifetime_lower_bound_minutes"] == 10 and r["lifetime_upper_bound_minutes"] == 15
    assert r["recommended_gap_minutes"] == 15 and not r["exceeds_longest_tested"]


def test_half_surviving_counts_as_alive_fewer_than_half_as_dead() -> None:
    half = idle_lifetime([probe(5, "a b c d", "a b x y")])
    assert half["lifetime_upper_bound_minutes"] is None and half["lifetime_lower_bound_minutes"] == 5
    assert idle_lifetime([probe(5, "a b c d", "a w x y")])["lifetime_upper_bound_minutes"] == 5


def test_if_nothing_ever_died_no_gap_is_recommended() -> None:
    r = idle_lifetime([probe(5, "a b", "a b"), probe(30, "a b", "a b")])

    assert r["exceeds_longest_tested"] and r["recommended_gap_minutes"] is None and r["lifetime_lower_bound_minutes"] == 30


def test_a_probe_that_may_have_missed_containers_is_not_treated_as_a_death() -> None:
    # 6 containers known but only 2 answered: the probe probably just did not reach the others
    weak = probe(10, "a b c d e f", "a b")

    assert not weak.reliable
    r = idle_lifetime([probe(5, "a b c d e f", "a b c d e f"), weak])
    assert r["lifetime_upper_bound_minutes"] is None and r["unreliable_probes"] == [10]


def test_probes_are_ordered_by_gap_regardless_of_input_order() -> None:
    probes = [probe(15, "a b", "x y"), probe(5, "a b", "a b")]

    assert idle_lifetime(probes)["lifetime_lower_bound_minutes"] == 5 and idle_lifetime(probes)["recommended_gap_minutes"] == 15


def test_a_late_survivor_after_a_death_does_not_extend_the_lifetime() -> None:
    probes = [probe(5, "a b", "a b"), probe(10, "a b", "x y"), probe(20, "a b", "a b")]

    r = idle_lifetime(probes)

    assert r["lifetime_lower_bound_minutes"] == 5 and r["recommended_gap_minutes"] == 10


def test_no_probes_means_nothing_is_concluded() -> None:
    r = idle_lifetime([])

    assert r["recommended_gap_minutes"] is None and not r["exceeds_longest_tested"]


# --- timings ------------------------------------------------------------------------------------------------


def test_timing_stats_and_spread() -> None:
    t = timing_stats([90.0, 100.0, 110.0])

    assert (t["n"], t["min_s"], t["median_s"], t["max_s"]) == (3, 90.0, 100.0, 110.0) and t["spread_pct"] == pytest.approx(20.0)


def test_timing_stats_of_nothing_are_unknown() -> None:
    assert timing_stats([])["median_s"] is None


# --- the final parameters -----------------------------------------------------------------------------------


def good_inputs() -> dict:
    rows = parse_report_lines([REPORT_COLD, REPORT_WARM])
    return dict(
        knee=find_knee(curve()), cold=cold_start_stats([{"ms": 800.0, "ok": True, "cold": True}, {"ms": 60.0, "ok": True, "cold": False}]),
        crosscheck=crosscheck_cold_starts(1, rows), durations=handler_duration_stats(rows),
        idle=idle_lifetime([probe(10, "a b", "a b"), probe(15, "a b", "x y")]),
        boot=timing_stats([92.0, 97.0, 95.0]), provisioned_ready=timing_stats([160.0, 170.0, 165.0]),
    )


def test_a_clean_calibration_yields_trusted_parameters() -> None:
    p = derive_parameters(**good_inputs())

    assert p["trustworthy"] and p["warnings"] == []
    assert p["capacity_rpm_per_instance"] == 1600 and p["capacity_rpm_per_instance_at_70pct"] == pytest.approx(1120.0)
    assert p["slo_ms"] == 200 and p["scenario_gap_minutes"] == 15 and p["lambda_billed_ms_per_request"] == 56.0
    assert p["instance_boot_seconds_median"] == 95.0 and p["provisioned_ready_seconds_median"] == 165.0


def test_each_problem_is_surfaced_as_a_warning_and_withdraws_trust() -> None:
    inputs = good_inputs()
    inputs["knee"] = find_knee([step(200, 100), step(400, 110)])  # never reached the limit
    inputs["crosscheck"] = crosscheck_cold_starts(5, parse_report_lines([REPORT_COLD]))
    inputs["idle"] = idle_lifetime([probe(5, "a b", "a b")])
    inputs["boot"] = timing_stats([95.0])

    p = derive_parameters(**inputs)

    text = " | ".join(p["warnings"])
    assert not p["trustworthy"]
    for needle in ("lower bound", "disagree", "fewer bursts", "fewer than 2"):
        assert needle in text


def test_a_generator_limited_knee_is_not_trusted() -> None:
    inputs = good_inputs()
    inputs["knee"] = find_knee([step(200, 100), step(400, 100, sent=250)])

    p = derive_parameters(**inputs)

    assert not p["trustworthy"] and any("generator" in w for w in p["warnings"])


def test_the_analysis_needs_no_aws_or_network() -> None:
    source = Path(cal.__file__).read_text(encoding="utf-8")

    for forbidden in ("import boto3", "import requests", "import socket", "import urllib"):
        assert forbidden not in source
