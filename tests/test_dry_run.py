from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import decision_engine.dry_run as dry_run
import decision_engine.engine as engine
from decision_engine.dry_run import (
    format_summary,
    future_windows,
    needed_capacity,
    replay,
    summarize,
)
from decision_engine.engine import ROUTES
from decision_engine.models import Aggressiveness, Forecast
from forecasting.data_pipeline.synthetic_data import generate_metrics


def flat_series(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.date_range("2026-06-01", periods=len(values), freq="min"), dtype="float64")


def forecast_at(series: pd.Series, i: int, service: str, p90_level: float) -> Forecast:
    n = 15
    p90 = tuple([p90_level] * n)
    return Forecast(service, series.index[i].to_pydatetime(), tuple([0.0] * n), tuple([p90_level / 2] * n), p90)


# --- nothing here may reach AWS ----------------------------------------------------------------


@pytest.mark.parametrize("module", [dry_run, engine])
def test_dry_run_code_never_references_aws(module) -> None:
    source = Path(module.__file__).read_text()

    assert "boto3" not in source
    assert "botocore" not in source


# --- ground truth helpers ----------------------------------------------------------------------


def test_future_windows_returns_the_minutes_after_each_origin() -> None:
    series = flat_series(list(range(100)))

    windows = future_windows(series, series.index[[10, 20]], steps=3)

    assert windows.tolist() == [[11.0, 12.0, 13.0], [21.0, 22.0, 23.0]]


def test_future_windows_rejects_origins_without_a_full_window() -> None:
    series = flat_series(list(range(30)))

    with pytest.raises(ValueError):
        future_windows(series, series.index[[28]], steps=5)


def test_needed_capacity_matches_the_policy_formulas_without_safety_margins() -> None:
    catalog, thumbnail = ROUTES["catalog_service"], ROUTES["thumbnail_service"]

    assert needed_capacity(catalog, 120.0) == 3  # ceil(120 / 50)
    assert needed_capacity(catalog, 0.0) == 1  # floor of min_instances
    assert needed_capacity(catalog, 1e9) == catalog.ec2.max_instances
    assert needed_capacity(thumbnail, 5.0) == 0  # below the provisioning threshold
    assert needed_capacity(thumbnail, 400.0) == 1  # ceil(400 / 60 * 0.1)


# --- replay ------------------------------------------------------------------------------------


def test_replay_chains_current_capacity_and_records_ground_truth() -> None:
    series = flat_series([2.0] * 200 + [400.0] * 20 + [2.0] * 200)
    route = ROUTES["thumbnail_service"]
    forecasts = [forecast_at(series, i, "thumbnail_service", level) for i, level in [(100, 2.0), (205, 300.0), (300, 2.0)]]

    df = replay(series, forecasts, route, Aggressiveness.BALANCED)

    assert len(df) == 3
    assert list(df["current"]) == [0, 0, int(df["desired"].iloc[1])]  # previous decision becomes current
    assert df["desired"].iloc[0] == 0 and df["desired"].iloc[1] >= 1
    assert df["needed"].iloc[1] == 1  # the real burst needed capacity
    assert df["needed"].iloc[0] == 0
    assert not df["cap_applied"].any()
    assert {"shape", "actual_shape", "reason", "forecast_demand", "actual_peak"} <= set(df.columns)


def test_replay_marks_a_missed_burst_as_under_provisioned() -> None:
    series = flat_series([2.0] * 200 + [400.0] * 20 + [2.0] * 200)
    route = ROUTES["thumbnail_service"]
    blind = [forecast_at(series, 205, "thumbnail_service", 2.0)]  # forecaster saw nothing coming

    df = replay(series, blind, route, Aggressiveness.BALANCED)
    summary = summarize(df).iloc[0]

    assert df["desired"].iloc[0] < df["needed"].iloc[0]
    assert summary["under_rate"] == 1.0
    assert summary["under_rate_when_needed"] == 1.0


def test_summarize_and_format_cover_each_service_and_aggressiveness() -> None:
    series = flat_series([2.0] * 300)
    route = ROUTES["thumbnail_service"]
    forecasts = [forecast_at(series, i, "thumbnail_service", 2.0) for i in (100, 110)]
    df = pd.concat([replay(series, forecasts, route, a) for a in Aggressiveness], ignore_index=True)

    summary = summarize(df)
    text = format_summary(summary)

    assert len(summary) == 3
    assert set(summary["aggressiveness"]) == {a.value for a in Aggressiveness}
    assert "thumbnail_service" in text and "balanced" in text


def test_ec2_replay_applies_gradual_scale_down_across_steps() -> None:
    series = flat_series([300.0] * 100 + [0.0] * 200)
    route = ROUTES["catalog_service"]
    forecasts = [forecast_at(series, 20, "catalog_service", 300.0)] + [
        forecast_at(series, i, "catalog_service", 0.0) for i in (130, 135, 140)
    ]

    df = replay(series, forecasts, route, Aggressiveness.BALANCED)

    desired = list(df["desired"])
    assert desired[0] == 6  # ceil(300 / 50)
    assert desired[1:] == [5, 4, 3]  # at most one instance released per decision


# --- end to end (real GBM fit on a small generated dataset, no DB, no AWS) ----------------------


def test_run_end_to_end_on_generated_data(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = generate_metrics(start, 9, 60, seed=3, thumbnail_ramp_fraction=0.7)

    def fake_load_series(conn, service, metric):
        picked = [(t, v) for t, s, m, v, _ in rows if s == service and m == metric]
        times, values = zip(*picked)
        index = pd.DatetimeIndex(times).tz_convert("UTC").tz_localize(None)
        return pd.Series(values, index=index, dtype="float64")

    monkeypatch.setattr(dry_run, "load_series", fake_load_series)

    df = dry_run.run(conn=None, holdout_days=1)

    assert set(df["service"]) == {"catalog_service", "thumbnail_service"}
    assert set(df["aggressiveness"]) == {a.value for a in Aggressiveness}
    assert df["desired"].between(0, 10).all()  # hard ceilings hold for every decision
    catalog = df[df["service"] == "catalog_service"]
    assert (catalog["desired"] >= 1).all() and (catalog["desired"] <= ROUTES["catalog_service"].ec2.max_instances).all()
    # Cadence: Lambda tier is replayed 5x as often as EC2 (1 min vs 5 min).
    per_aggr = df[df["aggressiveness"] == "balanced"].groupby("service").size()
    assert per_aggr["thumbnail_service"] > 4 * per_aggr["catalog_service"]
    assert not np.isnan(summarize(df)["under_rate"]).any()
