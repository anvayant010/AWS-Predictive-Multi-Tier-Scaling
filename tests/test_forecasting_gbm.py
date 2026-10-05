import numpy as np
import pandas as pd

from forecasting.gbm import (
    build_dataset,
    evaluate_gbm,
    fit_quantile_models,
    origin_features,
    predict_quantiles,
)


def make_series(days: int = 12, freq: str = "5min") -> pd.Series:
    index = pd.date_range("2026-06-01", periods=int(days * 1440 / pd.Timedelta(freq).total_seconds() * 60), freq=freq)
    minutes = index.hour * 60 + index.minute
    values = 50 + 40 * np.sin(2 * np.pi * minutes / 1440) + np.random.default_rng(0).normal(0, 2, len(index))
    return pd.Series(values, index=index, dtype="float64")


def test_origin_features_use_only_data_at_or_before_the_origin() -> None:
    series = make_series(days=3)
    tampered = series.copy()
    cut = series.index[200]
    tampered[tampered.index > cut] = 1e9  # rewrite the entire future

    original = origin_features(series).loc[cut]
    after_tamper = origin_features(tampered).loc[cut]

    assert original.equals(after_tamper)


def test_build_dataset_target_is_value_horizon_minutes_ahead() -> None:
    series = make_series(days=3)

    X, y, target_times = build_dataset(series, horizons=(5,), recent=False)

    # Grid is 5-min, so horizon 5 means exactly the next row.
    expected = series.shift(-1).reindex(X.index)
    assert np.allclose(y.to_numpy(), expected.dropna().reindex(y.index).to_numpy())
    assert (target_times == (y.index + pd.Timedelta(minutes=5))).all()


def test_build_dataset_respects_target_window_so_train_never_sees_holdout_targets() -> None:
    series = make_series(days=5)
    train_end = series.index[3 * 24 * 12]

    _, _, train_targets = build_dataset(series, horizons=(5, 10), target_until=train_end)
    _, _, holdout_targets = build_dataset(series, horizons=(5, 10), target_after=train_end)

    assert train_targets.max() <= train_end
    assert holdout_targets.min() > train_end


def test_calendar_only_dataset_has_no_recent_history_columns() -> None:
    series = make_series(days=3)

    X_recent, _, _ = build_dataset(series, horizons=(5,), recent=True)
    X_calendar, _, _ = build_dataset(series, horizons=(5,), recent=False)

    assert "lag_0" in X_recent.columns
    assert not any(c.startswith(("lag_", "mean_", "max_", "slope_")) for c in X_calendar.columns)
    assert {"horizon", "minute_of_day", "day_of_week", "season_1d", "season_7d"} <= set(X_calendar.columns)


def test_predict_quantiles_are_ordered_and_non_negative() -> None:
    series = make_series(days=10)  # > 7 days so season_7d has data (an all-NaN column breaks sklearn binning)
    X, y, _ = build_dataset(series, horizons=(5, 10), recent=True)
    models = fit_quantile_models(X, y, max_iter=20)

    forecast = predict_quantiles(models, X)

    assert list(forecast.columns) == ["p10", "p50", "p90"]
    assert (forecast["p10"] <= forecast["p50"]).all()
    assert (forecast["p50"] <= forecast["p90"]).all()
    assert (forecast >= 0).all().all()


def test_evaluate_gbm_reports_each_horizon_and_pooled_scores_on_a_learnable_signal() -> None:
    # Checks structure, and that a clean daily cycle is learned (error well below its amplitude).
    series = make_series(days=11, freq="1min")

    results = evaluate_gbm(series, holdout_days=2, recent=True, origin_stride=5)

    assert set(results) == {"h=1", "h=5", "h=15", "all h"}
    assert results["all h"]["mae"] < 15  # signal amplitude is 40; predicting the mean gives ~25
