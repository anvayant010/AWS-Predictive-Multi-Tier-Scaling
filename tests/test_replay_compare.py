import numpy as np
import pytest

import comparison.replay_compare as rcmp
from comparison.replay_compare import (
    Timing,
    burst_windows,
    cold_starts_per_minute,
    containers_needed,
    ec2_metrics,
    format_comparison,
    lambda_metrics,
    reactive_ec2_target,
    run_ec2_arms,
    run_lambda_arms,
    simulate_capacity,
    step_targets,
)


# --- the capacity engine -------------------------------------------------------------------------------------------------------


def test_extra_capacity_arrives_only_after_the_delay() -> None:
    out = simulate_capacity(8, 3, lambda t, cur, pend: 4 if t >= 2 else 1, initial=1, low=1, high=10)

    assert list(out) == [1, 1, 1, 1, 1, 4, 4, 4]  # asked at minute 2, usable at minute 5


def test_releases_are_immediate() -> None:
    out = simulate_capacity(6, 3, lambda t, cur, pend: 5 if t < 2 else 2, initial=5, low=1, high=10)

    assert list(out) == [5, 5, 2, 2, 2, 2]


def test_a_release_cancels_capacity_still_on_its_way_before_touching_what_is_running() -> None:
    wants = {0: 4, 1: 2}  # ask for 4 (3 more on the way), then change our mind and want 2
    out = simulate_capacity(7, 3, lambda t, cur, pend: wants.get(t, 2), initial=1, low=1, high=10)

    assert list(out) == [1, 1, 1, 2, 2, 2, 2]  # of the 3 on the way, 2 are cancelled and 1 arrives on schedule (minute 3); nothing running is dropped


def test_requests_are_clamped_to_the_limits() -> None:
    out = simulate_capacity(6, 1, lambda t, cur, pend: 99 if t < 3 else -5, initial=2, low=1, high=10)

    assert out.max() == 10 and out.min() == 1


def test_a_delay_below_one_minute_is_refused() -> None:
    with pytest.raises(ValueError):
        simulate_capacity(3, 0, lambda t, c, p: 1, initial=1, low=0, high=3)
    with pytest.raises(ValueError):
        Timing(ec2_delay_minutes=0)


def test_measured_delays_round_up_to_whole_minutes() -> None:
    t = Timing()

    assert (t.ec2_delay_minutes, t.pc_delay_minutes, t.metric_lag_minutes) == (2, 3, 1)  # 92.5 s -> 2, 178 s -> 3


def test_decisions_are_acted_on_after_the_metric_lag_and_held_until_the_next_one() -> None:
    out = step_targets(12, [0, 4], [3, 6], lag=1, floor=1)

    assert list(out) == [1, 1, 3, 3, 3, 3, 6, 6, 6, 6, 6, 6]  # a decision from minute p acts at p + 1 + lag: 0 -> minute 2, 4 -> minute 6


# --- the reactive rule ---------------------------------------------------------------------------------------------------------


def _run_reactive(demand, target, n=None, lag=1, initial=1):
    n = n or len(demand)
    return simulate_capacity(n, 2, reactive_ec2_target(np.asarray(demand, float), target, lag=lag, low=1), initial=initial, low=1, high=10)


def test_the_reactive_rule_does_nothing_while_load_stays_under_target() -> None:
    assert set(_run_reactive([50.0] * 40, 100.0)) == {1}


def test_the_reactive_rule_needs_three_consecutive_breaching_minutes_and_then_waits_for_boot_and_lag() -> None:
    demand = [50.0] * 5 + [350.0] * 30
    out = _run_reactive(demand, 100.0)

    first_up = int(np.argmax(out > 1))
    # load rises at minute 5; data visible 1 + lag(1) minutes later; third breach at 5+2+2=9; usable 2 minutes after: 11
    assert first_up == 11 and out[first_up] == 4  # ceil(350 / 100)


def test_two_breaching_minutes_are_not_enough() -> None:
    demand = [50.0] * 5 + [350.0] * 2 + [50.0] * 20

    assert set(_run_reactive(demand, 100.0)) == {1}


def test_reactive_scales_in_only_after_fifteen_minutes_of_room() -> None:
    demand = [450.0] * 15 + [90.0] * 60
    out = _run_reactive(demand, 100.0, initial=5)

    low_start = 15  # load drops here
    first_down = int(np.argmax(out < 5))
    assert first_down >= low_start + 15  # not before 15 consecutive minutes with room to remove one instance
    assert out[-1] == 1  # and it ends at the floor for 90 requests/min at a target of 100


def test_reactive_never_goes_below_the_floor_or_above_the_ceiling() -> None:
    out = _run_reactive([5000.0] * 60, 100.0)

    assert out.max() == 10 and out.min() >= 1


def test_reactive_rejects_a_nonpositive_target() -> None:
    with pytest.raises(ValueError):
        reactive_ec2_target(np.ones(3), 0.0, lag=1, low=1)


# --- EC2 metrics ----------------------------------------------------------------------------------------------------------------


def test_ec2_metrics_count_minutes_over_the_plan_and_over_the_real_capacity() -> None:
    demand = np.array([100.0, 100.0, 150.0, 250.0])
    in_service = np.array([1, 1, 1, 1])

    m = ec2_metrics(demand, in_service, cap_target=140.0, cap_full=200.0)

    assert m["over_target_minutes_pct"] == 50.0 and m["saturated_minutes_pct"] == 25.0
    assert m["demand_above_target_pct"] == pytest.approx((10.0 + 110.0) / 600.0 * 100.0)
    assert m["instance_hours"] == pytest.approx(4 / 60.0) and m["mean_instances"] == 1.0 and m["changes"] == 0


def test_ec2_changes_are_counted() -> None:
    m = ec2_metrics(np.ones(5), np.array([1, 2, 2, 1, 1]), cap_target=10.0, cap_full=20.0)

    assert m["changes"] == 2 and m["max_instances"] == 2


# --- cold starts ----------------------------------------------------------------------------------------------------------------


def test_containers_needed_rounds_up_and_is_zero_when_idle() -> None:
    out = containers_needed(np.array([0.0, 30.0, 60.0, 61.0, -3.0]), duration_seconds=1.0)

    assert list(out) == [0, 1, 1, 2, 0]


def test_a_burst_after_a_long_quiet_spell_is_all_cold_but_a_second_one_soon_after_finds_warm_containers() -> None:
    need = np.array([1] * 3 + [5] * 3 + [1] * 5 + [5] * 3 + [1] * 30 + [5] * 3)
    cold = cold_starts_per_minute(need, np.zeros(len(need), dtype=int))

    assert cold[3] == 4 and cold[4] == 0  # 1 -> 5 containers: 4 new ones, once
    assert cold[11:14].sum() == 0  # 5 minutes later the 5 are still warm
    assert cold[-3] == 4  # 30 minutes later (> 15 idle minutes) they are gone


def test_provisioned_capacity_absorbs_the_burst_and_overflow_is_cold() -> None:
    need = np.array([1, 1, 6, 6, 1])
    cold = cold_starts_per_minute(need, np.array([0, 0, 4, 4, 0]))

    assert list(cold) == [0, 0, 1, 0, 0]  # 6 needed, 4 provisioned, 1 already warm on demand -> 1 new


def test_burst_windows_include_lead_and_tail() -> None:
    values = np.zeros(100)
    values[40:45] = 100.0

    (window,) = burst_windows(values)

    assert window == (40 - rcmp.BURST_LEAD_MINUTES, 44 + rcmp.BURST_TAIL_MINUTES)


def test_lambda_metrics_split_cold_starts_inside_and_outside_bursts() -> None:
    inv = np.zeros(120)
    inv[50:55] = 120.0
    inv[100] = 15.0  # a stray blip well outside any burst, under the burst threshold but needing 3 containers
    need = containers_needed(inv, duration_seconds=12.0)
    windows = burst_windows(inv)

    m = lambda_metrics(inv, need, np.zeros(120, dtype=int), windows, load_scale=1.0)

    assert m["bursts"] == 1 and m["cold_starts_in_bursts"] > 0 and m["cold_starts_outside_bursts"] > 0
    assert m["cold_starts_total"] == m["cold_starts_in_bursts"] + m["cold_starts_outside_bursts"]
    assert m["served_by_provisioned_pct"] == 0.0 and m["provisioned_unit_hours"] == 0.0


def test_provisioned_capacity_is_charged_for_even_when_idle() -> None:
    inv = np.zeros(60)
    need = containers_needed(inv, 1.0)

    m = lambda_metrics(inv, need, np.full(60, 3), [], load_scale=1.0)

    assert m["provisioned_unit_hours"] == pytest.approx(3.0) and m["cost_usd"] > 0


# --- the arms side by side ------------------------------------------------------------------------------------------------------


def _step_demand():
    return np.array([100.0] * 30 + [700.0] * 60 + [100.0] * 30)


def test_a_predictive_arm_that_acts_early_beats_a_reactive_one_on_load_above_target() -> None:
    demand = _step_demand()
    # natural capacity: scale 1, declared 2000 -> make per-instance numbers small via load_scale
    scale = 2000.0 / 200.0  # cap_full = 200, cap_target = 140
    early = ([20], [5])  # asks for 5 instances 10 minutes before the jump (decision at minute 20)

    out = run_ec2_arms(demand, early[0], early[1], load_scale=scale, timing=Timing())

    assert out["predictive"]["demand_above_target_pct"] < out["reactive_standard"]["demand_above_target_pct"]
    assert out["static_peak"]["over_target_minutes_pct"] == 0.0
    assert out["static_peak"]["instance_hours"] > out["reactive_standard"]["instance_hours"]  # the oracle pays for it all day
    assert out["reactive_aggressive"]["demand_above_target_pct"] <= out["reactive_standard"]["demand_above_target_pct"]


def test_the_ec2_arms_never_exceed_the_hard_ceiling() -> None:
    out = run_ec2_arms(np.full(60, 1e6), [0], [99], load_scale=10.0, timing=Timing())

    assert all(m["max_instances"] <= 10 for m in out.values())


def test_lambda_reactive_arm_has_no_provisioned_capacity_and_static_removes_burst_cold_starts() -> None:
    inv = np.full(300, 2.0)
    inv[100:110] = 300.0
    inv[200:206] = 300.0
    out = run_lambda_arms(inv, [], [], load_scale=1.0, duration_seconds=1.2, timing=Timing())

    assert out["reactive_on_demand_only"]["provisioned_unit_hours"] == 0.0
    assert out["reactive_on_demand_only"]["cold_starts_per_burst_mean"] > 0
    assert out["static_peak"]["cold_starts_in_bursts"] == 0
    assert out["static_peak"]["provisioned_unit_hours"] > 0
    assert out["predictive"]["cold_starts_in_bursts"] == out["reactive_on_demand_only"]["cold_starts_in_bursts"]  # no decisions -> same as doing nothing


def test_predictive_lambda_that_asks_early_enough_avoids_cold_starts_but_one_that_is_late_does_not() -> None:
    inv = np.full(200, 2.0)
    inv[100:110] = 300.0
    early = run_lambda_arms(inv, [90], [6], load_scale=1.0, duration_seconds=1.2, timing=Timing())["predictive"]
    late = run_lambda_arms(inv, [99], [6], load_scale=1.0, duration_seconds=1.2, timing=Timing())["predictive"]

    assert early["cold_starts_in_bursts"] == 0
    assert late["cold_starts_in_bursts"] > 0  # PC needs ~3 minutes: asking at the burst start is too late for its first minutes


def test_the_text_report_says_it_is_a_replay() -> None:
    result = {
        "ramp_fraction": 0.7, "seed": 1, "timing": {"ec2_delay_minutes": 2, "pc_delay_minutes": 3, "metric_lag_minutes": 1},
        "catalog_service": {"_minutes": 10, **run_ec2_arms(np.ones(10), [0], [1], load_scale=10.0, timing=Timing())},
        "thumbnail_service": {"_minutes": 10, **run_lambda_arms(np.ones(10), [], [], load_scale=1.0, duration_seconds=1.0, timing=Timing())},
    }

    text = format_comparison(result)

    assert "not a live AWS experiment" in text and "reactive_standard" in text and "static_peak" in text


# --- across seeds -------------------------------------------------------------------------------------------------------------


def _fake_result(cost: float, cold: float) -> dict:
    ec2 = {"over_target_minutes_pct": 1.0, "demand_above_target_pct": 0.1, "mean_instances": 2.0, "cost_usd": cost}
    lam = {"cold_starts_per_burst_mean": cold, "bursts_with_zero_cold_starts_pct": 0.0, "provisioned_unit_hours": 1.0, "cost_usd": cost}
    return {"catalog_service": {"_minutes": 5, "predictive": dict(ec2)}, "thumbnail_service": {"_minutes": 5, "predictive": dict(lam)}}


def test_aggregate_reports_median_and_range_across_seeds() -> None:
    agg = rcmp.aggregate([_fake_result(1.0, 4.0), _fake_result(3.0, 6.0), _fake_result(2.0, 5.0)])

    cost = agg["catalog_service"]["predictive"]["cost_usd"]
    cold = agg["thumbnail_service"]["predictive"]["cold_starts_per_burst_mean"]
    assert (cost["median"], cost["min"], cost["max"]) == (2.0, 1.0, 3.0)
    assert (cold["median"], cold["min"], cold["max"]) == (5.0, 4.0, 6.0)
    assert "_minutes" not in agg["catalog_service"]


def test_aggregate_text_names_the_seed_count() -> None:
    results = [_fake_result(1.0, 4.0), _fake_result(2.0, 5.0)]

    text = rcmp.format_aggregate(rcmp.aggregate(results), 0.7, 2)

    assert "ACROSS 2 SEEDS" in text and "predictive" in text


def test_reactive_pc_tracking_arrives_after_the_lag_the_breaches_and_the_delay_so_it_misses_a_short_burst_start() -> None:
    inv = np.full(200, 2.0)
    inv[100:106] = 300.0  # a 6-minute burst
    arms = run_lambda_arms(inv, [], [], load_scale=1.0, duration_seconds=1.2, timing=Timing())

    tracking = arms["reactive_pc_tracking"]
    assert tracking["provisioned_unit_hours"] > 0  # it does react...
    assert tracking["cold_starts_in_bursts"] >= arms["static_peak"]["cold_starts_in_bursts"]  # ...but cannot match capacity held in advance
    assert tracking["cold_starts_in_bursts"] > 0  # and a 6-minute burst is over before 1 lag + 3 breaches + 3 delay minutes pass
