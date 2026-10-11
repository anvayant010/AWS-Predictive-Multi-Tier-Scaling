from pathlib import Path

import pandas as pd
import pytest

import decision_engine.tune_lambda as tune
from decision_engine.dry_run import needed_capacity
from decision_engine.engine import ROUTES
from decision_engine.models import Forecast
from decision_engine.policy import LambdaPolicyConfig
from decision_engine.tune_lambda import (
    choose_threshold,
    evaluate_threshold,
    origins_between,
    route_with_threshold,
    split_points,
    sweep,
)

THUMBNAIL = ROUTES["thumbnail_service"]


def flat_series(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.date_range("2026-06-01", periods=len(values), freq="min"), dtype="float64")


class StubForecaster:
    """Returns the same hand-made forecast shape for every origin (no model fitting)."""

    def __init__(self, p90_level: float) -> None:
        self.p90_level = p90_level

    def forecast_many(self, series: pd.Series, origins: pd.DatetimeIndex) -> list[Forecast]:
        n, level = 15, self.p90_level
        return [
            Forecast("thumbnail_service", o.to_pydatetime(), tuple([0.0] * n), tuple([level / 2] * n), tuple([level] * n))
            for o in origins
        ]


def test_ground_truth_does_not_move_with_the_policy_threshold() -> None:
    # The whole point of the fix: grading must not depend on the trigger being tuned.
    for policy_threshold in (20, 35, 50, 75):
        route = route_with_threshold(policy_threshold)

        assert needed_capacity(route, 30.0) == 1  # default truth threshold 20
        assert needed_capacity(route, 10.0) == 0
        assert needed_capacity(route, 30.0, truth_threshold=50.0) == 0


def test_route_with_threshold_changes_only_the_threshold() -> None:
    route = route_with_threshold(75)

    assert route.lambda_.provision_threshold_per_min == 75
    assert route.lambda_.max_concurrency == THUMBNAIL.lambda_.max_concurrency
    assert route.classifier == THUMBNAIL.classifier
    # The shared route is not mutated: it still carries the policy default, not our 75.
    assert THUMBNAIL.lambda_.provision_threshold_per_min == LambdaPolicyConfig().provision_threshold_per_min
    assert THUMBNAIL.lambda_.provision_threshold_per_min != 75


def test_evaluate_threshold_counts_coverage_and_provisioned_ratio() -> None:
    decisions = pd.DataFrame(
        {
            "actual_peak": [5.0, 100.0, 100.0, 5.0, 5.0],  # 2 minutes truly need capacity
            "desired": [0, 1, 0, 1, 1],  # covers one of them, wastes two
        }
    )

    result = evaluate_threshold(decisions, THUMBNAIL, truth_threshold=20.0)

    assert result["n_needed"] == 2
    assert result["n_provisioned"] == 3
    assert result["coverage"] == 0.5
    assert result["provisioned_ratio"] == 1.5


def test_evaluate_threshold_handles_nothing_needed() -> None:
    decisions = pd.DataFrame({"actual_peak": [1.0, 2.0], "desired": [0, 1]})

    result = evaluate_threshold(decisions, THUMBNAIL, truth_threshold=20.0)

    assert result["provisioned_ratio"] == float("inf")


def test_choose_threshold_picks_the_lowest_one_within_the_cost_limit() -> None:
    rows = [
        {"threshold": 20, "provisioned_ratio": 5.0},
        {"threshold": 35, "provisioned_ratio": 3.4},
        {"threshold": 50, "provisioned_ratio": 2.9},
        {"threshold": 75, "provisioned_ratio": 1.2},
    ]

    assert choose_threshold(rows, max_ratio=3.0) == 50
    assert choose_threshold(rows, max_ratio=6.0) == 20  # everything qualifies: keep the most coverage
    assert choose_threshold(rows, max_ratio=1.0) is None  # nothing qualifies: choose nothing


def test_split_points_leave_the_last_week_for_test_and_the_week_before_for_validation() -> None:
    series = flat_series([1.0] * (30 * 1440))

    val_start, test_start = split_points(series)

    assert series.index.max() - test_start == pd.Timedelta(days=7)
    assert test_start - val_start == pd.Timedelta(days=7)


def test_origins_between_stay_inside_the_window_and_keep_a_full_future_window() -> None:
    series = flat_series([1.0] * 500)
    start, end = series.index[100], series.index[200]

    origins = origins_between(series, start, end)
    assert origins.min() > start and origins.max() <= end

    tail = origins_between(series, series.index[400], series.index[-1])
    assert tail.max() <= series.index[-tune.FORECAST_STEPS - 2]


def test_sweep_keeps_needed_fixed_and_provisions_less_as_the_threshold_rises() -> None:
    series = flat_series([2.0] * 300 + [400.0] * 20 + [2.0] * 300)
    origins = series.index[250:420:5]
    stub = StubForecaster(p90_level=40.0)  # forecast p90 of 40 inv/min everywhere

    rows = sweep(series, stub, origins, [20.0, 35.0, 50.0, 75.0])

    assert len({r["n_needed"] for r in rows}) == 1  # same ground truth for every threshold
    provisioned = [r["n_provisioned"] for r in rows]
    assert provisioned == sorted(provisioned, reverse=True)
    assert provisioned[0] > 0  # 40 >= 20 and 35 triggers
    assert provisioned[2] == 0  # 40 < 50 does not


def test_tuning_code_never_references_aws() -> None:
    assert "boto3" not in Path(tune.__file__).read_text()


def test_the_provisioning_threshold_is_tuned_without_hysteresis_so_results_stay_reproducible() -> None:
    # ROUTES now carries a tuned release threshold; the threshold sweep must not inherit it.
    assert ROUTES["thumbnail_service"].lambda_.release_threshold_per_min is not None
    assert route_with_threshold(50).lambda_.release_threshold_per_min is None
