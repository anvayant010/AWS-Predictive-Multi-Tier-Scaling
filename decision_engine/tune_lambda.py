"""Tune the Lambda tier's `provision_threshold_per_min` without peeking at the test week.

The first dry-run showed the Lambda policy covering ~88% of the minutes that needed capacity but
holding capacity in ~34% of minutes against ~7% needed. This picks a threshold using a *separate*
validation week, then runs the chosen value once on the untouched test week.

Split (30 days): train = days 0-16, validation = days 16-23, test = last 7 days. The forecaster used
for the sweep is fitted on train only; the test run refits on everything before the test week.

Selection rule (fixed BEFORE looking at any result): pick the LOWEST threshold in the grid whose
provisioned-minutes / needed-minutes ratio is <= MAX_PROVISIONED_RATIO on validation (lowest =
most coverage that still meets the cost limit). Ground truth ("needed") uses a fixed
truth threshold, independent of the threshold being tuned; a second truth threshold is reported as
a sensitivity check but does not drive the choice.

Run: python -m decision_engine.tune_lambda   (local DB populated; no AWS)
Env: TUNE_THRESHOLDS (default 20,35,50,75), MAX_PROVISIONED_RATIO (default 3.0)
"""

from __future__ import annotations

import os
from dataclasses import replace

import pandas as pd

from decision_engine.dry_run import FORECAST_STEPS, LAMBDA_TRUTH_THRESHOLD, needed_capacity, replay
from decision_engine.engine import ROUTES, Route
from decision_engine.models import Aggressiveness
from forecasting.data_pipeline.db import get_connection
from forecasting.predictor import GbmForecaster
from forecasting.train import load_series

SERVICE = "thumbnail_service"
TEST_DAYS = 7
VALIDATION_DAYS = 7
SENSITIVITY_TRUTH_THRESHOLD = 50.0


def route_with_threshold(threshold: float) -> Route:
    base = ROUTES[SERVICE]
    assert base.lambda_ is not None
    return replace(base, lambda_=replace(base.lambda_, provision_threshold_per_min=threshold))


def evaluate_threshold(decisions: pd.DataFrame, route: Route, truth_threshold: float) -> dict[str, float]:
    """Grade one replay against a given truth threshold (recomputed from the recorded actual peaks)."""
    needed = decisions["actual_peak"].map(lambda p: needed_capacity(route, p, truth_threshold=truth_threshold))
    desired = decisions["desired"]
    needed_mask = needed > 0
    provisioned_mask = desired > 0
    n_needed = int(needed_mask.sum())
    return {
        "n_needed": n_needed,
        "n_provisioned": int(provisioned_mask.sum()),
        "coverage": float((desired[needed_mask] >= needed[needed_mask]).mean()) if n_needed else float("nan"),
        "provisioned_ratio": float(provisioned_mask.sum() / n_needed) if n_needed else float("inf"),
    }


def choose_threshold(rows: list[dict[str, float]], max_ratio: float) -> float | None:
    """Lowest threshold whose provisioned/needed ratio is within the limit (None if none qualifies)."""
    qualifying = [r["threshold"] for r in rows if r["provisioned_ratio"] <= max_ratio]
    return min(qualifying) if qualifying else None


def split_points(series: pd.Series) -> tuple[pd.Timestamp, pd.Timestamp]:
    """(validation_start, test_start): the last TEST_DAYS are test, the VALIDATION_DAYS before that validation."""
    test_start = series.index.max() - pd.Timedelta(days=TEST_DAYS)
    return test_start - pd.Timedelta(days=VALIDATION_DAYS), test_start


def origins_between(series: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """1-minute-cadence origins in (start, end] that still have a full window of actuals after them."""
    idx = series.index
    last_ok = idx[-FORECAST_STEPS - 2]
    return idx[(idx > start) & (idx <= min(end, last_ok))]


def sweep(
    series: pd.Series,
    forecaster: GbmForecaster,
    origins: pd.DatetimeIndex,
    thresholds: list[float],
) -> list[dict[str, float]]:
    forecasts = forecaster.forecast_many(series, origins)
    rows = []
    for threshold in thresholds:
        route = route_with_threshold(threshold)
        decisions = replay(series, forecasts, route, Aggressiveness.BALANCED)
        primary = evaluate_threshold(decisions, route, LAMBDA_TRUTH_THRESHOLD)
        sensitivity = evaluate_threshold(decisions, route, SENSITIVITY_TRUTH_THRESHOLD)
        rows.append(
            {
                "threshold": threshold,
                **primary,
                "coverage_truth50": sensitivity["coverage"],
                "ratio_truth50": sensitivity["provisioned_ratio"],
            }
        )
    return rows


def format_rows(rows: list[dict[str, float]]) -> str:
    header = (
        f"  {'threshold':>9}{'needed min':>12}{'provisioned':>13}{'coverage':>10}{'prov/needed':>13}"
        f"{'cov (truth 50)':>16}{'ratio (truth 50)':>18}"
    )
    lines = [header]
    for r in rows:
        lines.append(
            f"  {r['threshold']:>9g}{r['n_needed']:>12d}{r['n_provisioned']:>13d}{r['coverage']:>10.1%}"
            f"{r['provisioned_ratio']:>13.2f}{r['coverage_truth50']:>16.1%}{r['ratio_truth50']:>18.2f}"
        )
    return "\n".join(lines)


def main() -> None:
    thresholds = [float(t) for t in os.environ.get("TUNE_THRESHOLDS", "20,35,50,75").split(",")]
    max_ratio = float(os.environ.get("MAX_PROVISIONED_RATIO", "3.0"))

    series = load_series(get_connection(), SERVICE, "Invocations")
    val_start, test_start = split_points(series)

    print(f"split: train < {val_start:%Y-%m-%d %H:%M} <= validation < {test_start:%Y-%m-%d %H:%M} <= test")
    print(f"rule (fixed in advance): lowest threshold with provisioned/needed <= {max_ratio:g} on validation")

    tuner = GbmForecaster.fit(SERVICE, series, val_start)
    val_rows = sweep(series, tuner, origins_between(series, val_start, test_start), thresholds)
    print("\nVALIDATION (model fitted on train only):")
    print(format_rows(val_rows))

    chosen = choose_threshold(val_rows, max_ratio)
    if chosen is None:
        print(f"\nno threshold in the grid meets ratio <= {max_ratio:g}; nothing chosen, test week left untouched")
        return
    print(f"\nchosen threshold: {chosen:g}")

    final = GbmForecaster.fit(SERVICE, series, test_start)
    test_origins = origins_between(series, test_start, series.index.max())
    test_rows = sweep(series, final, test_origins, sorted({LAMBDA_TRUTH_THRESHOLD, chosen}))
    print("\nTEST WEEK (model refitted on everything before it; chosen threshold vs. the current 20):")
    print(format_rows(test_rows))


if __name__ == "__main__":
    main()
