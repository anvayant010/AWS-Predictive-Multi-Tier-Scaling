from __future__ import annotations

import math
import os
import random

from locust import LoadTestShape


class SeasonalLoadShape(LoadTestShape):
    """Compressed daily/weekly cycle for catalog_service: user count follows a sine wave."""

    min_users = int(os.environ.get("SEASONAL_MIN_USERS", 2))
    max_users = int(os.environ.get("SEASONAL_MAX_USERS", 30))
    period_seconds = int(os.environ.get("SEASONAL_PERIOD_SECONDS", 300))
    duration_seconds = int(os.environ.get("SEASONAL_DURATION_SECONDS", 1800))
    spawn_rate = max(1, max_users // 5)

    def tick(self):
        run_time = self.get_run_time()
        if run_time > self.duration_seconds:
            return None

        amplitude = (self.max_users - self.min_users) / 2
        midpoint = self.min_users + amplitude
        phase = (run_time % self.period_seconds) / self.period_seconds
        user_count = round(midpoint + amplitude * math.sin(2 * math.pi * phase))
        return user_count, self.spawn_rate


class BurstyLoadShape(LoadTestShape):
    """Flash-sale/viral pattern for thumbnail_service: low baseline, random sharp spikes."""

    baseline_users = int(os.environ.get("BURST_BASELINE_USERS", 1))
    peak_users_min = int(os.environ.get("BURST_PEAK_USERS_MIN", 20))
    peak_users_max = int(os.environ.get("BURST_PEAK_USERS_MAX", 60))
    burst_duration_min_seconds = int(os.environ.get("BURST_DURATION_MIN_SECONDS", 10))
    burst_duration_max_seconds = int(os.environ.get("BURST_DURATION_MAX_SECONDS", 30))
    gap_min_seconds = int(os.environ.get("BURST_GAP_MIN_SECONDS", 30))
    gap_max_seconds = int(os.environ.get("BURST_GAP_MAX_SECONDS", 120))
    duration_seconds = int(os.environ.get("BURST_TOTAL_DURATION_SECONDS", 1800))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._schedule = self._build_schedule()

    def _build_schedule(self) -> list[tuple[float, float, int]]:
        schedule = []
        t = random.uniform(self.gap_min_seconds, self.gap_max_seconds)
        while t < self.duration_seconds:
            burst_len = random.uniform(self.burst_duration_min_seconds, self.burst_duration_max_seconds)
            peak = random.randint(self.peak_users_min, self.peak_users_max)
            schedule.append((t, t + burst_len, peak))
            t += burst_len + random.uniform(self.gap_min_seconds, self.gap_max_seconds)
        return schedule

    def tick(self):
        run_time = self.get_run_time()
        if run_time > self.duration_seconds:
            return None

        for start, end, peak in self._schedule:
            if start <= run_time < end:
                return peak, peak
        return self.baseline_users, self.baseline_users
