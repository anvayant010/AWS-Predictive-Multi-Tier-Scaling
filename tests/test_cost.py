import json
import random
from pathlib import Path

import pytest

import comparison.cost as cost
from comparison.cost import (
    PRICES,
    LambdaShape,
    Prices,
    alb_cost,
    ec2_cost,
    estimate_before_run,
    instance_hours,
    integrate_step,
    lambda_cost,
    provisioned_unit_hours,
    read_capacity_log,
    run_cost,
)

HOUR = 3600.0


# --- integration --------------------------------------------------------------------------------------------


def test_a_step_function_is_integrated_value_times_seconds() -> None:
    assert integrate_step([(0.0, 1.0), (60.0, 2.0)], end=120.0) == 60 * 1 + 60 * 2


def test_the_last_value_is_held_until_the_end_of_the_run() -> None:
    assert integrate_step([(10.0, 3.0)], end=70.0) == 180.0


def test_no_samples_cost_nothing() -> None:
    assert integrate_step([], end=100.0) == 0.0


def test_unordered_samples_are_refused() -> None:
    with pytest.raises(ValueError, match="time order"):
        integrate_step([(60.0, 1.0), (0.0, 1.0)], end=100.0)


def test_a_run_end_before_the_last_sample_is_refused() -> None:
    with pytest.raises(ValueError, match="before the last"):
        integrate_step([(0.0, 1.0), (60.0, 1.0)], end=30.0)


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_negative_or_non_finite_capacity_is_refused(bad) -> None:
    with pytest.raises(ValueError):
        integrate_step([(0.0, bad)], end=10.0)


def test_capacity_above_the_hard_caps_is_refused_not_billed() -> None:
    with pytest.raises(ValueError, match="hard cap"):
        instance_hours([(0.0, 11.0)], end=HOUR)
    with pytest.raises(ValueError, match="hard cap"):
        provisioned_unit_hours([(0.0, 11.0)], end=HOUR)


def test_instance_hours_for_two_instances_over_an_hour() -> None:
    assert instance_hours([(0.0, 2.0)], end=HOUR) == pytest.approx(2.0)


# --- EC2 ----------------------------------------------------------------------------------------------------


def test_ec2_list_price_is_hours_times_the_hourly_rate() -> None:
    assert ec2_cost(10.0)["ec2_instances"] == pytest.approx(10 * 0.0104)


def test_the_unlimited_surcharge_upper_bound_is_for_cpu_above_baseline_only() -> None:
    # 100% CPU on 2 vCPUs, baseline 20%: 0.8 x 2 vCPU x $0.05 = $0.08 per instance-hour
    assert ec2_cost(1.0, cpu_utilization=1.0)["ec2_unlimited_surcharge_upper_bound"] == pytest.approx(0.08)
    assert ec2_cost(1.0, cpu_utilization=0.5)["ec2_unlimited_surcharge_upper_bound"] == pytest.approx(0.03)
    assert ec2_cost(1.0, cpu_utilization=0.2)["ec2_unlimited_surcharge_upper_bound"] == 0.0
    assert ec2_cost(1.0, cpu_utilization=0.0)["ec2_unlimited_surcharge_upper_bound"] == 0.0


def test_the_surcharge_defaults_to_the_conservative_worst_case() -> None:
    assert ec2_cost(1.0)["ec2_unlimited_surcharge_upper_bound"] == pytest.approx(0.08)


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_impossible_cpu_utilisation_is_refused(bad) -> None:
    with pytest.raises(ValueError):
        ec2_cost(1.0, cpu_utilization=bad)


def test_alb_cost_is_the_hourly_charge_plus_assumed_capacity_units() -> None:
    assert alb_cost(1.0) == pytest.approx(0.0225 + 0.008)
    assert alb_cost(2.0, lcu_per_hour=0) == pytest.approx(2 * 0.0225)


# --- Lambda -------------------------------------------------------------------------------------------------


def test_lambda_costs_for_a_million_on_demand_requests() -> None:
    c = lambda_cost(requests=1_000_000, provisioned_hours=0.0)

    assert c["lambda_requests"] == pytest.approx(0.20)
    assert c["api_gateway"] == pytest.approx(1.00)
    # 1M x 0.052 s (measured billed duration) x 0.125 GB x $0.0000166667 per GB-s
    assert c["lambda_execution_on_demand"] == pytest.approx(1_000_000 * 0.052 * 0.125 * 0.0000166667)
    assert c["lambda_execution_provisioned"] == 0.0 and c["lambda_provisioned_holding"] == 0.0


def test_holding_provisioned_concurrency_costs_per_gb_second_even_when_idle() -> None:
    c = lambda_cost(requests=0, provisioned_hours=1.0)

    assert c["lambda_provisioned_holding"] == pytest.approx(3600 * 0.125 * 0.0000041667)
    assert c["lambda_requests"] == 0.0 and c["lambda_execution_on_demand"] == 0.0


def test_requests_served_by_provisioned_concurrency_use_the_cheaper_execution_rate() -> None:
    all_od = lambda_cost(requests=1_000_000, provisioned_hours=0.0)
    all_pc = lambda_cost(requests=1_000_000, provisioned_hours=0.0, provisioned_fraction=1.0)
    half = lambda_cost(requests=1_000_000, provisioned_hours=0.0, provisioned_fraction=0.5)

    assert all_pc["lambda_execution_on_demand"] == 0.0 and all_od["lambda_execution_provisioned"] == 0.0
    assert all_pc["lambda_execution_provisioned"] < all_od["lambda_execution_on_demand"]
    assert half["lambda_execution_on_demand"] + half["lambda_execution_provisioned"] == pytest.approx(
        (all_od["lambda_execution_on_demand"] + all_pc["lambda_execution_provisioned"]) / 2
    )


def test_bigger_memory_scales_lambda_gb_second_costs() -> None:
    small = lambda_cost(requests=1000, provisioned_hours=1.0, shape=LambdaShape(memory_mb=128))
    big = lambda_cost(requests=1000, provisioned_hours=1.0, shape=LambdaShape(memory_mb=256))

    assert big["lambda_provisioned_holding"] == pytest.approx(2 * small["lambda_provisioned_holding"])


@pytest.mark.parametrize("kwargs", [dict(requests=-1, provisioned_hours=0.0), dict(requests=1, provisioned_hours=-1.0),
                                    dict(requests=1, provisioned_hours=0.0, provisioned_fraction=1.5)])
def test_nonsense_lambda_inputs_are_refused(kwargs) -> None:
    with pytest.raises(ValueError):
        lambda_cost(**kwargs)


def test_a_nonsense_lambda_shape_is_refused() -> None:
    with pytest.raises(ValueError):
        LambdaShape(memory_mb=0)


# --- a whole run -------------------------------------------------------------------------------------------


def test_a_run_adds_up_its_parts() -> None:
    capacity = {"catalog_service": [(0.0, 1.0), (1800.0, 3.0)], "thumbnail_service": [(900.0, 1.0), (2700.0, 0.0)]}

    out = run_cost(capacity=capacity, end=HOUR, stack_start=0.0, thumbnail_requests=50_000, provisioned_fraction=0.2)

    assert out["instance_hours"] == pytest.approx(0.5 * 1 + 0.5 * 3)
    assert out["provisioned_unit_hours"] == pytest.approx(0.5)
    assert out["stack_hours"] == pytest.approx(1.0)
    assert out["total_usd"] == pytest.approx(sum(out["parts_usd"].values()))


def test_a_static_fleet_costs_more_than_a_run_that_scales_down() -> None:
    static = {"catalog_service": [(0.0, 4.0)]}
    scaled = {"catalog_service": [(0.0, 1.0), (1800.0, 4.0), (2400.0, 1.0)]}

    a = run_cost(capacity=static, end=HOUR, stack_start=0.0, thumbnail_requests=0)
    b = run_cost(capacity=scaled, end=HOUR, stack_start=0.0, thumbnail_requests=0)

    assert a["total_usd"] > b["total_usd"]


def test_the_result_says_it_is_an_estimate_with_unverified_prices() -> None:
    out = run_cost(capacity={}, end=10.0, stack_start=0.0, thumbnail_requests=0)

    assert out["prices_verified"] is False and "estimate" in out["note"] and out["prices_as_of"] == "2026-10-13"
    assert PRICES.verified is False


def test_a_run_cannot_end_before_its_stack_started() -> None:
    with pytest.raises(ValueError, match="before the stack"):
        run_cost(capacity={}, end=5.0, stack_start=10.0, thumbnail_requests=0)


def test_corrected_prices_flow_through_to_the_total() -> None:
    capacity = {"catalog_service": [(0.0, 1.0)]}
    cheap = run_cost(capacity=capacity, end=HOUR, stack_start=0.0, thumbnail_requests=0, cpu_utilization=0.0)
    dear = run_cost(
        capacity=capacity, end=HOUR, stack_start=0.0, thumbnail_requests=0, cpu_utilization=0.0,
        prices=Prices(t3_micro_per_hour=0.0208, verified=True),
    )

    assert dear["parts_usd"]["ec2_instances"] == pytest.approx(2 * cheap["parts_usd"]["ec2_instances"])
    assert dear["prices_verified"] is True


# --- the pre-run estimate (D9) -----------------------------------------------------------------------------


def test_the_estimate_is_an_upper_bound_for_any_run_within_the_caps() -> None:
    rng = random.Random(5)
    scenario_minutes = 90.0
    bound = estimate_before_run(scenario_minutes=scenario_minutes, expected_thumbnail_requests=60_000)
    stack_seconds = (scenario_minutes + 25.0) * 60
    for _ in range(200):
        steps = sorted(rng.uniform(0, stack_seconds) for _ in range(rng.randint(1, 6)))
        capacity = {
            "catalog_service": [(t, float(rng.randint(1, 10))) for t in steps],
            "thumbnail_service": [(t, float(rng.randint(0, 10))) for t in steps],
        }
        actual = run_cost(
            capacity=capacity, end=stack_seconds, stack_start=0.0, thumbnail_requests=rng.randint(0, 60_000),
            provisioned_fraction=rng.random(),
        )
        assert actual["total_usd"] <= bound["total_usd"] + 1e-9


def test_the_estimate_includes_setup_and_teardown_time() -> None:
    short = estimate_before_run(scenario_minutes=60, setup_minutes=0)
    padded = estimate_before_run(scenario_minutes=60, setup_minutes=30)

    assert padded["total_usd"] > short["total_usd"] and padded["stack_hours"] == pytest.approx(1.5)


def test_the_estimate_for_a_plausible_run_is_a_few_dollars_at_most() -> None:
    bound = estimate_before_run(scenario_minutes=120, expected_thumbnail_requests=100_000)

    assert 0 < bound["total_usd"] < 5.0  # consistent with the $2-5 programme budget in decision D9


@pytest.mark.parametrize("kwargs", [dict(scenario_minutes=0), dict(scenario_minutes=10, setup_minutes=-1),
                                    dict(scenario_minutes=10, max_instances=11), dict(scenario_minutes=10, max_instances=0),
                                    dict(scenario_minutes=10, max_provisioned=11)])
def test_the_estimate_refuses_nonsense_and_over_cap_inputs(kwargs) -> None:
    with pytest.raises(ValueError):
        estimate_before_run(**kwargs)


# --- capacity log ------------------------------------------------------------------------------------------


def test_the_capacity_log_is_read_sorted_and_split_by_service(tmp_path: Path) -> None:
    rows = [
        {"t": 200.0, "service": "catalog_service", "instances": 3},
        {"t": 100.0, "service": "catalog_service", "instances": 1},
        {"t": 150.0, "service": "thumbnail_service", "provisioned": 1},
    ]
    path = tmp_path / "cap.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n\n", encoding="utf-8")

    log = read_capacity_log(path)

    assert log["catalog_service"] == [(100.0, 1.0), (200.0, 3.0)]
    assert log["thumbnail_service"] == [(150.0, 1.0)]


def test_an_unknown_service_in_the_capacity_log_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cap.jsonl"
    path.write_text(json.dumps({"t": 1.0, "service": "mystery", "instances": 1}) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown service"):
        read_capacity_log(path)


def test_the_cost_model_needs_no_aws_database_or_network() -> None:
    source = Path(cost.__file__).read_text(encoding="utf-8")

    for forbidden in ("import boto3", "import psycopg2", "import requests", "import socket"):
        assert forbidden not in source
