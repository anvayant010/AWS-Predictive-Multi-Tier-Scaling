"""Phase 4 experiment: gradient-boosted quantile forecaster with lag features.

Purpose is diagnostic, ahead of installing the heavy TFT stack: does a model that sees *recent
history* beat the baselines, especially on thumbnail_service bursts (the ramp-up lead signal)?
It is also a stronger, cheaper baseline for the TFT to beat.

One model per quantile (p10/p50/p90), with the forecast horizon (1..15 min) as a feature, so a
single model serves every step of the 15-minute window. Rows are origins: features use only data
at or before the origin time; the target is the value `horizon` minutes later.

Run: python -m forecasting.gbm   (needs the local TimescaleDB populated with synthetic data)
"""

from __future__ import annotations

import os
from datetime import timedelta

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from forecasting.data_pipeline.db import get_connection
from forecasting.evaluation import QUANTILE_COLUMNS, score
from forecasting.train import HIGH_LOAD_QUANTILE, TARGET_SERIES, load_series, split_train_holdout

HORIZONS = tuple(range(1, 16))
REPORT_HORIZONS = (1, 5, 15)
LAGS = (0, 1, 2, 3, 5, 10, 15, 30, 60)


def origin_features(series: pd.Series) -> pd.DataFrame:
    """Recent-history features at each origin. Assumes a regular 1-minute grid."""
    f = pd.DataFrame(index=series.index)
    for k in LAGS:
        f[f"lag_{k}"] = series.shift(k)
    for window in (5, 15, 60):
        f[f"mean_{window}"] = series.rolling(window).mean()
    for window in (15, 60):
        f[f"max_{window}"] = series.rolling(window).max()
    f["slope_4"] = series - series.shift(4)
    return f


def target_features(series: pd.Series, origin_index: pd.DatetimeIndex, horizon: int) -> pd.DataFrame:
    """Calendar + same-time-yesterday/last-week values for the target minute.

    The seasonal references are >= 1 day before the target, so they are known at the origin.
    """
    target_time = origin_index + timedelta(minutes=horizon)
    return pd.DataFrame(
        {
            "horizon": horizon,
            "minute_of_day": target_time.hour * 60 + target_time.minute,
            "day_of_week": target_time.dayofweek,
            "season_1d": series.reindex(target_time - timedelta(days=1)).to_numpy(),
            "season_7d": series.reindex(target_time - timedelta(days=7)).to_numpy(),
        },
        index=origin_index,
    )


def build_dataset(
    series: pd.Series,
    *,
    horizons: tuple[int, ...] = HORIZONS,
    origin_stride: int = 1,
    recent: bool = True,
    target_after: pd.Timestamp | None = None,
    target_until: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.Series, pd.DatetimeIndex]:
    """Return (X, y, target_times). `recent=False` drops lag/rolling features (calendar-only)."""
    origin_feats = origin_features(series) if recent else pd.DataFrame(index=series.index)
    origins = series.index[::origin_stride]
    frames, targets, target_times = [], [], []
    for h in horizons:
        tt = origins + timedelta(minutes=h)
        y = series.reindex(tt).to_numpy()
        keep = ~np.isnan(y)
        if target_after is not None:
            keep &= tt > target_after
        if target_until is not None:
            keep &= tt <= target_until
        if recent:
            keep &= origin_feats.reindex(origins).notna().all(axis=1).to_numpy()
        x = pd.concat(
            [origin_feats.reindex(origins), target_features(series, origins, h)], axis=1
        )
        frames.append(x[keep])
        targets.append(pd.Series(y[keep], index=origins[keep]))
        target_times.append(tt[keep])
    X = pd.concat(frames)
    y_all = pd.concat(targets)
    return X, y_all, pd.DatetimeIndex(np.concatenate([t.to_numpy() for t in target_times]))


def fit_quantile_models(
    X: pd.DataFrame, y: pd.Series, *, max_iter: int = 200, random_state: int = 0
) -> dict[float, HistGradientBoostingRegressor]:
    return {
        q: HistGradientBoostingRegressor(
            loss="quantile", quantile=q, max_iter=max_iter, random_state=random_state
        ).fit(X, y)
        for q in QUANTILE_COLUMNS
    }


def predict_quantiles(models: dict[float, HistGradientBoostingRegressor], X: pd.DataFrame) -> pd.DataFrame:
    raw = np.column_stack([models[q].predict(X) for q in sorted(QUANTILE_COLUMNS)])
    ordered = np.sort(raw, axis=1)  # independently fit quantiles can cross; sorting fixes that
    return pd.DataFrame(np.clip(ordered, 0, None), index=X.index, columns=["p10", "p50", "p90"])


def evaluate_gbm(
    series: pd.Series, holdout_days: int, *, recent: bool, origin_stride: int = 3
) -> dict[str, dict[str, float]]:
    train, holdout = split_train_holdout(series, holdout_days)
    train_end = train.index.max()
    threshold = float(train.quantile(HIGH_LOAD_QUANTILE))

    X_train, y_train, _ = build_dataset(
        series, recent=recent, origin_stride=origin_stride, target_until=train_end
    )
    models = fit_quantile_models(X_train, y_train)

    results: dict[str, dict[str, float]] = {}
    pooled_actual, pooled_forecast = [], []
    for h in HORIZONS:
        X, y, target_times = build_dataset(
            series, horizons=(h,), recent=recent, target_after=train_end
        )
        forecast = predict_quantiles(models, X)
        forecast.index = target_times
        actual = pd.Series(y.to_numpy(), index=target_times)
        if h in REPORT_HORIZONS:
            results[f"h={h}"] = score(actual, forecast, high_load_threshold=threshold)
        pooled_actual.append(actual.reset_index(drop=True))
        pooled_forecast.append(forecast.reset_index(drop=True))
    results["all h"] = score(
        pd.concat(pooled_actual, ignore_index=True),
        pd.concat(pooled_forecast, ignore_index=True),
        high_load_threshold=threshold,
    )
    return results


def format_results(results: dict[str, dict[str, dict[str, dict[str, float]]]]) -> str:
    lines: list[str] = []
    for service, variants in results.items():
        lines.append(f"\n{service} ({TARGET_SERIES[service]})")
        lines.append(f"  {'model':<26}{'MAE':>8}{'pinball':>9}{'p90 cov':>9}{'p90 cov (high)':>16}")
        for variant, by_horizon in variants.items():
            for horizon, m in by_horizon.items():
                lines.append(
                    f"  {variant + ' ' + horizon:<26}{m['mae']:>8.2f}{m['pinball']:>9.2f}"
                    f"{m['p90_coverage']:>9.2f}{m['p90_coverage_high_load']:>16.2f}"
                )
    return "\n".join(lines)


if __name__ == "__main__":
    conn = get_connection()
    holdout_days = int(os.environ.get("HOLDOUT_DAYS", "7"))
    out = {}
    for service, metric in TARGET_SERIES.items():
        series = load_series(conn, service, metric)
        out[service] = {
            "gbm+history": evaluate_gbm(series, holdout_days, recent=True),
            "gbm calendar-only": evaluate_gbm(series, holdout_days, recent=False),
        }
    print(format_results(out))
