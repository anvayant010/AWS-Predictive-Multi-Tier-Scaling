"""Turn the raw request logs of one comparison run into the numbers the comparison is judged on.

Input: the JSON-lines files written by load_generator/scenario_locustfile.py (one row per request) and the
saved scenario they were generated from. Output: one plain dict per run (JSON-serialisable) that the
analysis step (Phase 7 step 7) reads for every run of every arm. Pure functions: no AWS, no network, no
database.

Conventions (fixed here so every arm is measured identically):

* Latency percentiles use only SUCCESSFUL requests (HTTP 200), numpy's default linear interpolation, in
  milliseconds. Failures are never hidden: they are reported next to the percentiles as an error count/rate,
  and `bad_rate` (failed OR slower than `slo_ms`, when given) puts them on one scale. `p95_incl_errors` treats
  every failed request as infinitely slow (so it is None whenever more than 5% of requests failed).
* Rows are placed in time by the `minute` the request was sent in (the scenario minute), not by when the
  reply came back.
* "Load fidelity": how much of the offered load the load generator really delivered. A comparison where one
  arm's laptop fell behind would be measuring the laptop, so fidelity is reported with every run.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from comparison.scenario import CATALOG, THUMBNAIL, ServiceScenario

REQUIRED_COLUMNS = ("t", "service", "name", "ms", "ok", "status", "minute", "offered_rpm", "cold", "container", "init_type")
PROVISIONED = "provisioned-concurrency"

# Load fidelity thresholds: below these the run's load cannot be trusted as "the scenario".
MIN_TOTAL_FIDELITY = 0.95
MIN_MINUTE_FIDELITY = 0.80
MIN_EXPECTED_FOR_MINUTE_CHECK = 100  # a minute expecting fewer requests is too noisy to judge on its own

# D7 stopping rule: two runs of an arm "disagree" if a key metric differs by more than this fraction.
DISAGREEMENT_TOLERANCE = 0.20


# ------------------------------------------------------------------------------------------------ helpers


def _clean(value):
    """JSON-safe: numpy scalars -> python, NaN/inf -> None."""
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def load_frame(rows: list[dict]) -> pd.DataFrame:
    """Rows -> DataFrame, refusing logs that lack a column the analysis depends on."""
    if not rows:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))
    frame = pd.DataFrame(rows)
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"request log is missing columns: {missing}")
    return frame


def percentiles(values) -> dict[str, float | None]:
    arr = np.asarray(list(values), dtype="float64")
    if arr.size == 0:
        return {"p50": None, "p95": None, "p99": None}
    p50, p95, p99 = np.percentile(arr, [50, 95, 99])
    return {"p50": float(p50), "p95": float(p95), "p99": float(p99)}


def _latency_block(df: pd.DataFrame, slo_ms: float | None) -> dict:
    ok = df[df["ok"].astype(bool)]
    sent = len(df)
    errors = sent - len(ok)
    block = {
        "sent": sent,
        "errors": errors,
        "error_rate": (errors / sent) if sent else None,
        **percentiles(ok["ms"].to_numpy()),
        "p95_incl_errors": None,
        "status_counts": {str(k): int(v) for k, v in df["status"].fillna(0).astype(int).value_counts().items()},
    }
    if sent:
        # Failures count as infinitely slow; nearest-rank 95th percentile of all requests, so the result
        # is finite only if at most 5% of the requests failed.
        ordered = np.sort(np.concatenate([ok["ms"].to_numpy(dtype="float64"), np.full(errors, np.inf)]))
        p95_all = float(ordered[math.ceil(0.95 * len(ordered)) - 1])
        block["p95_incl_errors"] = None if math.isinf(p95_all) else p95_all
    if slo_ms is not None:
        slow = int((ok["ms"] > slo_ms).sum())
        block["bad_rate"] = ((errors + slow) / sent) if sent else None
    return block


# ----------------------------------------------------------------------------------------- per-minute view


def per_minute_table(df: pd.DataFrame, offered_rpm: np.ndarray, slo_ms: float | None = None) -> pd.DataFrame:
    """One row per scenario minute: what was offered, what was sent, errors and latency."""
    rows = []
    by_minute = {m: g for m, g in df.groupby("minute")} if len(df) else {}
    for minute, offered in enumerate(offered_rpm):
        group = by_minute.get(minute)
        if group is None:
            rows.append({"minute": minute, "offered_rpm": float(offered), "sent": 0, "errors": 0, "p50": None, "p95": None, "p99": None})
            continue
        block = _latency_block(group, slo_ms)
        rows.append(
            {
                "minute": minute,
                "offered_rpm": float(offered),
                "sent": block["sent"],
                "errors": block["errors"],
                "p50": block["p50"],
                "p95": block["p95"],
                "p99": block["p99"],
                **({"bad_rate": block["bad_rate"]} if slo_ms is not None else {}),
            }
        )
    return pd.DataFrame(rows)


def load_fidelity(table: pd.DataFrame) -> dict:
    """Did the load generator deliver the offered load? (Poisson noise is expected, hence the thresholds.)"""
    offered_total = float(table["offered_rpm"].sum())
    sent_total = float(table["sent"].sum())
    total = (sent_total / offered_total) if offered_total > 0 else None
    judged = table[table["offered_rpm"] >= MIN_EXPECTED_FOR_MINUTE_CHECK]
    worst = float((judged["sent"] / judged["offered_rpm"]).min()) if len(judged) else None
    ok = (total is None or total >= MIN_TOTAL_FIDELITY) and (worst is None or worst >= MIN_MINUTE_FIDELITY)
    return {"total": total, "worst_minute": worst, "minutes_judged": int(len(judged)), "ok": bool(ok)}


# ----------------------------------------------------------------------------------------------- services


def summarize_catalog(rows: list[dict], scenario: ServiceScenario, slo_ms: float | None = None) -> dict:
    df = load_frame(rows)
    table = per_minute_table(df, scenario.offered_rpm.to_numpy(dtype="float64"), slo_ms)
    return {
        "service": CATALOG,
        "overall": _latency_block(df, slo_ms),
        "fidelity": load_fidelity(table),
        "per_minute": table.to_dict(orient="records"),
    }


def summarize_thumbnail(rows: list[dict], scenario: ServiceScenario, slo_ms: float | None = None) -> dict:
    df = load_frame(rows)
    table = per_minute_table(df, scenario.offered_rpm.to_numpy(dtype="float64"), slo_ms)
    origin = scenario.series.index[0]

    def offset(ts: pd.Timestamp) -> int:
        return int((ts - origin) / pd.Timedelta(minutes=1))

    cold = df["cold"] == True  # noqa: E712  (the column holds True/False/None)
    inside = pd.Series(False, index=df.index)
    bursts = []
    for e in scenario.episodes:
        seg = (df["minute"] >= offset(e.segment_start)) & (df["minute"] <= offset(e.segment_end))
        loud = (df["minute"] >= offset(e.onset)) & (df["minute"] <= offset(e.end))
        inside |= seg
        in_seg = df[seg]
        bursts.append(
            {
                "burst": e.index,
                "peak_rpm": e.peak,
                "segment": _latency_block(in_seg, slo_ms),
                "loud_minutes": _latency_block(df[loud], slo_ms),
                "cold_starts": int((cold & seg).sum()),
                "cold_starts_before_onset": int((cold & seg & (df["minute"] < offset(e.onset))).sum()),
            }
        )

    ok = df["ok"].astype(bool) if len(df) else pd.Series(dtype=bool)
    cold_ok = df[ok & cold] if len(df) else df
    warm_ok = df[ok & (df["cold"] == False)] if len(df) else df  # noqa: E712
    cold_counts = [b["cold_starts"] for b in bursts]
    served = df["init_type"].value_counts() if len(df) else pd.Series(dtype=int)
    n_bursts = len(bursts)
    return {
        "service": THUMBNAIL,
        "overall": _latency_block(df, slo_ms),
        "fidelity": load_fidelity(table),
        "cold_starts_total": int(cold.sum()),
        "cold_starts_in_bursts": int(sum(cold_counts)),
        "cold_starts_outside_bursts": int((cold & ~inside).sum()),
        "cold_starts_per_burst": (sum(cold_counts) / n_bursts) if n_bursts else None,
        "cold_start_ms_median": float(cold_ok["ms"].median()) if len(cold_ok) else None,
        "warm_ms_median": float(warm_ok["ms"].median()) if len(warm_ok) else None,
        "containers_seen": int(df["container"].dropna().nunique()) if len(df) else 0,
        "unknown_marker_requests": int(df["cold"].isna().sum()) if len(df) else 0,
        "served_by_provisioned_fraction": (
            float(served.get(PROVISIONED, 0) / served.sum()) if len(served) and served.sum() else None
        ),
        "bursts": bursts,
        "per_minute": table.to_dict(orient="records"),
    }


# ------------------------------------------------------------------------------------------------ one run


def summarize_run(
    *,
    arm: str,
    run_index: int,
    request_logs: dict[str, list[dict]],
    scenarios: dict[str, ServiceScenario],
    fingerprint: str,
    slo_ms: float | None = None,
    extra: dict | None = None,
) -> dict:
    """The record kept for one real (or simulated) run. `request_logs` maps service -> rows."""
    services: dict[str, dict] = {}
    if CATALOG in request_logs:
        services[CATALOG] = summarize_catalog(request_logs[CATALOG], scenarios[CATALOG], slo_ms)
    if THUMBNAIL in request_logs:
        services[THUMBNAIL] = summarize_thumbnail(request_logs[THUMBNAIL], scenarios[THUMBNAIL], slo_ms)
    if not services:
        raise ValueError("no request logs given")
    return _clean(
        {
            "arm": arm,
            "run_index": run_index,
            "scenario_fingerprint": fingerprint,
            "slo_ms": slo_ms,
            "services": services,
            "extra": extra or {},
        }
    )


def write_result(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8", newline="")


def read_result(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------------------- D7: agreement


def key_metrics(result: dict) -> dict[str, float | None]:
    """The numbers the D7 stopping rule compares: P95 latency per service and cold starts per burst."""
    out: dict[str, float | None] = {}
    for name, svc in result["services"].items():
        out[f"{name}.p95_ms"] = svc["overall"]["p95"]
        if name == THUMBNAIL:
            out[f"{name}.cold_starts_per_burst"] = svc["cold_starts_per_burst"]
    return out


def relative_difference(a: float, b: float) -> float:
    """|a-b| as a fraction of the larger value; 0 when both are 0 (identical), so it is symmetric."""
    top = max(abs(a), abs(b))
    return 0.0 if top == 0 else abs(a - b) / top


def runs_disagree(a: dict, b: dict, tolerance: float = DISAGREEMENT_TOLERANCE) -> list[str]:
    """Metrics on which two runs of the same arm differ by more than `tolerance` (empty = they agree).

    Fires on disagreement in EITHER direction, and a metric missing from one run counts as a disagreement
    rather than being skipped (an unmeasured number must not quietly pass the rule).
    """
    if a["arm"] != b["arm"]:
        raise ValueError("the agreement rule compares two runs of the same arm")
    if a["scenario_fingerprint"] != b["scenario_fingerprint"]:
        raise ValueError("runs used different scenarios, so they cannot be compared")
    ka, kb = key_metrics(a), key_metrics(b)
    reasons = []
    for key in sorted(set(ka) | set(kb)):
        va, vb = ka.get(key), kb.get(key)
        if va is None or vb is None:
            reasons.append(f"{key}: not measured in both runs")
        elif relative_difference(va, vb) > tolerance:
            reasons.append(f"{key}: {va:.4g} vs {vb:.4g} ({relative_difference(va, vb):.0%} apart)")
    return reasons
