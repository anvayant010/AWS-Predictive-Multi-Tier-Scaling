"""Rule-based load-shape classifier: label a forecast window quiet / sustained / bursty.

Each service maps to one compute primitive (catalog -> EC2, thumbnail -> Lambda), so this does not
choose a tier; it chooses a *mode within the tier*. The one place the label changes behaviour today
is EC2: a bursty window means a short spike that will be over before a new instance can boot
(~3-6 min), so the EC2 policy sizes to the typical level instead of chasing the peak. Lambda keeps
sizing to the peak, since pre-warming for short spikes is exactly what provisioned concurrency is for.

Pure function of the forecast and a config — no state — so it is trivially testable. Thresholds
are per-service because the services' demand scales differ (catalog ~5-150 req/min, thumbnail
~2-400 invocations/min).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from decision_engine.models import QUANTILE_NAMES, Classification, Forecast, LoadShape


@dataclass(frozen=True)
class ClassifierConfig:
    quiet_below: float  # window peak under this (in the service's own units) => QUIET
    burst_ratio: float = 3.0  # peak must be at least this multiple of the typical level...
    max_spike_fraction: float = 0.4  # ...and sit up high for at most this share of the window
    series: str = "p90"  # classify on a cautious quantile: missing a burst costs more than a false alarm

    def __post_init__(self) -> None:
        if self.quiet_below < 0:
            raise ValueError("quiet_below must be non-negative")
        if self.burst_ratio <= 1:
            raise ValueError("burst_ratio must be greater than 1")
        if not 0 < self.max_spike_fraction < 1:
            raise ValueError("max_spike_fraction must be between 0 and 1")
        if self.series not in QUANTILE_NAMES:
            raise ValueError(f"series must be one of {QUANTILE_NAMES}")


# Assumed starting thresholds, to be checked against the dry-run output (Phase 5c).
CATALOG_CLASSIFIER = ClassifierConfig(quiet_below=10.0)
THUMBNAIL_CLASSIFIER = ClassifierConfig(quiet_below=20.0)

_LEVEL_FLOOR = 1.0  # avoids dividing by ~0 when the typical level is essentially idle


def classify(forecast: Forecast, config: ClassifierConfig) -> Classification:
    values = forecast.quantile(config.series)
    level = statistics.median(values)
    peak = max(values)

    # Steps in the upper half between the typical level and the peak: how long the "high" lasts.
    high_cutoff = level + 0.5 * (peak - level)
    elevated = sum(1 for v in values if v >= high_cutoff and v > level)

    if peak < config.quiet_below:
        shape = LoadShape.QUIET
        reason = f"{config.series} peak {peak:.1f} is below the quiet threshold {config.quiet_below:g}"
    elif peak / max(level, _LEVEL_FLOOR) >= config.burst_ratio and (
        elevated / len(values) <= config.max_spike_fraction
    ):
        shape = LoadShape.BURSTY
        reason = (
            f"{config.series} peak {peak:.1f} is {peak / max(level, _LEVEL_FLOOR):.1f}x the typical "
            f"level {level:.1f} and stays high for only {elevated}/{len(values)} steps"
        )
    else:
        shape = LoadShape.SUSTAINED
        reason = f"{config.series} level {level:.1f}, peak {peak:.1f}: elevated load that persists or varies gradually"

    return Classification(shape=shape, level=level, peak=peak, elevated_steps=elevated, reason=reason)
