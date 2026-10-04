from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from forecasting.baselines import prophet_forecast, seasonal_naive_forecast
from forecasting.data_pipeline.synthetic_data import generate_metrics
from forecasting.evaluation import coverage, mae, mape, mean_pinball_loss, pinball_loss, score
from forecasting.train import (
    evaluate_baselines,
    format_results,
    load_series,
    run,
    split_train_holdout,
)


def make_series(service: str, metric: str, days: int, interval_seconds: int = 3600) -> pd.Series:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = generate_metrics(start, days, interval_seconds, seed=5)
    picked = [(t, v) for t, s, m, v, _src in rows if s == service and m == metric]
    times, values = zip(*picked)
    index = pd.DatetimeIndex(times).tz_convert("UTC").tz_localize(None)
    return pd.Series(values, index=index, dtype="float64")


def test_pinball_loss_penalises_under_prediction_more_for_high_quantile() -> None:
    actual = pd.Series([10.0, 10.0])
    under = pd.Series([5.0, 5.0])
    over = pd.Series([15.0, 15.0])

    assert pinball_loss(actual, under, 0.9) > pinball_loss(actual, over, 0.9)
    assert pinball_loss(actual, under, 0.1) < pinball_loss(actual, over, 0.1)


def test_pinball_loss_is_zero_for_perfect_forecast() -> None:
    actual = pd.Series([1.0, 2.0, 3.0])
    forecast = pd.DataFrame({"p10": actual, "p50": actual, "p90": actual})

    assert mean_pinball_loss(actual, forecast) == 0.0


def test_mape_ignores_near_zero_actuals() -> None:
    actual = pd.Series([0.0, 100.0])
    predicted = pd.Series([50.0, 110.0])

    assert mape(actual, predicted) == pytest.approx(10.0)
    assert np.isnan(mape(pd.Series([0.0]), pd.Series([1.0])))


def test_mae_and_coverage() -> None:
    actual = pd.Series([1.0, 2.0, 3.0, 4.0])

    assert mae(actual, actual + 2) == 2.0
    assert coverage(actual, pd.Series([1.0, 1.0, 3.0, 5.0])) == 0.75


def test_score_high_load_coverage_exposes_missed_bursts() -> None:
    # Flat 2.0 with two bursts of 100. A forecast that ignores bursts covers 80% overall
    # but 0% of the high-load minutes -- the number that actually matters for scaling.
    actual = pd.Series([2.0] * 8 + [100.0, 100.0])
    flat = pd.Series([2.0] * 10)
    forecast = pd.DataFrame({"p10": flat, "p50": flat, "p90": flat + 1})

    result = score(actual, forecast, high_load_threshold=50.0)

    assert result["p90_coverage"] == pytest.approx(0.8)
    assert result["p90_coverage_high_load"] == 0.0


def test_seasonal_naive_returns_value_from_one_period_earlier() -> None:
    index = pd.date_range("2026-06-01", periods=5 * 24, freq="h")
    series = pd.Series(np.arange(len(index), dtype="float64"), index=index)
    train_end = index[3 * 24 - 1]
    holdout_index = index[index > train_end]

    forecast = seasonal_naive_forecast(series, train_end, holdout_index, period=timedelta(days=1))

    expected = series.shift(24).reindex(holdout_index)
    assert (forecast["p50"] == expected).all()


def test_seasonal_naive_quantiles_are_ordered_and_non_negative() -> None:
    series = make_series("catalog_service", "RequestCount", days=10)
    train, holdout = split_train_holdout(series, holdout_days=3)

    forecast = seasonal_naive_forecast(
        series, train.index.max(), holdout.index, period=timedelta(days=1)
    )

    assert (forecast["p10"] <= forecast["p50"]).all()
    assert (forecast["p50"] <= forecast["p90"]).all()
    assert (forecast >= 0).all().all()


def test_seasonal_naive_uses_only_training_residuals_for_intervals() -> None:
    index = pd.date_range("2026-06-01", periods=4 * 24, freq="h")
    flat = pd.Series(10.0, index=index)
    # A huge spike confined to the holdout must not widen the interval.
    spiky = flat.copy()
    spiky.iloc[-1] = 10_000.0
    train_end = index[2 * 24 - 1]
    holdout_index = index[index > train_end]

    clean = seasonal_naive_forecast(flat, train_end, holdout_index, period=timedelta(days=1))
    with_spike = seasonal_naive_forecast(spiky, train_end, holdout_index, period=timedelta(days=1))

    assert (clean["p90"].iloc[:24] == with_spike["p90"].iloc[:24]).all()


def test_split_train_holdout_is_time_ordered_and_disjoint() -> None:
    series = make_series("catalog_service", "RequestCount", days=10)

    train, holdout = split_train_holdout(series, holdout_days=3)

    assert train.index.max() < holdout.index.min()
    assert len(train) + len(holdout) == len(series)
    assert holdout.index.max() == series.index.max()


def test_prophet_forecast_shape_and_ordering() -> None:
    series = make_series("catalog_service", "RequestCount", days=10)
    train, holdout = split_train_holdout(series, holdout_days=2)

    forecast = prophet_forecast(train, holdout.index)

    assert list(forecast.columns) == ["p10", "p50", "p90"]
    assert forecast.index.equals(holdout.index)
    assert (forecast["p10"] <= forecast["p50"]).all()
    assert (forecast["p50"] <= forecast["p90"]).all()
    assert (forecast >= 0).all().all()


def test_evaluate_baselines_scores_all_three_models_and_prophet_beats_flat_guess_on_catalog() -> None:
    series = make_series("catalog_service", "RequestCount", days=14)

    results = evaluate_baselines(series, holdout_days=3)

    assert set(results) == {"seasonal_naive_1d", "seasonal_naive_7d", "prophet"}
    # The catalog series is a smooth daily cycle; a sane model must be far better than
    # guessing the training mean (MAE of that guess is roughly 35+ on this data).
    assert results["prophet"]["mae"] < 20
    for metrics in results.values():
        assert set(metrics) == {"mape", "mae", "pinball", "p90_coverage", "p90_coverage_high_load"}


def test_load_series_builds_tz_naive_float_series_from_db_rows() -> None:
    times = [datetime(2026, 6, 1, 0, m, tzinfo=timezone.utc) for m in range(3)]
    cursor = MagicMock()
    cursor.fetchall.return_value = [(t, float(i)) for i, t in enumerate(times)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    series = load_series(conn, "catalog_service", "RequestCount")

    assert series.index.tz is None
    assert list(series.values) == [0.0, 1.0, 2.0]
    sql, params = cursor.execute.call_args.args
    assert params == ("catalog_service", "RequestCount", "synthetic")
    assert "source = %s" in sql


def test_run_loads_each_target_series_and_format_results_renders_a_table() -> None:
    fake = {
        "seasonal_naive_1d": {
            "mape": 10.0, "mae": 1.0, "pinball": 0.5, "p90_coverage": 0.9, "p90_coverage_high_load": 0.8
        }
    }
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("forecasting.train.load_series", lambda conn, service, metric: (service, metric))
        mp.setattr("forecasting.train.evaluate_baselines", lambda series, days: fake)
        results = run(MagicMock(), holdout_days=7)

    assert set(results) == {"catalog_service", "thumbnail_service"}
    text = format_results(results)
    assert "catalog_service (RequestCount)" in text
    assert "thumbnail_service (Invocations)" in text
    assert "seasonal_naive_1d" in text
