"""The Phase 7 numbers that calibration and the plan-change decisions fixed, in one place (with where each came from).

Everything here was decided BEFORE any comparison run, so none of it was tuned to make an arm look good. Where a
value is a declared choice rather than a measurement, the comment says so; the README must repeat those.

Calibration sessions (2026-10-15, raw data under calibration_output/):
* 20261015-131116  first calibration (cold starts, idle lifetime, boot, provisioned-concurrency readiness)
* recheck-20261015-174739  connection re-test: the first session's 3200 rpm "knee" was a connection-set-up
  artifact of the calibration client, not instance capacity
* knee-20261015-200441  knee hunt: ONE t3.micro served 25,000 rpm with reused connections at flat latency
  (P95 ~280 ms, 0% slow, CPU ~27%); no knee found, so the instance cannot be saturated from this laptop
"""

from __future__ import annotations

import pandas as pd

from comparison.cost import LambdaShape
from comparison.scenario import (
    CATALOG,
    THUMBNAIL,
    CatalogParams,
    Scenario,
    ThumbnailParams,
    build_catalog_scenario,
    build_thumbnail_scenario,
)

# --- measured -----------------------------------------------------------------------------------------------------------------
BILLED_MS_PER_REQUEST = 52.0  # Lambda billed duration, warm calls at 128 MB (REPORT lines)
COLD_START_PENALTY_MS = 431.0  # client-side cold minus warm; AWS Init Duration median was 92.7 ms
INSTANCE_BOOT_SECONDS = 92.5  # ASG desired 1 -> 2 until healthy in the ALB (88.3 / 92.5 / 93.2)
PROVISIONED_READY_SECONDS = 178.0  # provisioned concurrency request -> READY, median (177.6 / 178.3 / 209.4)
CPU_UTILIZATION_UPPER = 0.27  # 27% = measured at 25,000 rpm, ~5x this scenario's peak: a deliberate UPPER bound for the T3 surcharge
UNLOADED_P95_MS = 324.0  # catalog, reused connections, lowest step (earlier sessions: 287); the larger one is used
SLO_MS = 650.0  # 2 x unloaded P95 (2 x 324 = 648, rounded up): a request slower than this counts as "bad" (see comparison/results.py)

# --- declared (NOT measured) --------------------------------------------------------------------------------------------------
# The real instance is far stronger than anything this laptop can send, so latency cannot be stressed on the EC2 side.
# Both scaling arms are therefore sized on this DECLARED per-instance capacity (the same 0.7 factor for both, decision D3).
CATALOG_DECLARED_CAPACITY_RPM = 2000.0
UTILIZATION = 0.7
CATALOG_TARGET_PEAK_INSTANCES = 4

# --- scenario choices (rules fixed before results; D4 + the 2026-10-15 plan changes) --------------------------------------------
GAP_MINUTES = 15  # first idle gap in the staircase where NO container survived (3 min 83%, 6 min 40%, 10 min 50%, 15+ min 0%)
N_BURSTS = 5  # was 8: each burst segment is ~25 min + 15 min gap, so 5 bursts = ~3.5 h per run; the simulator carries volume
LAMBDA_TARGET_PEAK_CONCURRENCY = 8.0  # the loudest selected burst needs 8 containers (cap is 10); at scale 1 it would need 0.35


def thumbnail_params() -> ThumbnailParams:
    return ThumbnailParams(
        n_bursts=N_BURSTS,
        gap_minutes=GAP_MINUTES,
        target_peak_concurrency=LAMBDA_TARGET_PEAK_CONCURRENCY,
        billed_seconds=BILLED_MS_PER_REQUEST / 1000.0,
    )


def catalog_params() -> CatalogParams:
    return CatalogParams(
        capacity_rpm_per_instance=CATALOG_DECLARED_CAPACITY_RPM,
        utilization=UTILIZATION,
        target_peak_instances=CATALOG_TARGET_PEAK_INSTANCES,
    )


def lambda_shape() -> LambdaShape:
    return LambdaShape(memory_mb=128, billed_ms_per_request=BILLED_MS_PER_REQUEST)


def build_pinned_scenario(thumbnail_series: pd.Series, catalog_series: pd.Series) -> Scenario:
    """The scenario every comparison arm is run against, from the recorded (synthetic) series."""
    return Scenario(
        services={
            THUMBNAIL: build_thumbnail_scenario(thumbnail_series, thumbnail_params()),
            CATALOG: build_catalog_scenario(catalog_series, catalog_params()),
        }
    )
