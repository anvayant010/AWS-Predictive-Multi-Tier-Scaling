"""Phase 5c: dry-run replay of forecaster -> classifier -> policy over the held-out week.

NOTHING HERE TOUCHES AWS. It replays history, writes every decision to a CSV and prints a summary
so the decisions can be reviewed by a person before any actuation code (Phase 6) exists.

For each decision it also records what the real traffic turned out to need, so the review can see
under-provisioning (capacity short of the real peak in the lookahead window) and over-provisioning
(paying for capacity that wasn't needed). The "current" capacity is simulated as the previous
decision's output, i.e. as if every decision had been applied.

Cadence follows the Phase 4 findings: the Lambda tier is replayed every minute (burst warning is
only ~3-4 min), EC2 every 5 minutes.

Run: python -m decision_engine.dry_run   (needs the local DB populated; see CLAUDE.md)
Env: HOLDOUT_DAYS (default 7), DRY_RUN_OUTPUT (default dry_run_output/dry_run_decisions.csv)
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

from decision_engine.classifier import classify
from decision_engine.engine import ROUTES, Route, decide
from decision_engine.models import Aggressiveness, Forecast, LoadShape
from forecasting.data_pipeline.db import get_connection
from forecasting.predictor import GbmForecaster
from forecasting.train import TARGET_SERIES, load_series, split_train_holdout

FORECAST_STEPS = 15
CADENCE_MINUTES = {"catalog_service": 5, "thumbnail_service": 1}
# Traffic level (invocations/min) at which provisioned concurrency counts as "needed" when grading
# Lambda decisions. Matches the original policy trigger so results stay comparable with the first
# dry-run; held fixed so tuning the policy threshold can't move the goalposts.
LAMBDA_TRUTH_THRESHOLD = 20.0


def future_windows(series: pd.Series, origins: pd.DatetimeIndex, steps: int) -> np.ndarray:
    """Actual values for origin+1 .. origin+steps minutes, one row per origin (1-minute grid)."""
    positions = series.index.get_indexer(origins)
    if (positions < 0).any() or (positions + steps >= len(series)).any():
        raise ValueError("every origin needs a full window of actual data after it")
    values = series.to_numpy()
    return np.stack([values[p + 1 : p + 1 + steps] for p in positions])


def actual_forecast(service: str, origin: pd.Timestamp, window: np.ndarray) -> Forecast:
    """Wrap the real future as a 'perfect' forecast so the same classifier can label it."""
    values = tuple(float(v) for v in window)
    return Forecast(service, origin.to_pydatetime(), values, values, values)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def needed_capacity(
    route: Route, actual_peak: float, *, truth_threshold: float = LAMBDA_TRUTH_THRESHOLD
) -> int:
    """Capacity the real peak required: no safety margin, no multiplier, but within the same caps.

    For Lambda, `truth_threshold` is the traffic level at which provisioned concurrency is judged
    worthwhile. It is deliberately independent of the policy's own trigger
    (`provision_threshold_per_min`): if the ground truth moved with the threshold being tuned, every
    threshold would grade itself on its own terms and look better than it is.
    """
    if route.ec2 is not None:
        cfg = route.ec2
        return _clamp(math.ceil(actual_peak / cfg.capacity_per_instance_rpm), cfg.min_instances, cfg.max_instances)
    assert route.lambda_ is not None
    cfg_l = route.lambda_
    if actual_peak < truth_threshold:
        return _clamp(0, cfg_l.min_concurrency, cfg_l.max_concurrency)
    return _clamp(
        math.ceil(actual_peak / 60.0 * cfg_l.avg_duration_seconds), cfg_l.min_concurrency, cfg_l.max_concurrency
    )


def _floor_and_lookahead(route: Route) -> tuple[int, int]:
    if route.ec2 is not None:
        return route.ec2.min_instances, route.ec2.lookahead_minutes
    assert route.lambda_ is not None
    return route.lambda_.min_concurrency, route.lambda_.lookahead_minutes


def replay(
    series: pd.Series,
    forecasts: list[Forecast],
    route: Route,
    aggressiveness: Aggressiveness,
    *,
    truth_threshold: float = LAMBDA_TRUTH_THRESHOLD,
) -> pd.DataFrame:
    """Run every forecast through classify+policy in time order, tracking simulated capacity."""
    origins = pd.DatetimeIndex([pd.Timestamp(f.origin) for f in forecasts])
    windows = future_windows(series, origins, FORECAST_STEPS)
    floor, lookahead = _floor_and_lookahead(route)

    current = floor
    rows = []
    for forecast, window in zip(forecasts, windows):
        # Use the route we were given (not the global table) so alternative configs can be replayed.
        decision = decide(forecast, current, aggressiveness, routes={forecast.service: route})
        scaling, cls = decision.scaling, decision.classification
        actual_peak = float(window[:lookahead].max())
        needed = needed_capacity(route, actual_peak, truth_threshold=truth_threshold)
        actual_shape = classify(actual_forecast(forecast.service, pd.Timestamp(forecast.origin), window), route.classifier)
        rows.append(
            {
                "time": forecast.origin,
                "service": forecast.service,
                "aggressiveness": aggressiveness.value,
                "shape": cls.shape.value,
                "actual_shape": actual_shape.shape.value,
                "forecast_demand": scaling.demand,
                "actual_peak": actual_peak,
                "current": current,
                "desired": scaling.desired,
                "uncapped": scaling.uncapped,
                "cap_applied": scaling.cap_applied,
                "needed": needed,
                "above_floor": needed > floor,
                "reason": scaling.reason,
            }
        )
        current = scaling.desired
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (service, aggressiveness) for reviewing the decisions at a glance."""
    out = []
    for (service, aggr), g in df.groupby(["service", "aggressiveness"], sort=False):
        needed_mask = g["above_floor"]
        out.append(
            {
                "service": service,
                "aggressiveness": aggr,
                "decisions": len(g),
                "under_rate": float((g["desired"] < g["needed"]).mean()),
                "under_rate_when_needed": float((g.loc[needed_mask, "desired"] < g.loc[needed_mask, "needed"]).mean())
                if needed_mask.any()
                else float("nan"),
                "over_rate": float((g["desired"] > g["needed"]).mean()),
                "mean_desired": float(g["desired"].mean()),
                "mean_needed": float(g["needed"].mean()),
                "cap_applied": int(g["cap_applied"].sum()),
                "shape_agreement": float((g["shape"] == g["actual_shape"]).mean()),
                "bursty_decisions": int((g["shape"] == LoadShape.BURSTY.value).sum()),
            }
        )
    return pd.DataFrame(out)


def format_summary(summary: pd.DataFrame) -> str:
    # (column, header, width, value format)
    cols = [
        ("aggressiveness", "aggressiveness", 18, "s"),
        ("decisions", "n", 7, "d"),
        ("under_rate", "under", 8, ".1%"),
        ("under_rate_when_needed", "under|needed", 13, ".1%"),
        ("over_rate", "over", 8, ".1%"),
        ("mean_desired", "avg desired", 12, ".2f"),
        ("mean_needed", "avg needed", 11, ".2f"),
        ("cap_applied", "cap hits", 9, "d"),
        ("shape_agreement", "shape agree", 12, ".1%"),
        ("bursty_decisions", "bursty", 7, "d"),
    ]
    def cell(value: object, width: int, fmt: str, left: bool) -> str:
        align = "<" if left else ">"
        return f"{value:{align}{width}{fmt}}"

    lines = []
    for service, g in summary.groupby("service", sort=False):
        lines.append(f"\n{service}")
        lines.append("  " + "".join(cell(header, width, "s", key == "aggressiveness") for key, header, width, _ in cols))
        for _, row in g.iterrows():
            lines.append("  " + "".join(cell(row[key], width, fmt, key == "aggressiveness") for key, _, width, fmt in cols))
    return "\n".join(lines)


def run(conn, holdout_days: int = 7) -> pd.DataFrame:
    frames = []
    for service, metric in TARGET_SERIES.items():
        series = load_series(conn, service, metric)
        train, _ = split_train_holdout(series, holdout_days)
        train_end = train.index.max()
        forecaster = GbmForecaster.fit(service, series, train_end)

        holdout_origins = series.index[(series.index > train_end)]
        holdout_origins = holdout_origins[holdout_origins <= series.index[-FORECAST_STEPS - 2]]
        origins = holdout_origins[:: CADENCE_MINUTES[service]]
        forecasts = forecaster.forecast_many(series, origins)
        for aggressiveness in Aggressiveness:
            frames.append(replay(series, forecasts, ROUTES[service], aggressiveness))
    return pd.concat(frames, ignore_index=True)


if __name__ == "__main__":
    decisions = run(get_connection(), int(os.environ.get("HOLDOUT_DAYS", "7")))
    output = Path(os.environ.get("DRY_RUN_OUTPUT", "dry_run_output/dry_run_decisions.csv"))
    output.parent.mkdir(parents=True, exist_ok=True)
    decisions.to_csv(output, index=False)
    print(f"wrote {len(decisions)} decisions to {output} (dry run: nothing was sent to AWS)")
    print(format_summary(summarize(decisions)))
