"""Phase 7 calibration: turn the raw measurements of one calibration session into the numbers the comparison needs.

Pure functions only (no AWS, no network). The session itself lives in comparison/calibration_stages.py and
scripts/run_calibration.py; this module is what decides what the measurements MEAN, so the rules are written
down and tested before any AWS money is spent (decision D5).

What it derives, and the rule used:

* CAPACITY KNEE (catalog): load is raised in steps against one fixed instance. The unloaded P95 is the P95 of
  the lowest step. A step "passes" if its P95 is within `latency_factor` (2x) of the unloaded P95 AND its error
  rate is below `max_error_rate` (1%) AND the load generator really delivered the offered load. The knee is
  the highest step such that it and every lower step pass. It is a measured step, never interpolated, so it
  is conservative. If no step failed, the knee is only a LOWER BOUND and is flagged as such; if the first
  failing step failed because the generator could not deliver, the result is flagged generator-limited.
* COLD START (thumbnail): cold vs warm client-side latency, and AWS's own `Init Duration` from the REPORT log
  lines, so the marker the handler returns can be cross-checked against AWS's records.
* IDLE-CONTAINER LIFETIME (thumbnail): a staircase of growing idle gaps. For each gap, what fraction of the
  containers known before the gap answered again afterwards. The lifetime is bracketed between the longest gap
  where at least half survived and the shortest gap where fewer did; the recommended scenario gap is that
  shortest failing gap ("just above the lifetime", decision D4). If nothing died, the lifetime is only known
  to exceed the longest gap tested, and no gap is recommended (use fewer bursts, not shorter gaps).
* TIMINGS (instance boot, provisioned-concurrency readiness): the spread of repeated measurements.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

import numpy as np

LATENCY_FACTOR = 2.0
MAX_ERROR_RATE = 0.01
MIN_FIDELITY = 0.95
SURVIVAL_THRESHOLD = 0.5

REPORT_RE = re.compile(r"REPORT RequestId:\s*(?P<id>\S+)\s+(?P<body>.*)")
FIELD_RE = re.compile(r"(?P<key>Duration|Billed Duration|Memory Size|Max Memory Used|Init Duration):\s*(?P<value>[\d.]+)\s*(?P<unit>ms|MB)")


# ------------------------------------------------------------------------------------------- capacity knee


@dataclass(frozen=True)
class LoadStep:
    """One constant-rate step, measured after its settle period."""

    offered_rpm: float
    sent: int
    errors: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    duration_s: float

    @property
    def error_rate(self) -> float:
        return (self.errors / self.sent) if self.sent else 1.0

    @property
    def achieved_rpm(self) -> float:
        return self.sent / (self.duration_s / 60.0) if self.duration_s > 0 else 0.0

    @property
    def fidelity(self) -> float:
        return (self.achieved_rpm / self.offered_rpm) if self.offered_rpm > 0 else 1.0


def summarize_step(rows: list[dict], offered_rpm: float, duration_s: float) -> LoadStep:
    """Rows of one step (dicts with `ms` and `ok`) -> LoadStep. P-values use successful requests only."""
    ok_ms = np.array([r["ms"] for r in rows if r["ok"]], dtype="float64")
    p50, p95, p99 = (float(x) for x in np.percentile(ok_ms, [50, 95, 99])) if ok_ms.size else (None, None, None)
    return LoadStep(offered_rpm, len(rows), len(rows) - int(ok_ms.size), p50, p95, p99, duration_s)


def step_passes(step: LoadStep, unloaded_p95: float, latency_factor: float, max_error_rate: float, min_fidelity: float) -> tuple[bool, str]:
    if step.fidelity < min_fidelity:
        return False, "generator_limited"
    if step.error_rate >= max_error_rate:
        return False, f"errors {step.error_rate:.1%} >= {max_error_rate:.0%}"
    if step.p95_ms is None or step.p95_ms > latency_factor * unloaded_p95:
        got = "no successful requests" if step.p95_ms is None else f"p95 {step.p95_ms:.0f} ms > {latency_factor:g}x {unloaded_p95:.0f} ms"
        return False, got
    return True, "ok"


def find_knee(
    steps: list[LoadStep],
    *,
    latency_factor: float = LATENCY_FACTOR,
    max_error_rate: float = MAX_ERROR_RATE,
    min_fidelity: float = MIN_FIDELITY,
) -> dict:
    if not steps:
        raise ValueError("no load steps to analyse")
    ordered = sorted(steps, key=lambda s: s.offered_rpm)
    base = ordered[0]
    if base.p95_ms is None or base.error_rate >= max_error_rate or base.fidelity < min_fidelity:
        raise ValueError("the lowest step is itself unhealthy, so there is no unloaded baseline to compare against")
    unloaded_p95 = base.p95_ms

    table, knee, failed_reason, failed_at = [], None, None, None
    for step in ordered:
        passed, reason = step_passes(step, unloaded_p95, latency_factor, max_error_rate, min_fidelity)
        table.append(
            {
                "offered_rpm": step.offered_rpm, "achieved_rpm": round(step.achieved_rpm, 1), "fidelity": round(step.fidelity, 3),
                "sent": step.sent, "error_rate": step.error_rate, "p50_ms": step.p50_ms, "p95_ms": step.p95_ms,
                "p99_ms": step.p99_ms, "passes": passed, "reason": reason,
            }
        )
        if failed_at is None:
            if passed:
                knee = step.offered_rpm
            else:
                failed_at, failed_reason = step.offered_rpm, reason
    return {
        "knee_rpm": knee,
        "limit_reached": failed_at is not None,
        "knee_is_lower_bound": failed_at is None,
        "generator_limited": failed_reason == "generator_limited",
        "first_failing_rpm": failed_at,
        "first_failing_reason": failed_reason,
        "unloaded_p95_ms": unloaded_p95,
        "slo_ms": latency_factor * unloaded_p95,
        "rule": {"latency_factor": latency_factor, "max_error_rate": max_error_rate, "min_fidelity": min_fidelity},
        "steps": table,
    }


# ----------------------------------------------------------------------------------------- cold starts


def cold_start_stats(rows: list[dict]) -> dict:
    """Client-side latency of cold vs warm thumbnail requests (successful ones only)."""
    ok = [r for r in rows if r["ok"]]
    cold = [r["ms"] for r in ok if r.get("cold") is True]
    warm = [r["ms"] for r in ok if r.get("cold") is False]
    out = {
        "cold_n": len(cold), "warm_n": len(warm),
        "unknown_n": sum(1 for r in rows if r.get("cold") is None),
        "cold_median_ms": float(statistics.median(cold)) if cold else None,
        "warm_median_ms": float(statistics.median(warm)) if warm else None,
        "cold_p95_ms": float(np.percentile(cold, 95)) if cold else None,
        "warm_p95_ms": float(np.percentile(warm, 95)) if warm else None,
    }
    out["cold_penalty_ms"] = (
        out["cold_median_ms"] - out["warm_median_ms"]
        if out["cold_median_ms"] is not None and out["warm_median_ms"] is not None else None
    )
    return out


def parse_report_lines(messages: list[str]) -> list[dict]:
    """Lambda REPORT log lines -> dicts. `init_ms` is present only for a cold start (AWS's own record of one)."""
    out = []
    for message in messages:
        m = REPORT_RE.search(message)
        if not m:
            continue
        fields = {f.group("key"): float(f.group("value")) for f in FIELD_RE.finditer(m.group("body"))}
        if "Duration" not in fields:
            continue
        out.append(
            {
                "request_id": m.group("id"),
                "duration_ms": fields["Duration"],
                "billed_ms": fields.get("Billed Duration"),
                "memory_mb": fields.get("Memory Size"),
                "max_memory_mb": fields.get("Max Memory Used"),
                "init_ms": fields.get("Init Duration"),
            }
        )
    return out


def crosscheck_cold_starts(marker_cold_count: int, report_rows: list[dict], tolerance: int = 0) -> dict:
    """Do the handler's cold-start markers agree with AWS's REPORT lines (`Init Duration` = a cold start)?"""
    aws_cold = [r for r in report_rows if r["init_ms"] is not None]
    difference = marker_cold_count - len(aws_cold)
    return {
        "marker_cold_starts": marker_cold_count,
        "aws_init_duration_lines": len(aws_cold),
        "aws_report_lines": len(report_rows),
        "difference": difference,
        "agree": abs(difference) <= tolerance,
        "aws_init_ms_median": float(statistics.median([r["init_ms"] for r in aws_cold])) if aws_cold else None,
        "aws_init_ms_max": max((r["init_ms"] for r in aws_cold), default=None),
    }


def handler_duration_stats(report_rows: list[dict]) -> dict:
    """AWS's billed handler duration, to replace the 100 ms assumption in the cost model (warm requests only)."""
    warm = [r for r in report_rows if r["init_ms"] is None]
    if not warm:
        return {"n": 0, "duration_ms_median": None, "billed_ms_median": None, "memory_mb": None}
    billed = [r["billed_ms"] for r in warm if r["billed_ms"] is not None]
    return {
        "n": len(warm),
        "duration_ms_median": float(statistics.median([r["duration_ms"] for r in warm])),
        "billed_ms_median": float(statistics.median(billed)) if billed else None,
        "memory_mb": warm[0]["memory_mb"],
    }


# ----------------------------------------------------------------------------------- idle-container lifetime


@dataclass(frozen=True)
class IdleProbe:
    """What one staircase step saw: the containers known before the gap and the ones that answered after it."""

    gap_minutes: float
    known_before: frozenset[str]
    seen_after: frozenset[str]

    @property
    def survivors(self) -> int:
        return len(self.known_before & self.seen_after)

    @property
    def survival(self) -> float | None:
        return (self.survivors / len(self.known_before)) if self.known_before else None

    @property
    def reliable(self) -> bool:
        """With too few answering containers a missing one may just mean the probe did not reach it."""
        return bool(self.known_before) and len(self.seen_after) >= max(1, len(self.known_before) - 1)


def idle_lifetime(probes: list[IdleProbe], survival_threshold: float = SURVIVAL_THRESHOLD) -> dict:
    ordered = sorted(probes, key=lambda p: p.gap_minutes)
    usable = [p for p in ordered if p.reliable and p.survival is not None]
    alive = [p.gap_minutes for p in usable if p.survival >= survival_threshold]
    dead = [p.gap_minutes for p in usable if p.survival < survival_threshold]
    # A gap counts as "alive" only below the first death: a container that vanished at 10 min does not
    # come back at 20, so a later "survival" would just be noise from an unreliable probe.
    first_dead = min(dead) if dead else None
    lower = max((g for g in alive if first_dead is None or g < first_dead), default=None)
    return {
        "lifetime_lower_bound_minutes": lower,
        "lifetime_upper_bound_minutes": first_dead,
        "exceeds_longest_tested": first_dead is None and bool(usable),
        "recommended_gap_minutes": first_dead,
        "unreliable_probes": [p.gap_minutes for p in ordered if not p.reliable],
        "probes": [
            {"gap_minutes": p.gap_minutes, "known": len(p.known_before), "answered": len(p.seen_after),
             "survivors": p.survivors, "survival": p.survival, "reliable": p.reliable}
            for p in ordered
        ],
    }


# ----------------------------------------------------------------------------------------------- timings


def timing_stats(seconds: list[float]) -> dict:
    if not seconds:
        return {"n": 0, "min_s": None, "median_s": None, "max_s": None, "spread_pct": None}
    low, high, mid = min(seconds), max(seconds), float(statistics.median(seconds))
    return {"n": len(seconds), "min_s": low, "median_s": mid, "max_s": high, "spread_pct": ((high - low) / mid * 100) if mid else None}


# --------------------------------------------------------------------------------------- final parameters


def derive_parameters(
    *, knee: dict, cold: dict, crosscheck: dict, durations: dict, idle: dict, boot: dict, provisioned_ready: dict,
    cpu_max_percent_at_knee: float | None = None,
) -> dict:
    """The few numbers the rest of Phase 7 takes from calibration, with a list of reasons not to trust them yet."""
    warnings = []
    if knee["knee_rpm"] is None:
        warnings.append("no load step passed: the capacity knee is unknown")
    if knee["knee_is_lower_bound"]:
        warnings.append("the knee is only a lower bound: no step reached the limit, test higher rates")
    if knee["generator_limited"]:
        warnings.append("the load generator could not deliver the offered load: the knee is not trustworthy")
    if not crosscheck["agree"]:
        warnings.append(
            f"cold-start markers ({crosscheck['marker_cold_starts']}) disagree with AWS Init Duration lines ({crosscheck['aws_init_duration_lines']})"
        )
    if idle["recommended_gap_minutes"] is None:
        warnings.append("idle-container lifetime exceeds the longest gap tested (or no reliable probe): use fewer bursts, not shorter gaps")
    if idle["unreliable_probes"]:
        warnings.append(f"unreliable idle probes at gaps {idle['unreliable_probes']}")
    if boot["n"] < 2 or provisioned_ready["n"] < 2:
        warnings.append("boot / provisioned-readiness were measured fewer than 2 times")
    knee_rpm = knee["knee_rpm"]
    return {
        "capacity_rpm_per_instance": knee_rpm,
        "capacity_rpm_per_instance_at_70pct": None if knee_rpm is None else knee_rpm * 0.7,
        "slo_ms": knee["slo_ms"],
        "unloaded_p95_ms": knee["unloaded_p95_ms"],
        "lambda_billed_ms_per_request": durations.get("billed_ms_median"),
        "lambda_cold_penalty_ms": cold.get("cold_penalty_ms"),
        "lambda_aws_init_ms_median": crosscheck.get("aws_init_ms_median"),
        "scenario_gap_minutes": idle["recommended_gap_minutes"],
        "instance_boot_seconds_median": boot["median_s"],
        "instance_boot_seconds_max": boot["max_s"],
        "provisioned_ready_seconds_median": provisioned_ready["median_s"],
        "provisioned_ready_seconds_max": provisioned_ready["max_s"],
        "cpu_max_percent_at_knee": cpu_max_percent_at_knee,
        "trustworthy": not warnings,
        "warnings": warnings,
    }
