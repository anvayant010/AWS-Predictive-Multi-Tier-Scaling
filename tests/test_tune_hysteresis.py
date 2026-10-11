from datetime import datetime
from pathlib import Path

import pandas as pd

import decision_engine.tune_hysteresis as tune
from decision_engine.engine import ROUTES
from decision_engine.models import Forecast
from decision_engine.tune_hysteresis import (
    choose_release,
    evaluate_run,
    route_with_release,
    sweep,
)

THUMBNAIL = ROUTES["thumbnail_service"]
PROVISION = THUMBNAIL.lambda_.provision_threshold_per_min


def row(release: float, ratio: float, coverage: float, creates: float) -> dict:
    return {"release": release, "provisioned_ratio": ratio, "coverage": coverage, "creates": creates}


def flat_series(n: int) -> pd.Series:
    return pd.Series(2.0, index=pd.date_range("2026-06-01", periods=n, freq="min"))


class HoveringForecaster:
    """p90 hovers between 25 and 40: above the release threshold throughout, but crossing the 35 trigger."""

    def forecast_many(self, series: pd.Series, origins: pd.DatetimeIndex) -> list[Forecast]:
        out = []
        for i, o in enumerate(origins):
            level = 40.0 if i % 2 == 0 else 25.0
            n = 15
            out.append(Forecast("thumbnail_service", o.to_pydatetime(), tuple([0.0] * n), tuple([level] * n), tuple([level] * n)))
        return out


# --- the pre-committed selection rule ----------------------------------------------------------------


def test_picks_the_candidate_with_the_fewest_creates_among_those_within_the_limits() -> None:
    rows = [
        row(35, ratio=5.0, coverage=0.88, creates=300),  # baseline
        row(30, ratio=5.2, coverage=0.88, creates=200),
        row(25, ratio=5.4, coverage=0.88, creates=120),  # fewest creates, still within +10% cost
        row(20, ratio=7.0, coverage=0.89, creates=60),  # fewer creates but too expensive
    ]

    assert choose_release(rows, PROVISION) == 25


def test_a_candidate_that_loses_too_much_coverage_is_rejected() -> None:
    rows = [row(35, 5.0, 0.88, 300), row(25, 4.9, 0.85, 100)]  # cheaper and calmer, but 3 points of coverage lost

    assert choose_release(rows, PROVISION) == 35


def test_when_nothing_beats_the_baseline_the_answer_is_no_hysteresis() -> None:
    rows = [row(35, 5.0, 0.88, 300), row(30, 6.5, 0.88, 250), row(25, 8.0, 0.88, 200)]

    assert choose_release(rows, PROVISION) == 35


def test_ties_prefer_the_higher_release_threshold_the_smaller_change() -> None:
    rows = [row(35, 5.0, 0.88, 300), row(30, 5.0, 0.88, 100), row(25, 5.0, 0.88, 100)]

    assert choose_release(rows, PROVISION) == 30


def test_the_baseline_always_qualifies() -> None:
    assert choose_release([row(35, 5.0, 0.88, 300)], PROVISION) == 35


# --- plumbing ----------------------------------------------------------------------------------------------


def test_route_with_release_changes_only_the_release_threshold() -> None:
    route = route_with_release(20.0)

    assert route.lambda_.release_threshold_per_min == 20.0
    assert route.lambda_.provision_threshold_per_min == PROVISION
    assert route.lambda_.max_concurrency == THUMBNAIL.lambda_.max_concurrency


def test_the_shared_route_is_not_mutated_by_building_a_candidate() -> None:
    before = THUMBNAIL.lambda_.release_threshold_per_min

    route_with_release(12.0)

    assert THUMBNAIL.lambda_.release_threshold_per_min == before


def test_hysteresis_cuts_create_cycles_on_a_hovering_forecast_end_to_end() -> None:
    series = flat_series(300)
    origins = series.index[50:200]

    rows = sweep(series, HoveringForecaster(), origins, [PROVISION, 20.0])
    baseline, with_hysteresis = rows

    assert baseline["creates"] > with_hysteresis["creates"]
    assert with_hysteresis["creates"] <= 2  # stays on once on: 25 never falls below the release threshold of 20
    assert with_hysteresis["median_on_run"] > baseline["median_on_run"]


def test_evaluate_run_reports_the_fields_the_rule_needs() -> None:
    decisions = pd.DataFrame({"actual_peak": [5.0, 100.0, 100.0, 5.0], "desired": [0, 1, 1, 0]})

    result = evaluate_run(decisions, THUMBNAIL, days=1.0)

    assert {"coverage", "provisioned_ratio", "creates", "creates_per_day", "median_on_run"} <= set(result)
    assert result["coverage"] == 1.0 and result["creates"] == 1.0


def test_tuning_code_never_references_aws() -> None:
    assert "boto3" not in Path(tune.__file__).read_text(encoding="utf-8")
