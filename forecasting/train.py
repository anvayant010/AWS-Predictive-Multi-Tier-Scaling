"""Phase 4a: score the baseline forecasters on a time-ordered holdout.

Run directly (needs the local TimescaleDB up and populated with synthetic data):
    python -m forecasting.train
Env: HOLDOUT_DAYS (default 7), DATABASE_URL (read by data_pipeline.db).
"""

from __future__ import annotations

import os
from datetime import timedelta

import pandas as pd
from psycopg2.extensions import connection as Connection

from forecasting.baselines import prophet_forecast, seasonal_naive_forecast
from forecasting.data_pipeline.db import get_connection
from forecasting.evaluation import score

# The series each service is forecast on (see docs/design.md, Phase 4).
TARGET_SERIES = {"catalog_service": "RequestCount", "thumbnail_service": "Invocations"}
HIGH_LOAD_QUANTILE = 0.97


def load_series(conn: Connection, service: str, metric_name: str, source: str = "synthetic") -> pd.Series:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT time, value FROM metrics "
            "WHERE service = %s AND metric_name = %s AND source = %s ORDER BY time",
            (service, metric_name, source),
        )
        rows = cur.fetchall()
    times, values = zip(*rows) if rows else ((), ())
    index = pd.DatetimeIndex(times, name="time")
    index = index.tz_convert("UTC").tz_localize(None) if index.tz is not None else index
    return pd.Series(values, index=index, dtype="float64", name=metric_name)


def split_train_holdout(series: pd.Series, holdout_days: int) -> tuple[pd.Series, pd.Series]:
    """Time-ordered split: the final `holdout_days` are held out, never shuffled."""
    cutoff = series.index.max() - timedelta(days=holdout_days)
    return series[series.index <= cutoff], series[series.index > cutoff]


def evaluate_baselines(series: pd.Series, holdout_days: int) -> dict[str, dict[str, float]]:
    train, holdout = split_train_holdout(series, holdout_days)
    threshold = float(train.quantile(HIGH_LOAD_QUANTILE))
    train_end = train.index.max()

    forecasts = {
        "seasonal_naive_1d": seasonal_naive_forecast(
            series, train_end, holdout.index, period=timedelta(days=1)
        ),
        "seasonal_naive_7d": seasonal_naive_forecast(
            series, train_end, holdout.index, period=timedelta(days=7)
        ),
        "prophet": prophet_forecast(train, holdout.index),
    }
    return {
        name: score(holdout.loc[forecast.index], forecast, high_load_threshold=threshold)
        for name, forecast in forecasts.items()
    }


def run(conn: Connection, holdout_days: int = 7) -> dict[str, dict[str, dict[str, float]]]:
    return {
        service: evaluate_baselines(load_series(conn, service, metric), holdout_days)
        for service, metric in TARGET_SERIES.items()
    }


def format_results(results: dict[str, dict[str, dict[str, float]]]) -> str:
    lines: list[str] = []
    for service, models in results.items():
        lines.append(f"\n{service} ({TARGET_SERIES[service]})")
        lines.append(f"  {'model':<20}{'MAPE%':>8}{'MAE':>8}{'pinball':>9}{'p90 cov':>9}{'p90 cov (high)':>16}")
        for name, m in models.items():
            lines.append(
                f"  {name:<20}{m['mape']:>8.1f}{m['mae']:>8.2f}{m['pinball']:>9.2f}"
                f"{m['p90_coverage']:>9.2f}{m['p90_coverage_high_load']:>16.2f}"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_results(run(get_connection(), int(os.environ.get("HOLDOUT_DAYS", "7")))))
