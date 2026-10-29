"""Export what the dashboard shows: a few recorded episodes, comparison results, accuracy figures and AWS facts.

NO AWS, NO database. Everything comes from the same in-memory synthetic week and the same decision engine and
replay rules as comparison/replay_compare.py, so the dashboard replays exactly what the comparison counted.

EPISODES are chosen by rules fixed here, never by looking at how well either system did:
  * catalog morning rise : the steepest 2 hours of the first full weekday of the held-out week (the scenario rule);
  * burst, with warning  : the FIRST burst of the held-out week that announced itself: its first loud minute is under
                           RAMP_START_FRACTION of its peak, i.e. traffic was already climbing - a ramp the model can see;
  * burst, no warning    : the FIRST burst that jumped straight to (nearly) full size.
Each episode also carries how typical it is: the same measure across ALL bursts (or the whole week).

UNITS. Traffic and forecasts are exported in the units the load generator would send (natural generator units times the
scenario's load scale). Lambda containers needed = invocations/min x billed seconds / 60. EC2 plan capacity per
instance = declared capacity x utilisation (see scenario_config).

The dashboard REPLAYS this recorded output; it does not run the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from comparison import replay_compare as rc
from comparison import scenario_config as cfg
from comparison.scenario import build_catalog_scenario, build_thumbnail_scenario, find_episodes
from decision_engine.grading import LAMBDA_TRUTH_THRESHOLD
from forecasting.predictor import GbmForecaster

CATALOG = rc.CATALOG
THUMBNAIL = rc.THUMBNAIL
HORIZON = rc.FORECAST_STEPS  # 15 forecast minutes per origin
RAMP_START_FRACTION = 0.5  # ramped bursts start at <= ~25% of peak; unramped ones start at 80-100% of it
FULL_ONSET_FRACTION = 0.6  # "the burst has really arrived": first minute at or above this share of the peak
EC2_LEAD_IN, EC2_TAIL = 15, 10
BURST_LEAD_IN, BURST_TAIL = 20, 25
ACCURACY_HORIZON = {CATALOG: 5, THUMBNAIL: 3}  # minutes ahead the accuracy strip is measured at
SCHEMA_VERSION = 1


# ------------------------------------------------------------------------------------------ episode rules


def ramp_visible(values: np.ndarray, onset: int, end: int) -> bool:
    """Did this burst announce itself? True when its first loud minute is still well below its peak (traffic was climbing)."""
    peak = float(values[onset : end + 1].max())
    return bool(peak > 0 and float(values[onset]) < RAMP_START_FRACTION * peak)


def full_onset(values: np.ndarray, onset: int, end: int) -> int:
    """First minute at which the burst reaches FULL_ONSET_FRACTION of its peak (where the real surge lands)."""
    peak = float(values[onset : end + 1].max())
    return next(i for i in range(onset, end + 1) if values[i] >= FULL_ONSET_FRACTION * peak)


def pick_burst_episodes(values: np.ndarray) -> dict[str, tuple[int, int] | None]:
    """First burst with a visible ramp and first without, in chronological order (never 'the best ones')."""
    picked: dict[str, tuple[int, int] | None] = {"warned": None, "unwarned": None}
    for a, b in find_episodes(values, LAMBDA_TRUTH_THRESHOLD, rc.MERGE_GAP_MINUTES):
        key = "warned" if ramp_visible(values, a, b) else "unwarned"
        if picked[key] is None:
            picked[key] = (a, b)
    return picked


def clamp_window(first: int, last: int, n: int) -> tuple[int, int]:
    """Inclusive window [first, last] clipped to [0, n-1]."""
    return max(0, first), min(n - 1, last)


def _ints(a) -> list[int]:
    return [int(v) for v in np.asarray(a)]


def _floats(a, digits: int = 1) -> list[float]:
    return [round(float(v), digits) for v in np.asarray(a)]


def _fan(forecasts: list, scale: float) -> dict[str, list[list[float]]]:
    """Per origin minute, the 15-step p10/p50/p90 forecast in exported units."""
    return {q: [_floats(np.asarray(getattr(f, q)) * scale) for f in forecasts] for q in ("p10", "p50", "p90")}


# ------------------------------------------------------------------------------------------------ episodes


def ec2_episode(
    *, times: pd.DatetimeIndex, demand: np.ndarray, fans: dict, timelines: dict, week_metrics: dict, window: tuple[int, int],
    load_scale: float, timing: rc.Timing,
) -> dict:
    """`demand` natural units over the whole held-out week; `timelines` per arm over the whole week; `fans` for the window's origins."""
    a, b = window
    cap_full = cfg.CATALOG_DECLARED_CAPACITY_RPM / load_scale
    cap_plan = cap_full * cfg.UTILIZATION
    arms = {}
    context = {}
    for name, tl in timelines.items():
        cap = tl["in_service"][a : b + 1]
        d = demand[a : b + 1]
        over = d > cap * cap_plan
        sat = d > cap * cap_full
        arms[name] = {"capacity": _ints(cap), "booting": _ints(tl["booting"][a : b + 1]), "asked": _ints(tl["asked"][a : b + 1]),
                      "over_plan": _ints(over), "saturated": _ints(sat)}
        context[name] = {"minutes_over_plan_here": int(over.sum()), "minutes_over_plan_week_pct": round(week_metrics[name]["over_target_minutes_pct"], 2),
                         "instance_hours_week": round(week_metrics[name]["instance_hours"], 1)}
    return {
        "id": "catalog-morning-rise", "service": CATALOG, "kind": "ec2",
        "title": "Morning rise", "start": times[a].isoformat(), "minutes": int(b - a + 1),
        "traffic": _floats(demand[a : b + 1] * load_scale, 0), "need": _floats(demand[a : b + 1] / cap_plan, 2),
        "forecast": fans, "arms": arms, "context": context,
        "units": {"traffic": "requests/min", "need": "instances needed at plan", "plan_rpm_per_instance": round(cap_plan * load_scale, 1),
                  "full_rpm_per_instance": round(cap_full * load_scale, 1)},
    }


def lambda_episode(
    *, episode_id: str, title: str, times: pd.DatetimeIndex, invocations: np.ndarray, need_full: np.ndarray, fans: dict, timelines: dict,
    burst: tuple[int, int], window: tuple[int, int], load_scale: float, all_bursts: dict, ramped: bool,
) -> dict:
    a, b = window
    onset, end = burst
    cold_window = (max(0, onset - rc.BURST_LEAD_MINUTES), min(len(invocations) - 1, end + rc.BURST_TAIL_MINUTES))
    arms, context = {}, {}
    for name, tl in timelines.items():
        prov = tl["in_service"]
        cold = rc.cold_starts_per_minute(need_full, prov)
        warm = rc.warm_containers_per_minute(need_full, prov)
        arms[name] = {"capacity": _ints(prov[a : b + 1]), "booting": _ints(tl["booting"][a : b + 1]), "asked": _ints(tl["asked"][a : b + 1]),
                      "cold": _ints(cold[a : b + 1]), "warm": _ints(warm[a : b + 1])}
        context[name] = {"cold_starts_in_this_burst": int(cold[cold_window[0] : cold_window[1] + 1].sum())}
    return {
        "id": episode_id, "service": THUMBNAIL, "kind": "lambda", "title": title,
        "start": times[a].isoformat(), "minutes": int(b - a + 1),
        "burst": {"onset": int(onset - a), "full_onset": int(full_onset(invocations, onset, end) - a), "end": int(end - a), "ramp_visible": bool(ramped), "peak": round(float(invocations[onset : end + 1].max() * load_scale), 0)},
        "traffic": _floats(invocations[a : b + 1] * load_scale, 0), "need": _ints(need_full[a : b + 1]),
        "forecast": fans, "arms": arms, "context": context, "all_bursts": all_bursts,
        "units": {"traffic": "invocations/min", "need": "containers needed"},
    }


def all_burst_stats(invocations: np.ndarray, need_full: np.ndarray, timelines: dict) -> dict:
    """Cold starts per burst across the whole held-out week, per approach, plus how often predictive beat reactive."""
    windows = rc.burst_windows(invocations)
    per_arm = {}
    for name, tl in timelines.items():
        cold = rc.cold_starts_per_minute(need_full, tl["in_service"])
        per_arm[name] = [int(cold[a : b + 1].sum()) for a, b in windows]
    out: dict = {"bursts": len(windows), "median_cold_starts": {k: float(np.median(v)) if v else 0.0 for k, v in per_arm.items()},
                 "mean_cold_starts": {k: round(float(np.mean(v)), 2) if v else 0.0 for k, v in per_arm.items()}}
    p, r = per_arm.get("predictive", []), per_arm.get("reactive_pc_tracking", [])
    if p and r:
        out["predictive_vs_reactive"] = {"better": int(sum(x < y for x, y in zip(p, r))), "same": int(sum(x == y for x, y in zip(p, r))),
                                         "worse": int(sum(x > y for x, y in zip(p, r)))}
    return out


# ------------------------------------------------------------------------------------------------ accuracy


def accuracy_block(service: str, series: pd.Series, forecasts: list, *, horizon: int, burst_threshold: float | None = None) -> dict:
    """How good the forecast is `horizon` minutes ahead, over the held-out week (natural units)."""
    pos = {pd.Timestamp(f.origin): i for i, f in enumerate(forecasts)}
    values = series.to_numpy()
    rows = []
    for f in forecasts:
        p = int(series.index.get_loc(pd.Timestamp(f.origin)))
        if p + horizon < len(values):
            rows.append((p, values[p + horizon], f.p10[horizon - 1], f.p50[horizon - 1], f.p90[horizon - 1]))
    arr = np.array(rows, dtype=float)
    actual, lo, mid, hi = arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4]
    out: dict = {"service": service, "horizon_minutes": horizon, "points": int(len(arr)),
                 "mae": round(float(np.abs(mid - actual).mean()), 2), "interval_coverage_pct": round(float(((actual >= lo) & (actual <= hi)).mean() * 100), 1)}
    if service == CATALOG:
        week = 7 * 24 * 60
        naive_idx = arr[:, 0].astype(int) + horizon - week
        ok = naive_idx >= 0
        naive = values[naive_idx[ok]]
        out["mae_same_time_last_week"] = round(float(np.abs(naive - actual[ok]).mean()), 2)
        big = actual >= 5.0
        out["mape_pct"] = round(float((np.abs(mid[big] - actual[big]) / actual[big]).mean() * 100), 1)
    if burst_threshold is not None:
        by_origin = {int(series.index.get_loc(pd.Timestamp(f.origin))): f for f in forecasts}
        warned = {"ramp_visible": [0, 0], "no_ramp": [0, 0]}  # [flagged, total]
        for a, b in find_episodes(values, LAMBDA_TRUTH_THRESHOLD, rc.MERGE_GAP_MINUTES):
            if a < min(by_origin):
                continue
            key = "ramp_visible" if ramp_visible(values, a, b) else "no_ramp"
            flagged = any(o in by_origin and max(by_origin[o].p90[:3]) >= burst_threshold for o in range(a - 3, a))
            warned[key][0] += int(flagged)
            warned[key][1] += 1
        out["burst_warning"] = {k: {"flagged": v[0], "bursts": v[1], "pct": round(100.0 * v[0] / v[1], 1) if v[1] else None} for k, v in warned.items()}
    return out


# --------------------------------------------------------------------------------------- comparison + facts


def comparison_block(path: Path) -> dict:
    """Median and range across seeds from the saved replay_compare run ({ramp fraction: [per-seed results]})."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict = {}
    for fraction, results in data.items():
        agg = rc.aggregate(results)
        out[fraction] = {"seeds": len(results), "catalog_service": agg[CATALOG], "thumbnail_service": agg[THUMBNAIL]}
    head = out.get("0.7")
    if head:
        over = head["catalog_service"]
        out["hero"] = {
            "predictive_inside_plan_pct": round(100.0 - over["predictive"]["over_target_minutes_pct"]["median"], 1),
            "reactive_standard_inside_plan_pct": round(100.0 - over["reactive_standard"]["over_target_minutes_pct"]["median"], 1),
        }
    return out


def aws_facts() -> dict:
    """Measured on real AWS (calibration, 2026-10-15). Numbers come from scenario_config, the rest is the recorded evidence."""
    return {
        "instance_healthy_seconds": cfg.INSTANCE_BOOT_SECONDS, "provisioned_ready_seconds": cfg.PROVISIONED_READY_SECONDS,
        "cold_start_penalty_ms": cfg.COLD_START_PENALTY_MS, "lambda_billed_ms": cfg.BILLED_MS_PER_REQUEST,
        "idle_container_expiry_minutes": rc.IDLE_EXPIRY_MINUTES,
        "single_instance_rpm_served": 25000, "single_instance_p95_ms": 283, "single_instance_cpu_pct": 27,
        "cold_starts_matched_aws_logs": {"marker": 25, "aws_init_lines": 25},
        "region": "us-east-1", "measured_on": "2026-10-15",
        "also_real": ["deployed with Terraform", "decision loop changed real instance and provisioned-concurrency capacity (2026-10-10)"],
    }


# ----------------------------------------------------------------------------------------------- the build


def holdout_slice(series: pd.Series, train_end: pd.Timestamp) -> tuple[int, int]:
    start = int(series.index.get_loc(series.index[series.index > train_end][0]))
    return start, len(series) - rc.FORECAST_STEPS - 1


def build_dashboard_data(
    *, ramp_fraction: float = 0.7, seed: int = 42, days: int = 30, holdout_days: int = 7, comparison_path: Path | None = None,
    timing: rc.Timing = rc.Timing(), log=print,
) -> dict:
    series = rc.synthetic_series(ramp_fraction, seed, days)
    catalog_scale = build_catalog_scenario(series[CATALOG], cfg.catalog_params()).load_scale
    lambda_scale = build_thumbnail_scenario(series[THUMBNAIL], cfg.thumbnail_params()).load_scale
    natural_duration = cfg.BILLED_MS_PER_REQUEST / 1000.0 * lambda_scale
    cap_target_natural = cfg.CATALOG_DECLARED_CAPACITY_RPM * cfg.UTILIZATION / catalog_scale

    episodes = []
    accuracy = {}

    # ---- catalog (EC2)
    log("catalog: training and replaying ...")
    s = series[CATALOG]
    run = rc.predictive_run(CATALOG, s, rc._policy_route_ec2(cap_target_natural), holdout_days)
    start, stop = holdout_slice(s, run.train_end)
    demand = s.to_numpy()[start:stop]
    times = s.index[start:stop]
    tl: dict = {}
    metrics = rc.run_ec2_arms(demand, [p - start for p in run.positions if start <= p < stop], [d for p, d in zip(run.positions, run.desired) if start <= p < stop],
                              load_scale=catalog_scale, timing=timing, timelines=tl)
    scen = build_catalog_scenario(s, cfg.catalog_params())
    first = int(s.index.get_loc(scen.series.index[0])) - start
    win = clamp_window(first - EC2_LEAD_IN, first + len(scen.series) + EC2_TAIL - 1, len(demand))
    origins = s.index[start + win[0] : start + win[1] + 1]
    episodes.append(ec2_episode(times=times, demand=demand, fans=_fan(run.forecaster.forecast_many(s, origins), catalog_scale), timelines=tl,
                                week_metrics=metrics, window=win, load_scale=catalog_scale, timing=timing))
    accuracy[CATALOG] = accuracy_block(CATALOG, s, run.forecasts, horizon=ACCURACY_HORIZON[CATALOG])

    # ---- thumbnail (Lambda)
    log("thumbnail: training and replaying ...")
    s = series[THUMBNAIL]
    run = rc.predictive_run(THUMBNAIL, s, rc._policy_route_lambda(natural_duration), holdout_days)
    start, stop = holdout_slice(s, run.train_end)
    inv = s.to_numpy()[start:stop]
    times = s.index[start:stop]
    need_full = rc.containers_needed(inv, natural_duration)
    tl = {}
    rc.run_lambda_arms(inv, [p - start for p in run.positions if start <= p < stop], [d for p, d in zip(run.positions, run.desired) if start <= p < stop],
                       load_scale=lambda_scale, duration_seconds=natural_duration, timing=timing, timelines=tl)
    stats = all_burst_stats(inv, need_full, tl)
    picks = pick_burst_episodes(inv)
    for key, eid, title in (("warned", "burst-with-warning", "Burst with warning"), ("unwarned", "burst-no-warning", "Burst, no warning")):
        if picks[key] is None:
            continue
        a, b = picks[key]
        win = clamp_window(a - BURST_LEAD_IN, b + BURST_TAIL, len(inv))
        origins = s.index[start + win[0] : start + win[1] + 1]
        episodes.append(lambda_episode(episode_id=eid, title=title, times=times, invocations=inv, need_full=need_full,
                                       fans=_fan(run.forecaster.forecast_many(s, origins), lambda_scale), timelines=tl, burst=(a, b), window=win,
                                       load_scale=lambda_scale, all_bursts=stats, ramped=key == "warned"))
    accuracy[THUMBNAIL] = accuracy_block(THUMBNAIL, s, run.forecasts, horizon=ACCURACY_HORIZON[THUMBNAIL], burst_threshold=35.0)

    data = {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seed": seed, "ramp_fraction": ramp_fraction,
            "synthetic_traffic": True, "replay_not_live_aws": True, "prices_verified": False,
            "load_scale": {CATALOG: round(catalog_scale, 3), THUMBNAIL: round(lambda_scale, 3)},
            "billed_seconds": cfg.BILLED_MS_PER_REQUEST / 1000.0,
            "delays_minutes": {"instance": timing.ec2_delay_minutes, "provisioned_concurrency": timing.pc_delay_minutes, "metric_lag": timing.metric_lag_minutes},
            "plan_utilization": cfg.UTILIZATION, "declared_rpm_per_instance": cfg.CATALOG_DECLARED_CAPACITY_RPM,
            "forecast_horizon_minutes": HORIZON,
            "episode_rules": {
                "catalog-morning-rise": "steepest 2 hours of the first full weekday of the held-out week",
                "burst-with-warning": "first burst of the held-out week whose traffic climbed before onset",
                "burst-no-warning": "first burst of the held-out week with no climb before onset",
            },
        },
        "episodes": episodes, "accuracy": accuracy, "aws": aws_facts(),
    }
    if comparison_path is not None and Path(comparison_path).exists():
        data["comparison"] = comparison_block(Path(comparison_path))
    return data


# ---------------------------------------------------------------------------------------------- validation


def validate_dashboard_data(data: dict) -> list[str]:
    """Problems that would make the dashboard lie or crash. Empty list = fine."""
    problems: list[str] = []
    if data.get("schema_version") != SCHEMA_VERSION:
        problems.append("unexpected schema_version")
    meta = data.get("meta", {})
    if not (meta.get("synthetic_traffic") and meta.get("replay_not_live_aws")):
        problems.append("meta must declare synthetic traffic and replay-not-live")
    for ep in data.get("episodes", []):
        n = ep["minutes"]
        tag = ep["id"]
        for key in ("traffic", "need"):
            if len(ep[key]) != n:
                problems.append(f"{tag}: {key} has {len(ep[key])} values, expected {n}")
        for q in ("p10", "p50", "p90"):
            fan = ep["forecast"][q]
            if len(fan) != n or any(len(r) != HORIZON for r in fan):
                problems.append(f"{tag}: forecast {q} must be {n} x {HORIZON}")
        if all(len(ep["forecast"][q]) == n for q in ("p10", "p50", "p90")):
            for i in range(n):
                for h in range(HORIZON):
                    if not (ep["forecast"]["p10"][i][h] <= ep["forecast"]["p50"][i][h] <= ep["forecast"]["p90"][i][h]):
                        problems.append(f"{tag}: forecast quantiles out of order at minute {i}, step {h}")
                        break
        for arm, a in ep["arms"].items():
            for key, vals in a.items():
                if len(vals) != n:
                    problems.append(f"{tag}: {arm}.{key} has {len(vals)} values, expected {n}")
                elif key != "over_plan" and key != "saturated" and min(vals) < 0:
                    problems.append(f"{tag}: {arm}.{key} has a negative value")
            if max(a["capacity"]) + max(a["booting"]) > 10 + 10:
                problems.append(f"{tag}: {arm} capacity above the hard caps")
            if max(a["capacity"]) > 10:
                problems.append(f"{tag}: {arm} capacity above the hard cap of 10")
    if "accuracy" not in data or "aws" not in data:
        problems.append("missing accuracy or aws block")
    return problems


def write_dashboard_data(data: dict, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, separators=(",", ":"))
    out.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))
