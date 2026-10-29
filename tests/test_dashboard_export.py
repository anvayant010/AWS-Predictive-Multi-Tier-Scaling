import copy
import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

import comparison.dashboard_export as de
import comparison.replay_compare as rc
from comparison import scenario_config as cfg
from decision_engine.models import Forecast


# --- episode rules ---------------------------------------------------------------------------------------------------------


def _series_with_bursts(kinds: list[str], gap: int = 60) -> np.ndarray:
    """Quiet baseline with bursts: 'ramp' climbs 25% -> 50% -> full, 'jump' goes straight to full size."""
    out = [2.0] * gap
    for k in kinds:
        out += [30.0, 75.0, 160.0, 300.0, 280.0, 290.0] if k == "ramp" else [290.0, 300.0, 280.0]
        out += [2.0] * gap
    return np.array(out)


def test_a_burst_that_starts_low_and_climbs_has_a_visible_ramp() -> None:
    v = _series_with_bursts(["ramp"])
    a = int(np.argmax(v >= 20))
    b = a + 5

    assert de.ramp_visible(v, a, b) is True
    assert v[de.full_onset(v, a, b)] >= de.FULL_ONSET_FRACTION * 300 and de.full_onset(v, a, b) > a


def test_a_burst_that_jumps_to_full_size_has_no_visible_ramp() -> None:
    v = _series_with_bursts(["jump"])
    a = int(np.argmax(v >= 20))

    assert de.ramp_visible(v, a, a + 2) is False
    assert de.full_onset(v, a, a + 2) == a


def test_the_picked_episodes_are_the_first_of_each_kind_in_chronological_order() -> None:
    v = _series_with_bursts(["jump", "ramp", "jump", "ramp"])
    picks = de.pick_burst_episodes(v)
    runs = rc.find_episodes(v, rc.LAMBDA_TRUTH_THRESHOLD, rc.MERGE_GAP_MINUTES)

    assert picks["unwarned"] == runs[0] and picks["warned"] == runs[1]  # first jump, first ramp - not the later ones


def test_a_kind_with_no_example_is_none_not_a_substitute() -> None:
    picks = de.pick_burst_episodes(_series_with_bursts(["jump", "jump"]))

    assert picks["warned"] is None and picks["unwarned"] is not None


def test_windows_are_clipped_to_the_data() -> None:
    assert de.clamp_window(-5, 400, 100) == (0, 99) and de.clamp_window(10, 20, 100) == (10, 20)


# --- accuracy --------------------------------------------------------------------------------------------------------------


def _forecasts(series: pd.Series, origins: list[int], level: float, spread: float, steps: int = 15) -> list[Forecast]:
    return [Forecast("svc", series.index[o].to_pydatetime(), (max(0.0, level - spread),) * steps, (level,) * steps, (level + spread,) * steps) for o in origins]


def test_catalog_accuracy_reports_error_coverage_and_the_same_time_last_week_baseline() -> None:
    n = 12000
    idx = pd.date_range("2026-01-01", periods=n, freq="min")
    i = np.arange(n)
    values = np.where(i >= 10080, 100.0, np.where(i >= 8000, 90.0, 50.0))  # a week ago 50, yesterday 90, now 100
    s = pd.Series(values, index=idx)
    fc = _forecasts(s, list(range(10100, 10200)), level=104.0, spread=10.0)

    a = de.accuracy_block("catalog_service", s, fc, horizon=5)

    assert a["mae"] == 4.0 and a["interval_coverage_pct"] == 100.0
    assert a["mae_same_time_last_week"] == 50.0  # same time LAST WEEK (50) against 100, not yesterday (90)
    assert a["mape_pct"] == 4.0 and a["points"] == 100 and "burst_warning" not in a


def test_burst_warning_counts_flagged_bursts_by_kind() -> None:
    v = _series_with_bursts(["ramp", "jump"])
    idx = pd.date_range("2026-01-01", periods=len(v), freq="min")
    s = pd.Series(v, index=idx)
    runs = rc.find_episodes(v, rc.LAMBDA_TRUTH_THRESHOLD, rc.MERGE_GAP_MINUTES)
    ramp_start = runs[0][0]
    origins = list(range(len(v) - 20))
    # a forecaster that raises p90 only in the minutes before the ramped burst
    fcs = []
    for o in origins:
        hi = 80.0 if ramp_start - 3 <= o < ramp_start else 5.0
        fcs.append(Forecast("t", idx[o].to_pydatetime(), (0.0,) * 15, (1.0,) * 15, (hi,) * 15))

    a = de.accuracy_block("thumbnail_service", s, fcs, horizon=3, burst_threshold=35.0)

    assert a["burst_warning"]["ramp_visible"] == {"flagged": 1, "bursts": 1, "pct": 100.0}
    assert a["burst_warning"]["no_ramp"] == {"flagged": 0, "bursts": 1, "pct": 0.0}


def test_interval_coverage_is_two_sided() -> None:
    n = 12000
    idx = pd.date_range("2026-01-01", periods=n, freq="min")
    s = pd.Series(np.full(n, 100.0), index=idx)
    below = de.accuracy_block("catalog_service", s, _forecasts(s, list(range(10100, 10150)), level=80.0, spread=5.0), horizon=5)  # band 75-85 < 100
    above = de.accuracy_block("catalog_service", s, _forecasts(s, list(range(10100, 10150)), level=120.0, spread=5.0), horizon=5)  # band 115-125 > 100

    assert below["interval_coverage_pct"] == 0.0 and above["interval_coverage_pct"] == 0.0


def test_forecast_fans_are_exported_in_load_generator_units() -> None:
    f = Forecast("s", datetime(2026, 1, 1), (1.0, 2.0), (2.0, 4.0), (3.0, 6.0))

    fan = de._fan([f], 10.0)

    assert fan == {"p10": [[10.0, 20.0]], "p50": [[20.0, 40.0]], "p90": [[30.0, 60.0]]}


# --- episodes and validation -----------------------------------------------------------------------------------------------


def _toy_lambda_episode():
    v = _series_with_bursts(["ramp"], gap=60)
    times = pd.date_range("2026-01-01", periods=len(v), freq="min")
    need = rc.containers_needed(v, 1.6)
    tl = {
        "predictive": rc.simulate_capacity_detail(len(v), 3, lambda t, c, p: 4 if 55 <= t < 90 else 0, initial=0, low=0, high=10),
        "reactive_on_demand_only": rc.simulate_capacity_detail(len(v), 3, lambda t, c, p: 0, initial=0, low=0, high=10),
    }
    a = int(np.argmax(v >= 20))
    fans = {q: [[0.0] * 15 for _ in range(30)] for q in ("p10", "p50", "p90")}  # separate lists per quantile
    stats = de.all_burst_stats(v, need, tl)
    return de.lambda_episode(episode_id="x", title="X", times=times, invocations=v, need_full=need, fans=fans, timelines=tl, burst=(a, a + 5),
                             window=(a - 15, a + 14), load_scale=10.0, all_bursts=stats, ramped=True)


def _valid_data():
    return {"schema_version": de.SCHEMA_VERSION, "meta": {"synthetic_traffic": True, "replay_not_live_aws": True}, "episodes": [_toy_lambda_episode()],
            "accuracy": {}, "aws": {}}


def test_a_lambda_episode_has_one_value_per_minute_everywhere_and_the_burst_marked() -> None:
    ep = _toy_lambda_episode()

    assert ep["minutes"] == 30 and len(ep["traffic"]) == 30 and len(ep["need"]) == 30
    assert all(len(arm[k]) == 30 for arm in ep["arms"].values() for k in arm)
    assert ep["burst"]["onset"] == 15 and ep["burst"]["ramp_visible"] is True and ep["burst"]["full_onset"] >= 15
    assert de.validate_dashboard_data(_valid_data()) == []


def test_traffic_is_exported_at_the_scale_the_load_generator_would_send() -> None:
    v = _series_with_bursts(["ramp"])
    ep = _toy_lambda_episode()

    assert max(ep["traffic"]) == round(float(v.max()) * 10.0)


def test_provisioned_capacity_arrives_after_the_delay_in_the_export() -> None:
    ep = _toy_lambda_episode()
    arm = ep["arms"]["predictive"]

    first_ask = next(i for i, x in enumerate(arm["asked"]) if x > 0)
    first_ready = next(i for i, x in enumerate(arm["capacity"]) if x > 0)
    assert first_ready - first_ask == 3 and arm["booting"][first_ask] == 4


def test_cold_starts_are_counted_for_the_burst_per_approach() -> None:
    ep = _toy_lambda_episode()

    assert ep["context"]["reactive_on_demand_only"]["cold_starts_in_this_burst"] > 0
    assert ep["all_bursts"]["bursts"] == 1


def test_all_burst_stats_say_how_often_predictive_beat_reactive() -> None:
    v = _series_with_bursts(["ramp", "ramp"])
    need = rc.containers_needed(v, 1.6)
    n = len(v)
    tl = {"predictive": rc.simulate_capacity_detail(n, 3, lambda t, c, p: 0, initial=0, low=0, high=10),
          "reactive_pc_tracking": rc.simulate_capacity_detail(n, 3, lambda t, c, p: 8, initial=8, low=0, high=10)}

    stats = de.all_burst_stats(v, need, tl)

    assert stats["predictive_vs_reactive"] == {"better": 0, "same": 0, "worse": 2}  # holding 8 always beats holding nothing


@pytest.mark.parametrize("mutate,expected", [
    (lambda d: d["episodes"][0].__setitem__("traffic", d["episodes"][0]["traffic"][:-1]), "traffic"),
    (lambda d: d["episodes"][0]["forecast"]["p50"].pop(), "forecast p50"),
    (lambda d: d["episodes"][0]["forecast"]["p10"][3].__setitem__(2, 99.0), "out of order"),
    (lambda d: d["episodes"][0]["arms"]["predictive"]["capacity"].__setitem__(4, 11), "hard cap"),
    (lambda d: d["episodes"][0]["arms"]["predictive"]["cold"].__setitem__(1, -1), "negative"),
    (lambda d: d["episodes"][0]["arms"]["predictive"].__setitem__("asked", [0]), "asked"),
    (lambda d: d["meta"].__setitem__("replay_not_live_aws", False), "replay-not-live"),
    (lambda d: d.pop("aws"), "aws"),
    (lambda d: d.__setitem__("schema_version", 99), "schema_version"),
])
def test_the_validator_catches_each_kind_of_inconsistency(mutate, expected) -> None:
    data = copy.deepcopy(_valid_data())
    mutate(data)

    problems = de.validate_dashboard_data(data)

    assert problems and any(expected in p for p in problems)


# --- comparison, facts, writing ---------------------------------------------------------------------------------------------


def _fake_run(over_pred: float, over_std: float, cold: float) -> dict:
    ec2 = {"over_target_minutes_pct": 0.0, "demand_above_target_pct": 0.0, "mean_instances": 2.0, "cost_usd": 1.0}
    lam = {"cold_starts_per_burst_mean": cold, "bursts_with_zero_cold_starts_pct": 0.0, "provisioned_unit_hours": 1.0, "cost_usd": 1.0}
    return {"catalog_service": {"_minutes": 5, "predictive": {**ec2, "over_target_minutes_pct": over_pred}, "reactive_standard": {**ec2, "over_target_minutes_pct": over_std}},
            "thumbnail_service": {"_minutes": 5, "predictive": dict(lam)}}


def test_the_comparison_block_gives_medians_across_seeds_and_the_hero_numbers(tmp_path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"0.7": [_fake_run(0.4, 5.0, 4.0), _fake_run(0.8, 6.0, 5.0), _fake_run(0.6, 5.1, 4.5)], "0.0": [_fake_run(0.2, 4.0, 4.0)]}), encoding="utf-8")

    block = de.comparison_block(path)

    assert block["0.7"]["seeds"] == 3 and block["0.0"]["seeds"] == 1
    assert block["0.7"]["catalog_service"]["predictive"]["over_target_minutes_pct"]["median"] == 0.6
    assert block["hero"] == {"predictive_inside_plan_pct": 99.4, "reactive_standard_inside_plan_pct": 94.9}


def test_aws_facts_come_from_the_pinned_measurements() -> None:
    f = de.aws_facts()

    assert f["instance_healthy_seconds"] == cfg.INSTANCE_BOOT_SECONDS == 92.5 and f["provisioned_ready_seconds"] == cfg.PROVISIONED_READY_SECONDS == 178.0
    assert f["cold_start_penalty_ms"] == cfg.COLD_START_PENALTY_MS and f["cold_starts_matched_aws_logs"] == {"marker": 25, "aws_init_lines": 25}


def test_write_round_trips_compactly(tmp_path) -> None:
    data = _valid_data()
    size = de.write_dashboard_data(data, tmp_path / "sub" / "d.json")

    assert json.loads((tmp_path / "sub" / "d.json").read_text(encoding="utf-8"))["schema_version"] == de.SCHEMA_VERSION
    assert size == (tmp_path / "sub" / "d.json").stat().st_size and ": " not in (tmp_path / "sub" / "d.json").read_text(encoding="utf-8")[:200]


# --- the real thing, small ---------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def built():
    """A real (shorter) export: 16 days of synthetic data, forecaster trained on the first 9."""
    return de.build_dashboard_data(ramp_fraction=0.7, seed=7, days=16, log=lambda *_: None)


def test_a_real_export_passes_validation_and_is_labelled_synthetic_replay(built) -> None:
    assert de.validate_dashboard_data(built) == []
    assert built["meta"]["synthetic_traffic"] and built["meta"]["replay_not_live_aws"] and built["meta"]["prices_verified"] is False
    assert {e["id"] for e in built["episodes"]} <= {"catalog-morning-rise", "burst-with-warning", "burst-no-warning"} and len(built["episodes"]) >= 2


def test_a_real_export_has_the_arms_the_dashboard_compares(built) -> None:
    by_id = {e["id"]: e for e in built["episodes"]}

    assert set(by_id["catalog-morning-rise"]["arms"]) == {"predictive", "reactive_standard", "reactive_aggressive", "static_peak"}
    lam = next(e for e in built["episodes"] if e["kind"] == "lambda")
    assert set(lam["arms"]) == {"predictive", "reactive_on_demand_only", "reactive_pc_tracking", "static_peak"}


def test_a_real_export_respects_the_hard_caps_and_the_oracle_never_misses(built) -> None:
    for ep in built["episodes"]:
        assert all(max(a["capacity"]) <= 10 for a in ep["arms"].values())
    ec2 = next(e for e in built["episodes"] if e["kind"] == "ec2")
    assert sum(ec2["arms"]["static_peak"]["over_plan"]) == 0
    for ep in (e for e in built["episodes"] if e["kind"] == "lambda"):
        assert sum(ep["arms"]["static_peak"]["cold"][ep["burst"]["onset"]:]) <= 1  # holding the peak in advance: (almost) no cold starts


def test_a_real_export_forecast_fans_cover_the_traffic_most_of_the_time(built) -> None:
    ec2 = next(e for e in built["episodes"] if e["kind"] == "ec2")
    n = ec2["minutes"]
    inside = [ec2["forecast"]["p10"][i][4] - 1 <= ec2["traffic"][i + 5] <= ec2["forecast"]["p90"][i][4] + 1 for i in range(n - 6)]  # 5 min ahead

    assert all(len(row) == 15 for row in ec2["forecast"]["p50"])
    assert 0.5 <= sum(inside) / len(inside) <= 1.0  # an 80% band should cover roughly 80%, certainly most of the time

    acc = built["accuracy"]["catalog_service"]
    assert 50 <= acc["interval_coverage_pct"] <= 100 and acc["mae"] < acc["mae_same_time_last_week"]  # beats "same time last week"


# --- EC2 episode and the cold-start window -------------------------------------------------------------------------------------


def _toy_ec2_episode():
    n = 200
    demand = np.full(n, 10.0)
    demand[80:] = 60.0  # natural units; scale 10 -> 100 -> 600 requests/min
    times = pd.date_range("2026-01-01", periods=n, freq="min")
    cap_full_natural = cfg.CATALOG_DECLARED_CAPACITY_RPM / 10.0  # 200 natural rpm per instance, plan 140
    tl = {
        "predictive": rc.simulate_capacity_detail(n, 2, lambda t, c, p: 1 if t < 70 else 1, initial=1, low=1, high=10),
        "reactive_standard": rc.simulate_capacity_detail(n, 2, lambda t, c, p: 1 if t < 80 else 3, initial=1, low=1, high=10),
    }
    week = {k: {"over_target_minutes_pct": 1.5, "instance_hours": 9.0} for k in tl}
    fans = {q: [[0.0] * 15 for _ in range(40)] for q in ("p10", "p50", "p90")}
    ep = de.ec2_episode(times=times, demand=demand, fans=fans, timelines=tl, week_metrics=week, window=(60, 99), load_scale=10.0, timing=rc.Timing())
    return ep, cap_full_natural


def test_an_ec2_episode_flags_the_minutes_over_the_plan_and_over_the_real_capacity() -> None:
    ep, _ = _toy_ec2_episode()
    pred, react = ep["arms"]["predictive"], ep["arms"]["reactive_standard"]

    # demand 600 rpm against 1 instance (plan 1400 rpm, full 2000 rpm at scale 10 -> plan 140, full 200 natural): 60 natural is under both
    assert sum(pred["over_plan"]) == 0 and sum(pred["saturated"]) == 0
    assert ep["minutes"] == 40 and len(ep["traffic"]) == 40 and ep["traffic"][0] == 100.0 and ep["traffic"][-1] == 600.0
    assert ep["units"]["plan_rpm_per_instance"] == 1400.0 and ep["units"]["full_rpm_per_instance"] == 2000.0
    assert ep["context"]["predictive"]["minutes_over_plan_week_pct"] == 1.5


def test_an_ec2_episode_exports_what_was_asked_and_what_is_still_booting() -> None:
    ep, _ = _toy_ec2_episode()
    react = ep["arms"]["reactive_standard"]

    first_ask = next(i for i, x in enumerate(react["asked"]) if x > 0)
    assert react["asked"][first_ask] == 2 and react["booting"][first_ask] == 2  # 1 -> 3 instances asked at minute 80 (window index 20)
    first_ready = next(i for i, x in enumerate(react["capacity"]) if x == 3)
    assert first_ready - first_ask == 2  # usable after the measured ~2-minute instance delay


def test_cold_starts_in_the_lead_up_to_a_burst_count_for_that_burst() -> None:
    n = 120
    inv = np.full(n, 2.0)
    inv[60:64] = 300.0
    need = np.ones(n, dtype=int)
    need[56] = 5  # a short spike 4 minutes before onset, inside the 5-minute lead-in
    times = pd.date_range("2026-01-01", periods=n, freq="min")
    tl = {"reactive_on_demand_only": rc.simulate_capacity_detail(n, 3, lambda t, c, p: 0, initial=0, low=0, high=10)}
    fans = {q: [[0.0] * 15 for _ in range(60)] for q in ("p10", "p50", "p90")}

    ep = de.lambda_episode(episode_id="e", title="E", times=times, invocations=inv, need_full=need, fans=fans, timelines=tl, burst=(60, 63),
                           window=(40, 99), load_scale=1.0, all_bursts={"bursts": 1}, ramped=False)

    assert ep["context"]["reactive_on_demand_only"]["cold_starts_in_this_burst"] >= 4  # the 4 extra containers at minute 56
