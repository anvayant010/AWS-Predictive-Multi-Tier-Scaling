"""Choose the Lambda tier's release threshold (trigger hysteresis) without peeking at the test week.

Background: even with damping, the Lambda tier still created its provisioned-concurrency config ~2x/hour
because the forecast p90 hovers around the 35 inv/min trigger. Hysteresis keeps capacity ON until the
forecast falls below a LOWER release threshold. Lowering it reduces on/off cycling but also holds capacity
longer, so there is a real cost trade-off; this picks the value by a rule fixed BEFORE seeing results.

Split (30 days): train = days 0-16, validation = days 16-23, test = last 7 days (same as tune_lambda).
Everything is evaluated with the production damping ON (DEFAULT_DAMPING), balanced aggressiveness, and the
fixed ground truth (LAMBDA_TRUTH_THRESHOLD), exactly as the live loop would run.

Candidates: RELEASE_GRID, where the provisioning threshold itself (35) means "no hysteresis" = the baseline.

Selection rule (fixed in advance): among candidates whose provisioned-minutes / needed-minutes ratio is at
most MAX_COST_VS_BASELINE x the baseline's AND whose coverage is at most MAX_COVERAGE_LOSS below the
baseline's, pick the one with the FEWEST create cycles (ties: the higher release threshold, i.e. the
smaller change). The baseline always qualifies, so if nothing beats it the answer is "no hysteresis".

Run: python -m decision_engine.tune_hysteresis   (local DB populated; no AWS)
Env: RELEASE_GRID (default 35,30,25,20,15)
"""

from __future__ import annotations

import os
from dataclasses import replace

import pandas as pd

from decision_engine.damping import DEFAULT_DAMPING
from decision_engine.dry_run import LAMBDA_TRUTH_THRESHOLD, churn_stats, replay
from decision_engine.engine import ROUTES, Route
from decision_engine.models import Aggressiveness
from decision_engine.tune_lambda import SENSITIVITY_TRUTH_THRESHOLD, evaluate_threshold, origins_between, split_points
from forecasting.data_pipeline.db import get_connection
from forecasting.predictor import GbmForecaster
from forecasting.train import load_series

SERVICE = "thumbnail_service"
MAX_COST_VS_BASELINE = 1.10
MAX_COVERAGE_LOSS = 0.01
DEFAULT_GRID = (35.0, 30.0, 25.0, 20.0, 15.0)


def route_with_release(release: float) -> Route:
    base = ROUTES[SERVICE]
    assert base.lambda_ is not None
    return replace(base, lambda_=replace(base.lambda_, release_threshold_per_min=release))


def evaluate_run(decisions: pd.DataFrame, route: Route, days: float) -> dict[str, float]:
    graded = evaluate_threshold(decisions, route, LAMBDA_TRUTH_THRESHOLD)
    sensitivity = evaluate_threshold(decisions, route, SENSITIVITY_TRUTH_THRESHOLD)
    churn = churn_stats(decisions.assign(service=SERVICE, aggressiveness="balanced")).iloc[0]
    return {
        "coverage": graded["coverage"],
        "provisioned_ratio": graded["provisioned_ratio"],
        "n_provisioned": graded["n_provisioned"],
        "creates": float(churn["creates"]),
        "creates_per_day": float(churn["creates"]) / days,
        "median_on_run": float(churn["median_on_run"]),
        "coverage_truth50": sensitivity["coverage"],
    }


def sweep(series: pd.Series, forecaster: GbmForecaster, origins: pd.DatetimeIndex, releases: list[float]) -> list[dict[str, float]]:
    forecasts = forecaster.forecast_many(series, origins)
    days = len(origins) / 1440
    rows = []
    for release in releases:
        route = route_with_release(release)
        decisions = replay(series, forecasts, route, Aggressiveness.BALANCED, damping=DEFAULT_DAMPING[SERVICE])
        rows.append({"release": release, **evaluate_run(decisions, route, days)})
    return rows


def choose_release(rows: list[dict[str, float]], provision_threshold: float) -> float:
    """Apply the pre-committed rule. The baseline (release == provision threshold) always qualifies."""
    baseline = next(r for r in rows if r["release"] == provision_threshold)
    qualifying = [
        r
        for r in rows
        if r["provisioned_ratio"] <= baseline["provisioned_ratio"] * MAX_COST_VS_BASELINE
        and r["coverage"] >= baseline["coverage"] - MAX_COVERAGE_LOSS
    ]
    best = min(qualifying, key=lambda r: (r["creates"], -r["release"]))
    return best["release"]


def format_rows(rows: list[dict[str, float]]) -> str:
    lines = [
        f"  {'release':>8}{'coverage':>10}{'prov/needed':>13}{'creates':>9}{'per day':>9}{'median on':>11}{'cov (truth 50)':>16}"
    ]
    for r in rows:
        lines.append(
            f"  {r['release']:>8g}{r['coverage']:>10.1%}{r['provisioned_ratio']:>13.2f}{r['creates']:>9.0f}"
            f"{r['creates_per_day']:>9.1f}{r['median_on_run']:>9.0f} m{r['coverage_truth50']:>16.1%}"
        )
    return "\n".join(lines)


def main() -> None:
    grid = [float(x) for x in os.environ.get("RELEASE_GRID", ",".join(f"{g:g}" for g in DEFAULT_GRID)).split(",")]
    series = load_series(get_connection(), SERVICE, "Invocations")
    val_start, test_start = split_points(series)
    provision = ROUTES[SERVICE].lambda_.provision_threshold_per_min

    print(f"split: train < {val_start:%Y-%m-%d %H:%M} <= validation < {test_start:%Y-%m-%d %H:%M} <= test")
    print(f"rule (fixed in advance): fewest create cycles with prov/needed <= baseline x {MAX_COST_VS_BASELINE:g} "
          f"and coverage within {MAX_COVERAGE_LOSS:.0%} of baseline; baseline = release {provision:g} (no hysteresis)")

    tuner = GbmForecaster.fit(SERVICE, series, val_start)
    val_rows = sweep(series, tuner, origins_between(series, val_start, test_start), grid)
    print("\nVALIDATION (model fitted on train only, production damping on):")
    print(format_rows(val_rows))

    chosen = choose_release(val_rows, provision)
    print(f"\nchosen release threshold: {chosen:g}" + ("  (= no hysteresis: nothing beat the baseline)" if chosen == provision else ""))

    final = GbmForecaster.fit(SERVICE, series, test_start)
    test_rows = sweep(series, final, origins_between(series, test_start, series.index.max()), sorted({provision, chosen}, reverse=True))
    print("\nTEST WEEK (refitted on everything before it; baseline vs chosen):")
    print(format_rows(test_rows))


if __name__ == "__main__":
    main()
