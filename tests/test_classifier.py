from datetime import datetime, timezone

import pytest

from decision_engine.classifier import (
    CATALOG_CLASSIFIER,
    THUMBNAIL_CLASSIFIER,
    ClassifierConfig,
    classify,
)
from decision_engine.models import Forecast, LoadShape

ORIGIN = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
CONFIG = ClassifierConfig(quiet_below=10.0)


def forecast_from(p90: list[float]) -> Forecast:
    p50 = [v * 0.6 for v in p90]
    p10 = [v * 0.3 for v in p90]
    return Forecast("svc", ORIGIN, tuple(p10), tuple(p50), tuple(p90))


def test_flat_low_load_is_quiet() -> None:
    assert classify(forecast_from([3.0] * 15), CONFIG).shape is LoadShape.QUIET


def test_flat_high_load_is_sustained() -> None:
    assert classify(forecast_from([120.0] * 15), CONFIG).shape is LoadShape.SUSTAINED


def test_gradual_daily_growth_is_sustained_not_bursty() -> None:
    growth = [20.0 + 6 * i for i in range(15)]  # 20 -> 104 over the window

    assert classify(forecast_from(growth), CONFIG).shape is LoadShape.SUSTAINED


def test_short_spike_after_a_quiet_stretch_is_bursty() -> None:
    spike = [2.0] * 11 + [30.0, 80.0, 100.0, 90.0]

    result = classify(forecast_from(spike), CONFIG)

    assert result.shape is LoadShape.BURSTY
    assert result.peak == 100.0
    assert result.level == 2.0
    assert "x the typical level" in result.reason


def test_ramp_then_peak_at_the_end_of_the_window_is_bursty() -> None:
    ramp = [2.0] * 12 + [15.0, 35.0, 70.0]

    assert classify(forecast_from(ramp), CONFIG).shape is LoadShape.BURSTY


def test_a_burst_that_fills_most_of_the_window_is_sustained() -> None:
    # Load is up for 12 of 15 steps: that is a new, persisting level, not a transient spike.
    long_burst = [2.0] * 3 + [100.0] * 12

    assert classify(forecast_from(long_burst), CONFIG).shape is LoadShape.SUSTAINED


def test_quiet_takes_priority_over_a_tiny_relative_spike() -> None:
    tiny = [0.5] * 12 + [4.0] * 3  # 8x the level, but nothing worth reacting to

    assert classify(forecast_from(tiny), CONFIG).shape is LoadShape.QUIET


def test_classifier_uses_the_configured_series() -> None:
    # p90 contains a spike; p50 does not.
    p90 = [2.0] * 11 + [100.0] * 4
    p50 = [2.0] * 15
    forecast = Forecast("svc", ORIGIN, tuple(p50), tuple(p50), tuple(p90))

    assert classify(forecast, ClassifierConfig(quiet_below=10.0, series="p90")).shape is LoadShape.BURSTY
    assert classify(forecast, ClassifierConfig(quiet_below=10.0, series="p50")).shape is LoadShape.QUIET


def test_zero_level_with_a_spike_does_not_divide_by_zero() -> None:
    spike = [0.0] * 12 + [50.0, 80.0, 60.0]

    assert classify(forecast_from(spike), CONFIG).shape is LoadShape.BURSTY


def test_classification_is_deterministic_and_has_a_reason() -> None:
    f = forecast_from([2.0] * 11 + [30.0, 80.0, 100.0, 90.0])

    assert classify(f, CONFIG) == classify(f, CONFIG)
    assert classify(f, CONFIG).reason


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quiet_below": -1},
        {"quiet_below": 5, "burst_ratio": 1.0},
        {"quiet_below": 5, "max_spike_fraction": 0.0},
        {"quiet_below": 5, "max_spike_fraction": 1.0},
        {"quiet_below": 5, "series": "p99"},
    ],
)
def test_config_rejects_nonsense(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        ClassifierConfig(**kwargs)


def test_default_service_configs_are_valid() -> None:
    assert CATALOG_CLASSIFIER.quiet_below < THUMBNAIL_CLASSIFIER.quiet_below
