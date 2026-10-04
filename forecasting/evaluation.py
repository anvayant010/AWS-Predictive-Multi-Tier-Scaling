"""Forecast scoring shared by every model (seasonal-naive, Prophet, later TFT).

A forecast is a DataFrame indexed by time with columns p10 / p50 / p90, so every model is
scored by the same code on the same holdout.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

QUANTILE_COLUMNS = {0.1: "p10", 0.5: "p50", 0.9: "p90"}


def mape(actual: pd.Series, predicted: pd.Series, *, min_actual: float = 1.0) -> float:
    """Mean absolute percentage error, ignoring points where |actual| < min_actual.

    MAPE explodes (or is undefined) near zero, which thumbnail_service's low baseline hits
    constantly, so those points are excluded rather than letting them dominate the score.
    """
    mask = actual.abs() >= min_actual
    if not mask.any():
        return float("nan")
    return float((np.abs(actual[mask] - predicted[mask]) / actual[mask].abs()).mean() * 100)


def mae(actual: pd.Series, predicted: pd.Series) -> float:
    return float(np.abs(actual - predicted).mean())


def pinball_loss(actual: pd.Series, predicted: pd.Series, quantile: float) -> float:
    error = actual - predicted
    return float(np.maximum(quantile * error, (quantile - 1) * error).mean())


def mean_pinball_loss(actual: pd.Series, forecast: pd.DataFrame) -> float:
    return float(
        np.mean([pinball_loss(actual, forecast[col], q) for q, col in QUANTILE_COLUMNS.items()])
    )


def coverage(actual: pd.Series, upper: pd.Series) -> float:
    """Fraction of points at or below the upper bound (a good p90 gives ~0.9)."""
    return float((actual <= upper).mean())


def score(actual: pd.Series, forecast: pd.DataFrame, *, high_load_threshold: float) -> dict[str, float]:
    """Score a forecast against actuals (aligned on index).

    p90_coverage_high_load restricts to minutes where actual load exceeds the threshold (the
    bursts / daily peaks). Overall p90 coverage alone is misleading for a spiky series: a
    forecast that never anticipates a burst still covers ~96% of the (mostly flat) minutes.
    """
    forecast = forecast.loc[actual.index]
    high = actual > high_load_threshold
    return {
        "mape": mape(actual, forecast["p50"]),
        "mae": mae(actual, forecast["p50"]),
        "pinball": mean_pinball_loss(actual, forecast),
        "p90_coverage": coverage(actual, forecast["p90"]),
        "p90_coverage_high_load": coverage(actual[high], forecast["p90"][high])
        if high.any()
        else float("nan"),
    }
