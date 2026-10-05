from datetime import datetime, timezone

import pandas as pd
import pytest

from decision_engine.models import Forecast
from forecasting.data_pipeline.synthetic_data import generate_metrics
from forecasting.predictor import GbmForecaster


def make_series(service: str, metric: str, days: int = 9) -> pd.Series:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = generate_metrics(start, days, 60, seed=11, thumbnail_ramp_fraction=0.7)
    picked = [(t, v) for t, s, m, v, _ in rows if s == service and m == metric]
    times, values = zip(*picked)
    index = pd.DatetimeIndex(times).tz_convert("UTC").tz_localize(None)
    return pd.Series(values, index=index, dtype="float64")


@pytest.fixture(scope="module")
def catalog():
    series = make_series("catalog_service", "RequestCount")
    train_end = series.index.max() - pd.Timedelta(days=1)
    return series, train_end, GbmForecaster.fit("catalog_service", series, train_end)


def test_forecast_many_returns_valid_15_step_forecasts(catalog) -> None:
    series, train_end, forecaster = catalog
    origins = series.index[series.index > train_end][:30:10]

    forecasts = forecaster.forecast_many(series, origins)

    assert len(forecasts) == 3
    for f, origin in zip(forecasts, origins):
        assert isinstance(f, Forecast)  # construction already validates ordering / finiteness
        assert f.service == "catalog_service"
        assert f.horizon_minutes == 15
        assert f.origin == origin.to_pydatetime()


def test_single_forecast_matches_the_batched_one(catalog) -> None:
    series, train_end, forecaster = catalog
    origins = series.index[series.index > train_end][:5]

    batched = forecaster.forecast_many(series, origins)

    assert forecaster.forecast(series, origins[2]) == batched[2]


def test_forecast_uses_no_data_after_the_origin(catalog) -> None:
    series, train_end, forecaster = catalog
    origin = series.index[series.index > train_end][100]
    tampered = series.copy()
    tampered[tampered.index > origin] = 1e9

    assert forecaster.forecast(series, origin) == forecaster.forecast(tampered, origin)


def test_fit_never_sees_values_after_train_end() -> None:
    series = make_series("catalog_service", "RequestCount")
    train_end = series.index.max() - pd.Timedelta(days=1)
    tampered = series.copy()
    tampered[tampered.index > train_end] = 1e9
    origin = series.index[series.index <= train_end][-200]

    clean_fit = GbmForecaster.fit("catalog_service", series, train_end)
    tampered_fit = GbmForecaster.fit("catalog_service", tampered, train_end)

    assert clean_fit.forecast(series, origin) == tampered_fit.forecast(series, origin)


def test_refuses_to_forecast_from_incomplete_history(catalog) -> None:
    series, _train_end, forecaster = catalog

    with pytest.raises(ValueError, match="incomplete inputs"):
        forecaster.forecast_many(series, series.index[:1])  # first minute: no lag history yet


def test_empty_origins_give_no_forecasts(catalog) -> None:
    series, _train_end, forecaster = catalog

    assert forecaster.forecast_many(series, pd.DatetimeIndex([])) == []
