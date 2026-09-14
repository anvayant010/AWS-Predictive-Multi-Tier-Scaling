"""Fabricates historical metrics directly into TimescaleDB.

Real AWS infra is ephemeral (see docs/design.md Sec 2.1), so this is the primary data
source for developing the Phase 4 forecasting model and Phase 5 decision engine -- not
just a bootstrapping nicety. Traffic shapes mirror load_generator/shapes.py: a daily/
weekly sine-wave cycle for catalog_service, a low baseline with random sharp bursts for
thumbnail_service. All rows are tagged source='synthetic' so they never mix with real
CloudWatch rows (source='cloudwatch').

Concurrency/Throttles modeling note: real thumbnail_service invocations never come close
to naturally hitting this AWS account's Lambda concurrency ceiling (confirmed in Phase 2:
~1675 invocations/min against a ~0.05s handler duration implies ~1.4 concurrent executions
at most) -- the ceiling of 10 seen in practice is an account-wide quota artifact, not a
demand signal. So ConcurrentExecutions here is derived from invocations * avg handler
duration with no artificial cap, and Throttles is always 0 -- this generator models
natural, unconstrained demand for the forecasting model to learn from.
"""

from __future__ import annotations

import math
import os
import random
from datetime import datetime, timedelta, timezone

from psycopg2.extensions import connection as Connection

from forecasting.data_pipeline.cloudwatch_pull import MetricRow, write_metrics
from forecasting.data_pipeline.db import get_connection

SECONDS_PER_DAY = 86400
BurstSchedule = list[tuple[float, float, float]]


def catalog_request_count(
    t: datetime,
    *,
    min_rpm: float,
    max_rpm: float,
    weekend_factor: float,
    noise_std: float,
    rng: random.Random,
) -> float:
    time_of_day_fraction = (t.hour * 3600 + t.minute * 60 + t.second) / SECONDS_PER_DAY
    amplitude = (max_rpm - min_rpm) / 2
    midpoint = min_rpm + amplitude
    # Phase-shifted so the peak lands around midday rather than at fraction=0.
    daily = midpoint + amplitude * math.sin(2 * math.pi * (time_of_day_fraction - 0.25))
    weekday_factor = weekend_factor if t.weekday() >= 5 else 1.0
    return max(0.0, daily * weekday_factor + rng.gauss(0, noise_std))


def catalog_target_response_time(
    request_count: float,
    *,
    base_ms: float,
    max_rpm: float,
    load_sensitivity: float,
    noise_std: float,
    rng: random.Random,
) -> float:
    load_fraction = min(1.0, request_count / max_rpm) if max_rpm else 0.0
    latency_ms = base_ms + load_sensitivity * load_fraction * base_ms + rng.gauss(0, noise_std)
    return max(1.0, latency_ms) / 1000.0  # CloudWatch reports TargetResponseTime in seconds


def build_burst_schedule(
    total_seconds: float,
    *,
    peak_min: float,
    peak_max: float,
    burst_min_minutes: float,
    burst_max_minutes: float,
    gap_min_minutes: float,
    gap_max_minutes: float,
    rng: random.Random,
) -> BurstSchedule:
    schedule: BurstSchedule = []
    t = rng.uniform(gap_min_minutes, gap_max_minutes) * 60
    while t < total_seconds:
        burst_len = rng.uniform(burst_min_minutes, burst_max_minutes) * 60
        peak = rng.uniform(peak_min, peak_max)
        schedule.append((t, t + burst_len, peak))
        t += burst_len + rng.uniform(gap_min_minutes, gap_max_minutes) * 60
    return schedule


def thumbnail_invocations(
    elapsed_seconds: float,
    schedule: BurstSchedule,
    *,
    baseline: float,
    noise_std: float,
    rng: random.Random,
) -> float:
    for start, end, peak in schedule:
        if start <= elapsed_seconds < end:
            return max(0.0, peak + rng.gauss(0, noise_std))
    return max(0.0, baseline + rng.gauss(0, noise_std * 0.2))


def thumbnail_concurrent_executions(invocations_per_minute: float, *, avg_duration_seconds: float) -> float:
    return invocations_per_minute / 60.0 * avg_duration_seconds


def generate_metrics(
    start: datetime,
    days: int,
    interval_seconds: int = 60,
    *,
    seed: int | None = None,
    catalog_min_rpm: float = 5.0,
    catalog_max_rpm: float = 150.0,
    catalog_weekend_factor: float = 0.5,
    catalog_noise_std: float = 5.0,
    catalog_base_latency_ms: float = 120.0,
    catalog_latency_load_sensitivity: float = 1.5,
    catalog_latency_noise_std: float = 15.0,
    thumbnail_baseline_invocations: float = 2.0,
    thumbnail_peak_min: float = 100.0,
    thumbnail_peak_max: float = 400.0,
    thumbnail_burst_min_minutes: float = 2.0,
    thumbnail_burst_max_minutes: float = 10.0,
    thumbnail_gap_min_minutes: float = 30.0,
    thumbnail_gap_max_minutes: float = 240.0,
    thumbnail_noise_std: float = 3.0,
    thumbnail_avg_duration_seconds: float = 0.1,
) -> list[MetricRow]:
    rng = random.Random(seed)
    total_seconds = days * SECONDS_PER_DAY
    schedule = build_burst_schedule(
        total_seconds,
        peak_min=thumbnail_peak_min,
        peak_max=thumbnail_peak_max,
        burst_min_minutes=thumbnail_burst_min_minutes,
        burst_max_minutes=thumbnail_burst_max_minutes,
        gap_min_minutes=thumbnail_gap_min_minutes,
        gap_max_minutes=thumbnail_gap_max_minutes,
        rng=rng,
    )

    rows: list[MetricRow] = []
    elapsed = 0
    while elapsed < total_seconds:
        t = start + timedelta(seconds=elapsed)

        request_count = catalog_request_count(
            t,
            min_rpm=catalog_min_rpm,
            max_rpm=catalog_max_rpm,
            weekend_factor=catalog_weekend_factor,
            noise_std=catalog_noise_std,
            rng=rng,
        )
        response_time = catalog_target_response_time(
            request_count,
            base_ms=catalog_base_latency_ms,
            max_rpm=catalog_max_rpm,
            load_sensitivity=catalog_latency_load_sensitivity,
            noise_std=catalog_latency_noise_std,
            rng=rng,
        )
        rows.append((t, "catalog_service", "RequestCount", round(request_count, 2), "synthetic"))
        rows.append((t, "catalog_service", "TargetResponseTime", round(response_time, 4), "synthetic"))

        invocations = thumbnail_invocations(
            elapsed,
            schedule,
            baseline=thumbnail_baseline_invocations,
            noise_std=thumbnail_noise_std,
            rng=rng,
        )
        concurrent = thumbnail_concurrent_executions(
            invocations, avg_duration_seconds=thumbnail_avg_duration_seconds
        )
        rows.append((t, "thumbnail_service", "Invocations", round(invocations, 2), "synthetic"))
        rows.append((t, "thumbnail_service", "ConcurrentExecutions", round(concurrent, 2), "synthetic"))
        rows.append((t, "thumbnail_service", "Throttles", 0.0, "synthetic"))

        elapsed += interval_seconds

    return rows


def run(conn: Connection, start: datetime, days: int, interval_seconds: int = 60, seed: int | None = None) -> int:
    rows = generate_metrics(start, days, interval_seconds, seed=seed)
    return write_metrics(conn, rows)


if __name__ == "__main__":
    _days = int(os.environ.get("SYNTHETIC_DAYS", "14"))
    _interval_seconds = int(os.environ.get("SYNTHETIC_INTERVAL_SECONDS", "60"))
    _seed_env = os.environ.get("SYNTHETIC_SEED")
    _seed = int(_seed_env) if _seed_env else None
    _end = datetime.now(timezone.utc)
    _start = _end - timedelta(days=_days)
    written = run(get_connection(), _start, _days, _interval_seconds, seed=_seed)
    print(f"wrote {written} rows")
