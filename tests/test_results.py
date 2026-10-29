import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import comparison.results as results
from comparison.results import (
    load_fidelity,
    load_frame,
    per_minute_table,
    percentiles,
    read_result,
    relative_difference,
    runs_disagree,
    summarize_catalog,
    summarize_run,
    summarize_thumbnail,
    write_result,
)
from comparison.scenario import Episode, ServiceScenario

T0 = pd.Timestamp("2026-06-24 10:00")


def row(minute: int, ms: float = 50.0, ok: bool = True, cold=None, container=None, init="on-demand", status=None, service="thumbnail_service", offered=600.0) -> dict:
    return {
        "t": 0.0, "service": service, "name": "/thumbnail" if service == "thumbnail_service" else "/products",
        "ms": ms, "ok": ok, "status": status if status is not None else (200 if ok else 503), "error": None if ok else "HTTP 503",
        "minute": minute, "offered_rpm": offered, "cold": cold, "container": container, "init_type": init,
    }


def thumbnail_scenario(minutes: int = 60) -> ServiceScenario:
    index = pd.date_range(T0, periods=minutes, freq="min")
    series = pd.Series(2.0, index=index)

    def ep(i, seg_a, on, end, seg_b):
        m = lambda k: T0 + pd.Timedelta(minutes=k)  # noqa: E731
        return Episode(i, m(seg_a), m(on), m(end), m(seg_b), 300.0, m(1000 + i))

    return ServiceScenario(
        "thumbnail_service", series.iloc[:0], series, T0, 1.0,
        episodes=(ep(0, 10, 12, 15, 18), ep(1, 30, 32, 35, 38)),
    )


def catalog_scenario(minutes: int = 10, load_scale: float = 10.0) -> ServiceScenario:
    series = pd.Series(np.full(minutes, 10.0), index=pd.date_range(T0, periods=minutes, freq="min"))
    return ServiceScenario("catalog_service", series.iloc[:0], series, T0, load_scale)


# --- percentiles and errors ---------------------------------------------------------------------------


def test_percentiles_of_known_values() -> None:
    p = percentiles(range(1, 101))

    assert p["p50"] == pytest.approx(50.5) and p["p95"] == pytest.approx(95.05) and p["p99"] == pytest.approx(99.01)


def test_percentiles_of_nothing_are_none_not_zero() -> None:
    assert percentiles([]) == {"p50": None, "p95": None, "p99": None}


def test_latency_percentiles_use_only_successful_requests_but_errors_are_reported() -> None:
    rows = [row(0, ms=100.0) for _ in range(90)] + [row(0, ms=5.0, ok=False) for _ in range(10)]  # fast failures

    block = summarize_catalog([dict(r, service="catalog_service") for r in rows], catalog_scenario())["overall"]

    assert block["p50"] == 100.0 and block["p95"] == 100.0  # the 5 ms failures do not make it look faster
    assert (block["sent"], block["errors"], block["error_rate"]) == (100, 10, 0.1)
    assert block["status_counts"] == {"200": 90, "503": 10}


def test_p95_including_errors_is_finite_only_if_at_most_five_percent_failed() -> None:
    few = [row(0, ms=100.0) for _ in range(97)] + [row(0, ok=False) for _ in range(3)]
    many = [row(0, ms=100.0) for _ in range(94)] + [row(0, ok=False) for _ in range(6)]
    scen = catalog_scenario()

    assert summarize_catalog(few, scen)["overall"]["p95_incl_errors"] == 100.0
    assert summarize_catalog(many, scen)["overall"]["p95_incl_errors"] is None


def test_bad_rate_counts_failed_and_slow_requests() -> None:
    rows = [row(0, ms=50.0)] * 7 + [row(0, ms=500.0)] * 2 + [row(0, ok=False)]

    block = summarize_catalog(rows, catalog_scenario(), slo_ms=200.0)["overall"]

    assert block["bad_rate"] == pytest.approx(0.3)


def test_bad_rate_is_absent_without_an_slo() -> None:
    assert "bad_rate" not in summarize_catalog([row(0)], catalog_scenario())["overall"]


def test_an_empty_log_summarises_without_crashing() -> None:
    out = summarize_catalog([], catalog_scenario())

    assert out["overall"]["sent"] == 0 and out["overall"]["p95"] is None and out["overall"]["error_rate"] is None


def test_logs_missing_a_needed_column_are_refused() -> None:
    bad = [{"t": 1.0, "ms": 5.0}]

    with pytest.raises(ValueError, match="missing columns"):
        load_frame(bad)


# --- per-minute view and load fidelity ---------------------------------------------------------------------


def test_every_scenario_minute_appears_even_when_nothing_was_sent() -> None:
    df = load_frame([row(0), row(0), row(2)])

    table = per_minute_table(df, np.array([60.0, 60.0, 60.0, 60.0]))

    assert list(table["minute"]) == [0, 1, 2, 3] and list(table["sent"]) == [2, 0, 1, 0]
    assert table.loc[1, "p95"] is None or pd.isna(table.loc[1, "p95"])


def test_fidelity_is_fine_when_the_offered_load_was_delivered() -> None:
    table = pd.DataFrame({"offered_rpm": [600.0] * 10, "sent": [590, 610, 600, 585, 615, 598, 602, 600, 595, 605]})

    f = load_fidelity(table)

    assert f["ok"] and f["total"] == pytest.approx(1.0, abs=0.01)


def test_a_laptop_that_fell_behind_is_flagged() -> None:
    table = pd.DataFrame({"offered_rpm": [600.0] * 10, "sent": [300] * 10})

    f = load_fidelity(table)

    assert not f["ok"] and f["total"] == pytest.approx(0.5)


def test_one_starved_busy_minute_is_flagged_even_if_the_total_looks_fine() -> None:
    table = pd.DataFrame({"offered_rpm": [600.0] * 20, "sent": [620] * 19 + [200]})

    f = load_fidelity(table)

    assert f["total"] > 0.95 and f["worst_minute"] < 0.8 and not f["ok"]


def test_quiet_minutes_are_too_noisy_to_judge_individually() -> None:
    table = pd.DataFrame({"offered_rpm": [2.0, 2.0, 600.0], "sent": [0, 9, 600]})

    f = load_fidelity(table)

    assert f["minutes_judged"] == 1 and f["ok"]


def test_fidelity_of_an_all_quiet_run_is_not_judged() -> None:
    f = load_fidelity(pd.DataFrame({"offered_rpm": [0.0, 0.0], "sent": [0, 0]}))

    assert f["total"] is None and f["ok"]


# --- thumbnail: cold starts per burst --------------------------------------------------------------------


def thumbnail_rows() -> list[dict]:
    rows = []
    rows += [row(5, ms=40.0, cold=False, container="a")] * 3  # quiet gap, warm
    rows += [row(11, ms=900.0, cold=True, container="b")]  # ramp, before onset (12): cold
    rows += [row(12, ms=800.0, cold=True, container="c"), row(13, ms=60.0, cold=False, container="c")]
    rows += [row(14, ms=70.0, cold=False, container="b")] * 2
    rows += [row(20, ms=950.0, cold=True, container="d")]  # between bursts: a cold start outside any burst
    rows += [row(33, ms=700.0, cold=True, container="e"), row(33, ms=300.0, ok=False, cold=None, status=429)]
    rows += [row(34, ms=50.0, cold=False, container="e", init="provisioned-concurrency")]
    return rows


def test_cold_starts_are_counted_per_burst_and_in_total() -> None:
    out = summarize_thumbnail(thumbnail_rows(), thumbnail_scenario())

    assert out["cold_starts_total"] == 4
    assert [b["cold_starts"] for b in out["bursts"]] == [2, 1]
    assert out["cold_starts_in_bursts"] == 3 and out["cold_starts_per_burst"] == 1.5
    assert out["cold_starts_outside_bursts"] == 1  # only the one at minute 20, between the two bursts


def test_cold_starts_before_the_burst_onset_are_reported_separately() -> None:
    out = summarize_thumbnail(thumbnail_rows(), thumbnail_scenario())

    assert [b["cold_starts_before_onset"] for b in out["bursts"]] == [1, 0]


def test_containers_and_cold_vs_warm_latency() -> None:
    out = summarize_thumbnail(thumbnail_rows(), thumbnail_scenario())

    assert out["containers_seen"] == 5
    assert out["cold_start_ms_median"] > 5 * out["warm_ms_median"]


def test_requests_served_by_provisioned_concurrency_are_tracked() -> None:
    out = summarize_thumbnail(thumbnail_rows(), thumbnail_scenario())

    assert out["served_by_provisioned_fraction"] == pytest.approx(1 / len(thumbnail_rows()))


def test_requests_without_a_readable_marker_are_counted_not_assumed_warm() -> None:
    out = summarize_thumbnail(thumbnail_rows(), thumbnail_scenario())

    assert out["unknown_marker_requests"] == 1  # the throttled request


def test_errors_inside_a_burst_are_attributed_to_that_burst() -> None:
    out = summarize_thumbnail(thumbnail_rows(), thumbnail_scenario())

    assert out["bursts"][1]["segment"]["errors"] == 1 and out["bursts"][0]["segment"]["errors"] == 0
    assert out["bursts"][1]["segment"]["status_counts"]["429"] == 1


def test_burst_latency_covers_only_that_bursts_loud_minutes() -> None:
    rows = [row(11, ms=5000.0, cold=True, container="x"), row(13, ms=100.0, cold=False, container="x")]

    out = summarize_thumbnail(rows, thumbnail_scenario())

    assert out["bursts"][0]["loud_minutes"]["sent"] == 1 and out["bursts"][0]["loud_minutes"]["p95"] == 100.0
    assert out["bursts"][0]["segment"]["sent"] == 2  # the segment also holds the ramp minute


def test_a_burst_with_no_traffic_has_empty_stats_not_zeroes() -> None:
    out = summarize_thumbnail([row(5, cold=False, container="a")], thumbnail_scenario())

    assert out["bursts"][1]["segment"]["p95"] is None and out["bursts"][1]["cold_starts"] == 0


def test_catalog_summary_has_no_cold_start_fields() -> None:
    out = summarize_catalog([row(0, service="catalog_service")], catalog_scenario())

    assert "cold_starts_total" not in out and out["service"] == "catalog_service"


# --- one run ------------------------------------------------------------------------------------------------


def make_run(arm="predictive", idx=0, p95=100.0, cold_per_burst=1.0, fingerprint="abc", n=100) -> dict:
    """Build a run whose key metrics are exactly the given numbers."""
    rows = [dict(row(0, ms=p95, service="catalog_service"), offered=600.0) for _ in range(n)]
    cold_rows = []
    for b in range(2):
        for k in range(int(cold_per_burst)):
            cold_rows.append(row(12 + b * 20, ms=p95, cold=True, container=f"c{b}{k}"))
        cold_rows.append(row(13 + b * 20, ms=p95, cold=False, container="warm"))
    scen = {"catalog_service": catalog_scenario(), "thumbnail_service": thumbnail_scenario()}
    return summarize_run(
        arm=arm, run_index=idx, request_logs={"catalog_service": rows, "thumbnail_service": cold_rows},
        scenarios=scen, fingerprint=fingerprint,
    )


def test_a_run_summary_is_json_serialisable_and_round_trips(tmp_path: Path) -> None:
    run = make_run()
    path = tmp_path / "r" / "run.json"

    write_result(run, path)

    assert read_result(path) == run
    assert "NaN" not in path.read_text(encoding="utf-8") and "Infinity" not in path.read_text(encoding="utf-8")


def test_a_run_needs_at_least_one_request_log() -> None:
    with pytest.raises(ValueError, match="no request logs"):
        summarize_run(arm="x", run_index=0, request_logs={}, scenarios={}, fingerprint="f")


def test_run_summary_records_the_arm_and_scenario_identity() -> None:
    run = make_run(arm="reactive_standard", idx=1, fingerprint="deadbeef")

    assert (run["arm"], run["run_index"], run["scenario_fingerprint"]) == ("reactive_standard", 1, "deadbeef")


# --- D7: when do two runs of an arm disagree? --------------------------------------------------------------


def test_relative_difference_is_symmetric_and_zero_for_equal_values() -> None:
    assert relative_difference(100, 80) == relative_difference(80, 100) == pytest.approx(0.2)
    assert relative_difference(0, 0) == 0.0 and relative_difference(5, 5) == 0.0


def test_runs_within_twenty_percent_agree() -> None:
    assert runs_disagree(make_run(p95=100.0), make_run(p95=85.0, idx=1)) == []


def test_runs_that_differ_by_more_than_twenty_percent_on_p95_disagree_in_either_direction() -> None:
    low, high = make_run(p95=100.0), make_run(p95=200.0, idx=1)

    assert any("p95_ms" in r for r in runs_disagree(low, high))
    assert any("p95_ms" in r for r in runs_disagree(high, low))


def test_exactly_twenty_percent_apart_is_not_yet_a_disagreement() -> None:
    assert runs_disagree(make_run(p95=100.0), make_run(p95=125.0, idx=1)) == []  # |100-125|/125 = 20%


def test_cold_starts_per_burst_disagreement_is_caught() -> None:
    reasons = runs_disagree(make_run(cold_per_burst=1.0), make_run(cold_per_burst=5.0, idx=1))

    assert any("cold_starts_per_burst" in r for r in reasons)


def test_a_metric_missing_from_one_run_is_a_disagreement_not_a_pass() -> None:
    a = make_run()
    b = make_run(idx=1)
    b["services"]["catalog_service"]["overall"]["p95"] = None

    assert any("not measured" in r for r in runs_disagree(a, b))


def test_runs_of_different_arms_or_scenarios_cannot_be_compared() -> None:
    with pytest.raises(ValueError, match="same arm"):
        runs_disagree(make_run(arm="predictive"), make_run(arm="static_peak"))
    with pytest.raises(ValueError, match="different scenarios"):
        runs_disagree(make_run(fingerprint="a"), make_run(fingerprint="b"))


def test_the_collector_needs_no_aws_database_or_network() -> None:
    source = Path(results.__file__).read_text(encoding="utf-8")

    for forbidden in ("import boto3", "import psycopg2", "import requests", "import socket", "import locust"):
        assert forbidden not in source


def test_the_collector_reads_exactly_the_columns_the_load_generator_writes() -> None:
    source = (Path(results.__file__).resolve().parent.parent / "load_generator" / "scenario_locustfile.py").read_text(encoding="utf-8")

    for column in results.REQUIRED_COLUMNS:
        assert f'"{column}":' in source, column


def test_a_burst_segment_includes_both_its_first_and_last_minute_and_nothing_beside_them() -> None:
    # burst 0's segment is minutes 10..18 inclusive
    edges = [row(m, cold=True, container=f"c{m}") for m in (9, 10, 18, 19)]

    out = summarize_thumbnail(edges, thumbnail_scenario())

    assert out["bursts"][0]["cold_starts"] == 2 and out["bursts"][0]["segment"]["sent"] == 2
    assert out["cold_starts_outside_bursts"] == 2
