import math
import random
from datetime import datetime, timezone

import pytest

from decision_engine.models import Aggressiveness, Forecast, LoadShape
from decision_engine.policy import (
    ABSOLUTE_MAX_INSTANCES,
    ABSOLUTE_MAX_LAMBDA_CONCURRENCY,
    Ec2PolicyConfig,
    LambdaPolicyConfig,
    ec2_desired_capacity,
    lambda_provisioned_concurrency,
)

ORIGIN = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def make_forecast(p90: list[float], p50: list[float] | None = None, service: str = "svc") -> Forecast:
    p50 = p50 if p50 is not None else [v * 0.6 for v in p90]
    p10 = [v * 0.5 for v in p50]
    return Forecast(service, ORIGIN, tuple(p10), tuple(p50), tuple(p90))


# --- Forecast validation: bad input must fail loudly, never become an action -------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_forecast_rejects_invalid_values(bad: float) -> None:
    with pytest.raises(ValueError):
        Forecast("svc", ORIGIN, (1.0,), (1.0,), (bad,))


def test_forecast_rejects_empty_mismatched_and_unordered() -> None:
    with pytest.raises(ValueError):
        Forecast("svc", ORIGIN, (), (), ())
    with pytest.raises(ValueError):
        Forecast("svc", ORIGIN, (1.0,), (1.0, 2.0), (2.0,))
    with pytest.raises(ValueError):
        Forecast("svc", ORIGIN, (5.0,), (3.0,), (4.0,))


# --- EC2 ---------------------------------------------------------------------------------------


def test_ec2_sizes_to_p90_peak_over_per_instance_capacity() -> None:
    forecast = make_forecast([60.0, 120.0, 90.0])
    config = Ec2PolicyConfig(capacity_per_instance_rpm=50)

    decision = ec2_desired_capacity(forecast, current_capacity=1, config=config)

    assert decision.desired == math.ceil(120 / 50) == 3
    assert decision.quantile == "p90"
    assert decision.target == "ec2_asg"
    assert not decision.cap_applied


def test_ec2_ignores_demand_beyond_the_lookahead_window() -> None:
    forecast = make_forecast([10.0] * 5 + [1000.0] * 5)
    config = Ec2PolicyConfig(capacity_per_instance_rpm=50, lookahead_minutes=5)

    assert ec2_desired_capacity(forecast, 1, config).desired == 1


def test_ec2_aggressiveness_orders_cost_below_balanced_below_latency() -> None:
    forecast = make_forecast(p90=[200.0] * 5, p50=[100.0] * 5)
    config = Ec2PolicyConfig(capacity_per_instance_rpm=50)

    cost = ec2_desired_capacity(forecast, 1, config, Aggressiveness.COST_OPTIMIZED).desired
    balanced = ec2_desired_capacity(forecast, 1, config, Aggressiveness.BALANCED).desired
    latency = ec2_desired_capacity(forecast, 1, config, Aggressiveness.LATENCY_OPTIMIZED).desired

    assert cost < balanced < latency
    assert (cost, balanced, latency) == (2, 4, 5)


def test_ec2_never_exceeds_max_even_for_an_absurd_forecast() -> None:
    forecast = make_forecast([1e12] * 10)
    config = Ec2PolicyConfig(max_instances=6)

    decision = ec2_desired_capacity(forecast, 1, config, Aggressiveness.LATENCY_OPTIMIZED)

    assert decision.desired == 6
    assert decision.cap_applied is True
    assert decision.uncapped > 6  # the report still shows what the forecast alone wanted


def test_ec2_holds_the_minimum_when_demand_is_zero() -> None:
    forecast = make_forecast([0.0] * 5)

    assert ec2_desired_capacity(forecast, 4, Ec2PolicyConfig(min_instances=2, max_scale_down_step=10)).desired == 2


def test_ec2_releases_capacity_gradually() -> None:
    forecast = make_forecast([0.0] * 5)
    config = Ec2PolicyConfig(min_instances=1, max_scale_down_step=1)

    assert ec2_desired_capacity(forecast, 8, config).desired == 7


def test_ec2_scales_up_immediately_not_gradually() -> None:
    forecast = make_forecast([400.0] * 5)

    assert ec2_desired_capacity(forecast, 1, Ec2PolicyConfig(capacity_per_instance_rpm=50)).desired == 8


def test_ec2_cap_holds_even_if_current_capacity_is_already_above_it() -> None:
    forecast = make_forecast([0.0] * 5)
    config = Ec2PolicyConfig(max_instances=4, max_scale_down_step=1)

    # Something set the ASG to 9 out-of-band; a gentle step down would land at 8, still over cap.
    assert ec2_desired_capacity(forecast, 9, config).desired <= 4


def test_ec2_caps_hold_for_any_forecast_current_capacity_and_aggressiveness() -> None:
    rng = random.Random(0)
    for _ in range(500):
        n = rng.randint(1, 15)
        p90 = [rng.choice([0.0, rng.uniform(0, 1e6)]) for _ in range(n)]
        config = Ec2PolicyConfig(
            capacity_per_instance_rpm=rng.uniform(1, 200),
            min_instances=rng.randint(0, 3),
            max_instances=rng.randint(3, ABSOLUTE_MAX_INSTANCES),
            lookahead_minutes=rng.randint(1, 15),
            max_scale_down_step=rng.randint(1, 5),
        )
        decision = ec2_desired_capacity(
            make_forecast(p90), rng.randint(0, 40), config, rng.choice(list(Aggressiveness))
        )
        assert config.min_instances <= decision.desired <= config.max_instances


def test_ec2_config_cannot_raise_max_above_the_absolute_ceiling() -> None:
    with pytest.raises(ValueError):
        Ec2PolicyConfig(max_instances=ABSOLUTE_MAX_INSTANCES + 1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capacity_per_instance_rpm": 0},
        {"min_instances": 5, "max_instances": 3},
        {"min_instances": -1},
        {"lookahead_minutes": 0},
        {"max_scale_down_step": 0},
    ],
)
def test_ec2_config_rejects_nonsense(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        Ec2PolicyConfig(**kwargs)


# --- Lambda ------------------------------------------------------------------------------------


def test_lambda_formula_divides_per_minute_rate_by_sixty() -> None:
    # 600 inv/min = 10 inv/s; at 1.0 s each that is 10 concurrent executions (x1.0 safety).
    forecast = make_forecast([600.0] * 3, p50=[600.0] * 3)
    config = LambdaPolicyConfig(avg_duration_seconds=1.0, max_concurrency=10)

    decision = lambda_provisioned_concurrency(forecast, 0, config, Aggressiveness.COST_OPTIMIZED)

    assert decision.desired == 10
    assert decision.uncapped == 10


def test_lambda_cost_optimized_sizes_on_p50_not_p90() -> None:
    forecast = make_forecast(p90=[600.0] * 3, p50=[360.0] * 3)
    config = LambdaPolicyConfig(avg_duration_seconds=1.0, max_concurrency=10)

    decision = lambda_provisioned_concurrency(forecast, 0, config, Aggressiveness.COST_OPTIMIZED)

    assert decision.quantile == "p50"
    assert decision.desired == 6  # 360 / 60 * 1.0


def test_lambda_realistic_burst_needs_only_a_little_concurrency() -> None:
    # The corrected formula: 400 inv/min at 0.1 s is ~0.67 concurrent, so 1 after rounding up
    # (the unfixed formula would have asked for 40).
    forecast = make_forecast([400.0] * 3, p50=[400.0] * 3)

    decision = lambda_provisioned_concurrency(
        forecast, 0, LambdaPolicyConfig(), Aggressiveness.COST_OPTIMIZED
    )

    assert decision.desired == 1


def test_lambda_provisions_nothing_below_the_threshold() -> None:
    forecast = make_forecast([15.0] * 3)

    decision = lambda_provisioned_concurrency(forecast, 2, LambdaPolicyConfig(provision_threshold_per_min=20))

    assert decision.desired == 0
    assert decision.uncapped == 0


def test_lambda_safety_multiplier_grows_with_aggressiveness() -> None:
    # p50 == p90 so only the safety factor differs: 1200/60 * 0.2 = 4.0 base concurrency.
    forecast = make_forecast(p90=[1200.0] * 3, p50=[1200.0] * 3)
    config = LambdaPolicyConfig(avg_duration_seconds=0.2, max_concurrency=10)

    def desired(aggressiveness: Aggressiveness) -> int:
        return lambda_provisioned_concurrency(forecast, 0, config, aggressiveness).desired

    assert desired(Aggressiveness.COST_OPTIMIZED) == 4
    assert desired(Aggressiveness.BALANCED) == 6  # x1.5
    assert desired(Aggressiveness.LATENCY_OPTIMIZED) == 8  # x2.0


def test_lambda_never_exceeds_max_even_for_an_absurd_forecast() -> None:
    forecast = make_forecast([1e12] * 3)
    config = LambdaPolicyConfig(max_concurrency=3)

    decision = lambda_provisioned_concurrency(forecast, 0, config, Aggressiveness.LATENCY_OPTIMIZED)

    assert decision.desired == 3
    assert decision.cap_applied is True
    assert decision.uncapped > 3


def test_lambda_caps_hold_for_any_forecast_and_aggressiveness() -> None:
    rng = random.Random(1)
    for _ in range(500):
        n = rng.randint(1, 15)
        p90 = [rng.choice([0.0, rng.uniform(0, 1e7)]) for _ in range(n)]
        config = LambdaPolicyConfig(
            avg_duration_seconds=rng.uniform(0.01, 5),
            min_concurrency=rng.randint(0, 2),
            max_concurrency=rng.randint(2, ABSOLUTE_MAX_LAMBDA_CONCURRENCY),
            lookahead_minutes=rng.randint(1, 15),
            provision_threshold_per_min=rng.uniform(0, 100),
        )
        decision = lambda_provisioned_concurrency(
            make_forecast(p90), rng.randint(0, 20), config, rng.choice(list(Aggressiveness))
        )
        assert config.min_concurrency <= decision.desired <= config.max_concurrency


def test_lambda_config_cannot_raise_max_above_the_absolute_ceiling() -> None:
    with pytest.raises(ValueError):
        LambdaPolicyConfig(max_concurrency=ABSOLUTE_MAX_LAMBDA_CONCURRENCY + 1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"avg_duration_seconds": 0},
        {"min_concurrency": 4, "max_concurrency": 2},
        {"lookahead_minutes": 0},
        {"provision_threshold_per_min": -1},
    ],
)
def test_lambda_config_rejects_nonsense(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        LambdaPolicyConfig(**kwargs)


# --- EC2 mode: bursty windows are not chased --------------------------------------------------


def test_ec2_bursty_shape_sizes_to_the_median_not_the_spike() -> None:
    # A short spike to 400 req/min inside a 40 req/min baseline: boot time (3-6 min) means a new
    # instance could not serve it anyway, so size for the baseline.
    forecast = make_forecast([40.0] * 7 + [400.0] * 3)
    config = Ec2PolicyConfig(capacity_per_instance_rpm=50)

    chasing = ec2_desired_capacity(forecast, 1, config, shape=LoadShape.SUSTAINED)
    ignoring = ec2_desired_capacity(forecast, 1, config, shape=LoadShape.BURSTY)

    assert chasing.desired == 8
    assert ignoring.desired == 1
    assert ignoring.demand == 40.0
    assert "ignoring transient spike" in ignoring.reason


def test_ec2_shape_defaults_to_sustained_so_existing_behaviour_is_unchanged() -> None:
    forecast = make_forecast([40.0] * 7 + [400.0] * 3)
    config = Ec2PolicyConfig(capacity_per_instance_rpm=50)

    default = ec2_desired_capacity(forecast, 1, config)
    explicit = ec2_desired_capacity(forecast, 1, config, shape=LoadShape.SUSTAINED)

    assert default == explicit
    assert default.desired == 8


def test_ec2_caps_hold_for_every_shape() -> None:
    rng = random.Random(2)
    for _ in range(300):
        p90 = [rng.choice([0.0, rng.uniform(0, 1e6)]) for _ in range(rng.randint(1, 15))]
        config = Ec2PolicyConfig(min_instances=1, max_instances=rng.randint(2, ABSOLUTE_MAX_INSTANCES))
        decision = ec2_desired_capacity(
            make_forecast(p90), rng.randint(0, 40), config, rng.choice(list(Aggressiveness)),
            shape=rng.choice(list(LoadShape)),
        )
        assert config.min_instances <= decision.desired <= config.max_instances
