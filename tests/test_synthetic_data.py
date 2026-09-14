import random
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from forecasting.data_pipeline.synthetic_data import (
    build_burst_schedule,
    catalog_request_count,
    catalog_target_response_time,
    generate_metrics,
    run,
    thumbnail_concurrent_executions,
    thumbnail_invocations,
)


def test_catalog_request_count_peaks_at_midday_over_midnight() -> None:
    rng = random.Random(0)
    midday = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)  # Monday
    midnight = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)

    midday_count = catalog_request_count(
        midday, min_rpm=5, max_rpm=150, weekend_factor=0.5, noise_std=0, rng=rng
    )
    midnight_count = catalog_request_count(
        midnight, min_rpm=5, max_rpm=150, weekend_factor=0.5, noise_std=0, rng=rng
    )

    assert midday_count > midnight_count


def test_catalog_request_count_lower_on_weekend() -> None:
    rng = random.Random(0)
    saturday_midday = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)  # Saturday
    monday_midday = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)  # Monday

    weekend_count = catalog_request_count(
        saturday_midday, min_rpm=5, max_rpm=150, weekend_factor=0.5, noise_std=0, rng=rng
    )
    weekday_count = catalog_request_count(
        monday_midday, min_rpm=5, max_rpm=150, weekend_factor=0.5, noise_std=0, rng=rng
    )

    assert weekend_count < weekday_count


def test_catalog_target_response_time_increases_with_load() -> None:
    rng = random.Random(0)
    low_load = catalog_target_response_time(
        request_count=0, base_ms=120, max_rpm=150, load_sensitivity=1.5, noise_std=0, rng=rng
    )
    high_load = catalog_target_response_time(
        request_count=150, base_ms=120, max_rpm=150, load_sensitivity=1.5, noise_std=0, rng=rng
    )

    assert high_load > low_load


def test_build_burst_schedule_produces_non_overlapping_windows() -> None:
    rng = random.Random(0)
    schedule = build_burst_schedule(
        total_seconds=7 * 86400,
        peak_min=100,
        peak_max=400,
        burst_min_minutes=2,
        burst_max_minutes=10,
        gap_min_minutes=30,
        gap_max_minutes=240,
        rng=rng,
    )

    assert len(schedule) > 0
    for (start, end, _peak), (next_start, _next_end, _next_peak) in zip(schedule, schedule[1:]):
        assert end <= next_start


def test_thumbnail_invocations_higher_inside_burst_window() -> None:
    rng = random.Random(0)
    schedule = [(100.0, 200.0, 300.0)]

    inside = thumbnail_invocations(150.0, schedule, baseline=2.0, noise_std=0, rng=rng)
    outside = thumbnail_invocations(50.0, schedule, baseline=2.0, noise_std=0, rng=rng)

    assert inside > outside


def test_thumbnail_concurrent_executions_scales_with_invocations_and_duration() -> None:
    assert thumbnail_concurrent_executions(60.0, avg_duration_seconds=1.0) == 1.0
    assert thumbnail_concurrent_executions(60.0, avg_duration_seconds=0.1) < thumbnail_concurrent_executions(
        60.0, avg_duration_seconds=1.0
    )


def test_generate_metrics_covers_full_range_with_five_metrics_per_interval() -> None:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    days = 1
    interval_seconds = 3600  # keep the test fast: 24 intervals instead of 1440

    rows = generate_metrics(start, days, interval_seconds, seed=42)

    expected_intervals = (days * 86400) // interval_seconds
    assert len(rows) == expected_intervals * 5
    assert all(row[4] == "synthetic" for row in rows)
    assert min(row[0] for row in rows) == start
    assert max(row[0] for row in rows) == start + timedelta(seconds=(expected_intervals - 1) * interval_seconds)


def test_generate_metrics_is_deterministic_for_a_given_seed() -> None:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    first = generate_metrics(start, days=1, interval_seconds=3600, seed=7)
    second = generate_metrics(start, days=1, interval_seconds=3600, seed=7)

    assert first == second


@patch("forecasting.data_pipeline.synthetic_data.write_metrics")
def test_run_writes_generated_rows(mock_write_metrics: MagicMock) -> None:
    mock_write_metrics.return_value = 120
    conn = MagicMock()
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)

    written = run(conn, start, days=1, interval_seconds=3600, seed=1)

    assert written == 120
    mock_write_metrics.assert_called_once()
    written_conn, written_rows = mock_write_metrics.call_args.args
    assert written_conn is conn
    assert len(written_rows) == 24 * 5
