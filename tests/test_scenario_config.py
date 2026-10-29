from datetime import datetime, timezone

import pandas as pd
import pytest

import comparison.scenario_config as cfg
from comparison.cost import LambdaShape, PRICES, lambda_cost
from comparison.scenario import ABSOLUTE_MAX_INSTANCES
from forecasting.data_pipeline.synthetic_data import generate_metrics

START = datetime(2026, 6, 1, tzinfo=timezone.utc)  # a Monday


@pytest.fixture(scope="module")
def pinned():
    rows = generate_metrics(START, 30, 60, seed=42, thumbnail_ramp_fraction=0.7)
    out: dict[str, list] = {"catalog_service": [], "thumbnail_service": []}
    for t, service, metric, value, _source in rows:
        if (service, metric) in {("catalog_service", "RequestCount"), ("thumbnail_service", "Invocations")}:
            out[service].append((t.replace(tzinfo=None), value))
    series = {s: pd.Series(dict(v), dtype="float64").sort_index() for s, v in out.items()}
    return cfg.build_pinned_scenario(series["thumbnail_service"], series["catalog_service"])


def test_the_pinned_values_are_the_ones_decided_after_calibration() -> None:
    assert cfg.GAP_MINUTES == 15 and cfg.N_BURSTS == 5 and cfg.UTILIZATION == 0.7
    assert cfg.CATALOG_DECLARED_CAPACITY_RPM == 2000.0 and cfg.BILLED_MS_PER_REQUEST == 52.0
    assert cfg.SLO_MS >= 2 * cfg.UNLOADED_P95_MS and cfg.SLO_MS < 2 * cfg.UNLOADED_P95_MS + 10  # twice the unloaded P95, rounded up a little


def test_the_cost_model_default_matches_the_measured_billed_duration() -> None:
    assert LambdaShape().billed_ms_per_request == cfg.BILLED_MS_PER_REQUEST == cfg.lambda_shape().billed_ms_per_request


def test_thumbnail_scenario_uses_the_fixed_burst_count_and_gap(pinned) -> None:
    s = pinned.services["thumbnail_service"]

    assert len(s.episodes) == cfg.N_BURSTS
    assert s.params["gap_minutes"] == cfg.GAP_MINUTES and s.params["n_bursts"] == cfg.N_BURSTS
    for a, b in zip(s.episodes, s.episodes[1:]):  # consecutive bursts are separated by at least the idle gap
        assert (b.segment_start - a.segment_end) >= pd.Timedelta(minutes=cfg.GAP_MINUTES)


def test_a_real_run_fits_in_a_few_hours(pinned) -> None:
    assert pinned.services["thumbnail_service"].duration_minutes <= 4 * 60
    assert pinned.services["catalog_service"].duration_minutes == 120


def test_the_loudest_burst_needs_about_the_target_concurrency_and_the_cap_is_respected(pinned) -> None:
    s = pinned.services["thumbnail_service"]
    peak_rpm = float(s.offered_rpm.max())

    needed = peak_rpm / 60.0 * cfg.BILLED_MS_PER_REQUEST / 1000.0
    assert needed == pytest.approx(cfg.LAMBDA_TARGET_PEAK_CONCURRENCY, rel=0.02) and needed <= 10


def test_the_policy_sees_the_same_needed_units_as_the_real_load_does(pinned) -> None:
    s = pinned.services["thumbnail_service"]
    natural_peak = max(e.peak for e in s.episodes)

    assert natural_peak / 60.0 * s.natural_duration_seconds == pytest.approx(cfg.LAMBDA_TARGET_PEAK_CONCURRENCY)


def test_the_catalog_scenario_peaks_at_the_target_number_of_instances_on_the_declared_capacity(pinned) -> None:
    s = pinned.services["catalog_service"]

    assert s.capacity_rpm_per_instance == 2000.0 and s.utilization == 0.7
    assert 2 <= s.static_peak_instances <= 5 and s.static_peak_instances <= ABSOLUTE_MAX_INSTANCES
    smoothed = (s.series * s.load_scale).rolling(cfg.catalog_params().smooth_minutes).mean().max()
    assert smoothed == pytest.approx(cfg.CATALOG_TARGET_PEAK_INSTANCES * 0.7 * 2000.0)


def test_the_real_catalog_load_is_far_below_what_one_real_instance_was_shown_to_handle(pinned) -> None:
    s = pinned.services["catalog_service"]

    assert float(s.offered_rpm.max()) < 25_000  # the laptop/instance combination was verified flat up to 25,000 rpm


def test_the_scenario_is_deterministic(pinned) -> None:
    rows = generate_metrics(START, 30, 60, seed=42, thumbnail_ramp_fraction=0.7)
    series = {"catalog_service": {}, "thumbnail_service": {}}
    for t, service, metric, value, _s in rows:
        if (service, metric) in {("catalog_service", "RequestCount"), ("thumbnail_service", "Invocations")}:
            series[service][t.replace(tzinfo=None)] = value
    again = cfg.build_pinned_scenario(
        pd.Series(series["thumbnail_service"], dtype="float64").sort_index(),
        pd.Series(series["catalog_service"], dtype="float64").sort_index(),
    )

    assert again.fingerprint() == pinned.fingerprint()


def test_lambda_cost_scales_with_the_billed_duration_and_defaults_to_the_measured_one() -> None:
    default = lambda_cost(requests=1_000_000, provisioned_hours=0.0)
    double = lambda_cost(requests=1_000_000, provisioned_hours=0.0, shape=LambdaShape(billed_ms_per_request=104.0))

    assert double["lambda_execution_on_demand"] == pytest.approx(2 * default["lambda_execution_on_demand"])
    assert default["lambda_execution_on_demand"] == pytest.approx(1_000_000 * 0.052 * 0.125 * PRICES.lambda_per_gb_second)
