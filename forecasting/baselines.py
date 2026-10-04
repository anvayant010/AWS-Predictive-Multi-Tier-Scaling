"""Baseline forecasters for Phase 4a: seasonal-naive and Prophet.

Both return a DataFrame (index = timestamps to forecast, columns p10 / p50 / p90) so they plug
into forecasting.evaluation.score unchanged.

Neither baseline is sensitive to forecast horizon within our 15-minute window: seasonal-naive
looks back >= 1 day, and Prophet is fit once and never sees recent values. So both can be
evaluated over every holdout minute at once. The TFT *will* depend on the issue time, and gets
rolling-origin evaluation when it is built.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd
from prophet import Prophet

# Prophet/cmdstanpy log every fit at INFO, which drowns the evaluation output.
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
logging.getLogger("prophet").setLevel(logging.WARNING)


def seasonal_naive_forecast(
    full_series: pd.Series,
    train_end: pd.Timestamp,
    holdout_index: pd.DatetimeIndex,
    *,
    period: timedelta,
) -> pd.DataFrame:
    """Forecast = the value one `period` earlier (same time yesterday / last week).

    Uses the full series, which does not leak: a forecast issued at t sees data up to t, and
    `period` (>= 1 day) is far longer than the 15-minute horizon. p10/p90 come from the spread
    of this method's own errors on the training portion only.
    """
    lagged = full_series.shift(freq=period).reindex(full_series.index)
    train_mask = full_series.index <= train_end
    residuals = (full_series - lagged)[train_mask].dropna()
    p10_offset, p90_offset = residuals.quantile(0.1), residuals.quantile(0.9)

    point = lagged.reindex(holdout_index)
    return pd.DataFrame(
        {
            "p10": (point + p10_offset).clip(lower=0),
            "p50": point.clip(lower=0),
            "p90": (point + p90_offset).clip(lower=0),
        },
        index=holdout_index,
    ).dropna()


def prophet_forecast(
    train: pd.Series,
    holdout_index: pd.DatetimeIndex,
    *,
    interval_width: float = 0.8,
) -> pd.DataFrame:
    """Fit Prophet once on `train`, forecast every timestamp in `holdout_index`.

    interval_width=0.8 puts yhat_lower / yhat_upper at the 10th / 90th percentile.
    """
    model = Prophet(
        daily_seasonality=True,
        weekly_seasonality=True,
        yearly_seasonality=False,
        interval_width=interval_width,
    )
    model.fit(pd.DataFrame({"ds": train.index, "y": train.values}))
    predicted = model.predict(pd.DataFrame({"ds": holdout_index})).set_index("ds")
    return pd.DataFrame(
        {
            "p10": predicted["yhat_lower"].clip(lower=0),
            "p50": predicted["yhat"].clip(lower=0),
            "p90": predicted["yhat_upper"].clip(lower=0),
        },
        index=holdout_index,
    )
