"""Cheap replay comparison: predictive vs reactive vs static capacity over the held-out week (NO AWS, NO database).

This is the lightweight answer to "does predictive scaling beat reactive scaling?". It replays the same held-out
week of (synthetic) traffic through every policy and counts what each would have cost and missed. It is a REPLAY,
not a live AWS experiment, and must be labelled that way wherever its numbers are shown.

What is real in it: the delays between "capacity requested" and "capacity usable" (instance 92.5 s, provisioned
concurrency 178 s) and the idle-container lifetime come from the AWS calibration. What is modelled: demand (the
synthetic generator), the reactive rule (AWS target tracking, approximated), and how cold starts arise (below).

ARMS
  EC2 (catalog):    predictive (the real decision engine + damping) / reactive-standard (target = 70% of capacity) /
                    reactive-aggressive (50%) / static-peak (oracle: sized for the week's highest minute).
  Lambda (thumbnail): predictive / reactive-on-demand-only (= do nothing, the AWS default) / reactive-pc-tracking
                    (provisioned concurrency scaled from observed utilisation) / static-peak (oracle: provisioned concurrency held at the week's highest need, capped at 10).

FAIRNESS (same rules as the real-run plan, decisions D3/D8)
  * every arm gets the same demand, the same delays and the same one-minute metric publication lag;
  * predictive sizes on the same 70% of the declared capacity that the standard reactive arm targets;
  * the predictive forecaster is trained on the weeks before the held-out week of the same generator (an
    advantage a new deployment would not have) - disclose it.

MODEL SIMPLIFICATIONS (all documented because they matter)
  * 1-minute grid: bursts last 2-12 minutes, so a minute is coarse; demand inside a minute is its average.
  * Capacity requested at the start of minute t becomes usable at t + ceil(delay); releases are immediate.
  * Reactive EC2 approximates AWS target tracking: scale out after 3 consecutive minutes above target to
    ceil(demand / target); scale in after 15 consecutive minutes where one instance fewer would still be under
    target. AWS's real alarms, warm-up accounting and cooldowns are not reproduced.
  * Cold starts: a minute needs ceil(concurrency) containers (concurrency = invocations/60 x duration). Containers
    beyond provisioned capacity are on-demand; those used in the last 15 minutes are still warm; every on-demand
    container that is not warm is one cold start. Bursts shorter than a minute inside a minute are not seen.
  * EC2 latency is NOT modelled (the real instance never saturated on this laptop). "Over target" and "saturated"
    minutes are the proxies: demand above 70% / above 100% of the declared per-instance capacity.
  * Cost = capacity-hours x list prices (comparison/cost.py, prices unverified). The ALB is the same for every arm
    and left out; the T3 credit surcharge is left out because CPU at these rates is far under the 20% baseline.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from comparison.cost import ec2_cost, lambda_cost
from comparison.scenario import build_catalog_scenario, build_thumbnail_scenario, find_episodes
from comparison.scenario_config import (
    BILLED_MS_PER_REQUEST,
    CATALOG_DECLARED_CAPACITY_RPM,
    INSTANCE_BOOT_SECONDS,
    PROVISIONED_READY_SECONDS,
    UTILIZATION,
    catalog_params,
    lambda_shape,
    thumbnail_params,
)
from decision_engine.damping import DEFAULT_DAMPING
from decision_engine.dry_run import CADENCE_MINUTES, FORECAST_STEPS, replay
from decision_engine.engine import ROUTES, Route
from decision_engine.grading import LAMBDA_TRUTH_THRESHOLD
from decision_engine.models import Aggressiveness
from forecasting.data_pipeline.synthetic_data import generate_metrics
from forecasting.predictor import GbmForecaster
from forecasting.train import TARGET_SERIES, split_train_holdout

CATALOG = "catalog_service"
THUMBNAIL = "thumbnail_service"
IDLE_EXPIRY_MINUTES = 15  # the first idle gap at which no container survived (calibration)
BURST_LEAD_MINUTES = 5  # cold starts this long before a burst's first loud minute belong to it (the ramp is 4 min)
BURST_TAIL_MINUTES = 5
MERGE_GAP_MINUTES = 3
SCALE_OUT_AFTER_MINUTES = 3  # AWS target tracking: 3 consecutive breaching datapoints
SCALE_IN_AFTER_MINUTES = 15  # ... and 15 consecutive datapoints for scale-in
LAMBDA_ABSOLUTE_MAX = 10
PC_TRACKING_TARGET = 0.7  # AWS's usual provisioned-concurrency utilisation target
EC2_ABSOLUTE_MAX = 10


@dataclass(frozen=True)
class Timing:
    ec2_delay_minutes: int = math.ceil(INSTANCE_BOOT_SECONDS / 60.0)  # 2
    pc_delay_minutes: int = math.ceil(PROVISIONED_READY_SECONDS / 60.0)  # 3
    metric_lag_minutes: int = 1  # CloudWatch publishes a minute's data about a minute after it ends

    def __post_init__(self) -> None:
        if min(self.ec2_delay_minutes, self.pc_delay_minutes) < 1 or self.metric_lag_minutes < 0:
            raise ValueError("delays must be at least one minute and the metric lag non-negative")


# ----------------------------------------------------------------------------------------- capacity engine


TargetFn = Callable[[int, int, int], int]  # (minute, in_service, pending) -> requested total capacity


def simulate_capacity_detail(n: int, delay: int, target_at: TargetFn, *, initial: int, low: int, high: int) -> dict[str, np.ndarray]:
    """Capacity usable in each of `n` minutes, plus what is still booting and what was asked for each minute.

    Each minute the policy states the TOTAL capacity it wants. More than we have in service plus on the way:
    the difference is requested and arrives `delay` minutes later. Less: the surplus is dropped at once, newest
    pending requests first. Requests are clamped to [low, high].

    Returns {"in_service", "booting", "asked"}: units usable in minute t, units requested earlier and not yet usable
    in minute t, and units newly requested at the start of minute t.
    """
    if delay < 1:
        raise ValueError("delay must be at least one minute")
    in_service = np.zeros(n, dtype=int)
    booting = np.zeros(n, dtype=int)
    asked = np.zeros(n, dtype=int)
    current = int(initial)
    pending: list[list[int]] = []  # [arrival minute, count]
    for t in range(n):
        current += sum(count for arrival, count in pending if arrival <= t)
        pending = [p for p in pending if p[0] > t]
        waiting = sum(count for _, count in pending)
        want = max(low, min(high, int(target_at(t, current, waiting))))
        have = current + waiting
        if want > have:
            pending.append([t + delay, want - have])
            asked[t] = want - have
        elif want < have:
            drop = have - want
            while drop and pending:
                take = min(pending[-1][1], drop)
                pending[-1][1] -= take
                drop -= take
                if pending[-1][1] == 0:
                    pending.pop()
            current -= drop
        in_service[t] = current
        booting[t] = sum(count for _, count in pending)
    return {"in_service": in_service, "booting": booting, "asked": asked}


def simulate_capacity(n: int, delay: int, target_at: TargetFn, *, initial: int, low: int, high: int) -> np.ndarray:
    """Capacity usable in each of `n` minutes (see `simulate_capacity_detail`)."""
    return simulate_capacity_detail(n, delay, target_at, initial=initial, low=low, high=high)["in_service"]


def step_targets(n: int, decision_positions: list[int], desired: list[int], *, lag: int, floor: int) -> np.ndarray:
    """A decision made from data up to minute p is acted on at the start of minute p + 1 + lag and held until the next one."""
    target = np.full(n, floor, dtype=int)
    order = sorted(zip(decision_positions, desired))
    for k, (position, value) in enumerate(order):
        start = position + 1 + lag
        end = order[k + 1][0] + 1 + lag if k + 1 < len(order) else n
        if start < n:
            target[start : min(end, n)] = value
    return target


def reactive_ec2_target(demand: np.ndarray, target_per_instance: float, *, lag: int, low: int) -> TargetFn:
    """Approximate AWS target tracking on requests per instance (see the module docstring for what is simplified)."""
    if target_per_instance <= 0:
        raise ValueError("target_per_instance must be positive")
    counters = {"above": 0, "below": 0}

    def target_at(t: int, in_service: int, pending: int) -> int:
        observed = t - 1 - lag  # the latest minute whose data is visible
        want = in_service + pending
        if observed < 0:
            return want
        d = float(demand[observed])
        counters["above"] = counters["above"] + 1 if d / max(in_service, 1) > target_per_instance else 0
        removable = in_service > 1 and d / (in_service - 1) < target_per_instance
        counters["below"] = counters["below"] + 1 if removable else 0
        needed = math.ceil(d / target_per_instance)
        if counters["above"] >= SCALE_OUT_AFTER_MINUTES:
            counters["above"] = 0
            return max(want, needed)
        if counters["below"] >= SCALE_IN_AFTER_MINUTES:
            counters["below"] = 0
            return min(want, max(low, needed))
        return want

    return target_at


# --------------------------------------------------------------------------------------------- EC2 metrics


def ec2_metrics(demand: np.ndarray, in_service: np.ndarray, *, cap_target: float, cap_full: float) -> dict:
    """`cap_target` = requests/min one instance should carry (70% plan); `cap_full` = what it can carry (declared)."""
    carried_at_target = in_service * cap_target
    carried_full = in_service * cap_full
    total = float(demand.sum())
    short = np.maximum(0.0, demand - carried_at_target)
    changes = int((np.diff(in_service) != 0).sum())
    hours = float(in_service.sum()) / 60.0
    return {
        "minutes": int(len(demand)),
        "over_target_minutes_pct": float((demand > carried_at_target).mean() * 100.0),
        "saturated_minutes_pct": float((demand > carried_full).mean() * 100.0),
        "demand_above_target_pct": float(short.sum() / total * 100.0) if total > 0 else 0.0,
        "mean_instances": float(in_service.mean()),
        "max_instances": int(in_service.max()),
        "changes": changes,
        "instance_hours": hours,
        "cost_usd": ec2_cost(hours, cpu_utilization=0.0)["ec2_instances"],
    }


# ------------------------------------------------------------------------------------------ Lambda metrics


def containers_needed(invocations: np.ndarray, duration_seconds: float) -> np.ndarray:
    """Containers busy at once, rounded up: invocations per minute / 60 x seconds per invocation (0 when idle)."""
    concurrency = np.maximum(invocations, 0.0) / 60.0 * duration_seconds
    return np.where(concurrency > 1e-9, np.ceil(concurrency - 1e-9), 0).astype(int)


def cold_starts_per_minute(need: np.ndarray, provisioned: np.ndarray, idle_expiry: int = IDLE_EXPIRY_MINUTES) -> np.ndarray:
    """Cold starts each minute: on-demand containers wanted beyond those still warm from the last `idle_expiry` minutes."""
    on_demand = np.maximum(0, need - provisioned)
    cold = np.zeros(len(need), dtype=int)
    for t in range(len(need)):
        warm = int(on_demand[max(0, t - idle_expiry) : t].max()) if t > 0 else int(on_demand[0])
        cold[t] = max(0, int(on_demand[t]) - warm)
    return cold


def warm_containers_per_minute(need: np.ndarray, provisioned: np.ndarray, idle_expiry: int = IDLE_EXPIRY_MINUTES) -> np.ndarray:
    """On-demand containers alive each minute: those used now plus those used within the last `idle_expiry` minutes."""
    on_demand = np.maximum(0, need - provisioned)
    warm = np.zeros(len(need), dtype=int)
    for t in range(len(need)):
        previous = int(on_demand[max(0, t - idle_expiry) : t].max()) if t > 0 else 0
        warm[t] = max(int(on_demand[t]), previous)
    return warm


def burst_windows(values: np.ndarray, threshold: float = LAMBDA_TRUTH_THRESHOLD) -> list[tuple[int, int]]:
    """(first minute counted, last minute counted) for every burst: lead and tail included."""
    n = len(values)
    return [
        (max(0, a - BURST_LEAD_MINUTES), min(n - 1, b + BURST_TAIL_MINUTES))
        for a, b in find_episodes(values, threshold, MERGE_GAP_MINUTES)
    ]


def lambda_metrics(
    invocations: np.ndarray, need: np.ndarray, provisioned: np.ndarray, windows: list[tuple[int, int]], *, load_scale: float
) -> dict:
    cold = cold_starts_per_minute(need, provisioned)
    per_burst = [int(cold[a : b + 1].sum()) for a, b in windows]
    inside = np.zeros(len(cold), dtype=bool)
    for a, b in windows:
        inside[a : b + 1] = True
    busy = need > 0
    served_by_pc = np.where(busy, np.minimum(need, provisioned) / np.maximum(need, 1), 0.0)
    total_inv = float(invocations.sum())
    pc_fraction = float((invocations * served_by_pc).sum() / total_inv) if total_inv > 0 else 0.0
    unit_hours = float(provisioned.sum()) / 60.0
    cost = lambda_cost(
        requests=int(total_inv * load_scale), provisioned_hours=unit_hours, provisioned_fraction=pc_fraction, shape=lambda_shape()
    )
    return {
        "bursts": len(windows),
        "cold_starts_total": int(cold.sum()),
        "cold_starts_in_bursts": int(sum(per_burst)),
        "cold_starts_outside_bursts": int(cold[~inside].sum()),
        "cold_starts_per_burst_mean": float(np.mean(per_burst)) if per_burst else 0.0,
        "bursts_with_zero_cold_starts_pct": float(np.mean([c == 0 for c in per_burst]) * 100.0) if per_burst else 0.0,
        "served_by_provisioned_pct": pc_fraction * 100.0,
        "provisioned_unit_hours": unit_hours,
        "max_provisioned": int(provisioned.max()),
        "cost_usd": float(sum(cost.values())),
    }


# ------------------------------------------------------------------------------------------- the arms


def _policy_route_ec2(natural_capacity_target: float) -> Route:
    base = ROUTES[CATALOG]
    assert base.ec2 is not None
    return replace(base, ec2=replace(base.ec2, capacity_per_instance_rpm=natural_capacity_target))


def _policy_route_lambda(natural_duration_seconds: float) -> Route:
    base = ROUTES[THUMBNAIL]
    assert base.lambda_ is not None
    return replace(base, lambda_=replace(base.lambda_, avg_duration_seconds=natural_duration_seconds, max_concurrency=LAMBDA_ABSOLUTE_MAX))


def run_ec2_arms(
    demand: np.ndarray, predictive_positions: list[int], predictive_desired: list[int], *, load_scale: float, timing: Timing,
    timelines: dict | None = None,
) -> dict:
    """Metrics per arm. If `timelines` is given it is filled with each arm's per-minute {in_service, booting, asked}."""
    cap_full = CATALOG_DECLARED_CAPACITY_RPM / load_scale
    cap_target = cap_full * UTILIZATION
    n = len(demand)
    low, high = 1, EC2_ABSOLUTE_MAX
    out = {}

    pred_targets = step_targets(n, predictive_positions, predictive_desired, lag=timing.metric_lag_minutes, floor=low)
    arms: dict[str, tuple[TargetFn, int]] = {
        "predictive": (lambda t, cur, pend: int(pred_targets[t]), low),
        "reactive_standard": (reactive_ec2_target(demand, cap_target, lag=timing.metric_lag_minutes, low=low), low),
        "reactive_aggressive": (reactive_ec2_target(demand, cap_full * 0.5, lag=timing.metric_lag_minutes, low=low), low),
    }
    static_n = max(low, min(high, math.ceil(float(demand.max()) / cap_target)))
    arms["static_peak"] = (lambda t, cur, pend: static_n, static_n)
    for name, (target_fn, initial) in arms.items():
        detail = simulate_capacity_detail(n, timing.ec2_delay_minutes, target_fn, initial=initial, low=low, high=high)
        if timelines is not None:
            timelines[name] = detail
        out[name] = ec2_metrics(demand, detail["in_service"], cap_target=cap_target, cap_full=cap_full)
    return out


def run_lambda_arms(
    invocations: np.ndarray, predictive_positions: list[int], predictive_desired: list[int], *, load_scale: float, duration_seconds: float, timing: Timing,
    timelines: dict | None = None,
) -> dict:
    """Metrics per arm. If `timelines` is given it is filled with each arm's per-minute {in_service, booting, asked}."""
    n = len(invocations)
    need = containers_needed(invocations, duration_seconds)
    windows = burst_windows(invocations)
    pred_targets = step_targets(n, predictive_positions, predictive_desired, lag=timing.metric_lag_minutes, floor=0)
    static_n = min(LAMBDA_ABSOLUTE_MAX, int(need.max()))
    arms: dict[str, tuple[TargetFn, int]] = {
        "predictive": (lambda t, cur, pend: int(pred_targets[t]), 0),
        "reactive_on_demand_only": (lambda t, cur, pend: 0, 0),
        # Approximates AWS Application Auto Scaling on provisioned-concurrency utilisation (70% target): reacts to the
        # containers seen busy, with the same lag, 3-minute breach rule and 15-minute scale-in rule as the EC2 reactive arm.
        "reactive_pc_tracking": (reactive_ec2_target(need.astype(float), PC_TRACKING_TARGET, lag=timing.metric_lag_minutes, low=0), 0),
        "static_peak": (lambda t, cur, pend: static_n, static_n),
    }
    out = {}
    for name, (target_fn, initial) in arms.items():
        detail = simulate_capacity_detail(n, timing.pc_delay_minutes, target_fn, initial=initial, low=0, high=LAMBDA_ABSOLUTE_MAX)
        if timelines is not None:
            timelines[name] = detail
        out[name] = lambda_metrics(invocations, need, detail["in_service"], windows, load_scale=load_scale)
    return out


# --------------------------------------------------------------------------------------------- the run


def synthetic_series(ramp_fraction: float, seed: int, days: int = 30) -> dict[str, pd.Series]:
    """Re-generate the training/holdout series in memory (same generator as the database was filled with)."""
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)  # a Monday
    rows = generate_metrics(start, days, 60, seed=seed, thumbnail_ramp_fraction=ramp_fraction)
    wanted = {(s, m) for s, m in TARGET_SERIES.items()}
    out: dict[str, list] = {s: [] for s in TARGET_SERIES}
    for t, service, metric, value, _source in rows:
        if (service, metric) in wanted:
            out[service].append((t.replace(tzinfo=None), value))
    return {s: pd.Series(dict(v), dtype="float64").sort_index() for s, v in out.items()}


@dataclass(frozen=True)
class PredictiveRun:
    """The trained forecaster, its forecasts at the replay cadence, and the (damped) decisions made from them."""

    forecaster: GbmForecaster
    forecasts: list
    positions: list[int]  # series position of each decision's origin
    desired: list[int]
    train_end: pd.Timestamp


def predictive_run(service: str, series: pd.Series, route: Route, holdout_days: int) -> PredictiveRun:
    train, _ = split_train_holdout(series, holdout_days)
    train_end = train.index.max()
    forecaster = GbmForecaster.fit(service, series, train_end)
    origins = series.index[series.index > train_end]
    origins = origins[origins <= series.index[-FORECAST_STEPS - 2]][:: CADENCE_MINUTES[service]]
    forecasts = forecaster.forecast_many(series, origins)
    frame = replay(series, forecasts, route, Aggressiveness.BALANCED, damping=DEFAULT_DAMPING[service])
    positions = [int(series.index.get_loc(pd.Timestamp(t))) for t in frame["time"]]
    return PredictiveRun(forecaster, forecasts, positions, [int(d) for d in frame["desired"]], train_end)


def _predictive_decisions(service: str, series: pd.Series, route: Route, holdout_days: int) -> tuple[list[int], list[int], int, pd.Timestamp]:
    run = predictive_run(service, series, route, holdout_days)
    return run.positions, run.desired, int(len(split_train_holdout(series, holdout_days)[0])), run.train_end


def run_comparison(ramp_fraction: float = 0.7, seed: int = 42, holdout_days: int = 7, timing: Timing = Timing()) -> dict:
    series = synthetic_series(ramp_fraction, seed)

    catalog_scale = build_catalog_scenario(series[CATALOG], catalog_params()).load_scale
    lambda_scale = build_thumbnail_scenario(series[THUMBNAIL], thumbnail_params()).load_scale
    natural_duration = BILLED_MS_PER_REQUEST / 1000.0 * lambda_scale
    cap_target_natural = CATALOG_DECLARED_CAPACITY_RPM * UTILIZATION / catalog_scale

    result: dict = {
        "ramp_fraction": ramp_fraction, "seed": seed,
        "load_scale": {CATALOG: catalog_scale, THUMBNAIL: lambda_scale},
        "timing": {"ec2_delay_minutes": timing.ec2_delay_minutes, "pc_delay_minutes": timing.pc_delay_minutes, "metric_lag_minutes": timing.metric_lag_minutes},
    }

    for service, runner in ((CATALOG, "ec2"), (THUMBNAIL, "lambda")):
        s = series[service]
        route = _policy_route_ec2(cap_target_natural) if runner == "ec2" else _policy_route_lambda(natural_duration)
        positions, desired, _n_train, train_end = _predictive_decisions(service, s, route, holdout_days)
        start = int(s.index.get_loc(s.index[s.index > train_end][0]))
        stop = len(s) - FORECAST_STEPS - 1  # decisions need a full window of data after them
        values = s.to_numpy()[start:stop]
        rel_positions = [p - start for p in positions if start <= p < stop]
        rel_desired = [d for p, d in zip(positions, desired) if start <= p < stop]
        if runner == "ec2":
            result[service] = run_ec2_arms(values, rel_positions, rel_desired, load_scale=catalog_scale, timing=timing)
        else:
            result[service] = run_lambda_arms(values, rel_positions, rel_desired, load_scale=lambda_scale, duration_seconds=natural_duration, timing=timing)
        result[service]["_minutes"] = int(len(values))
    return result


# ------------------------------------------------------------------------------------------- reporting


def format_comparison(result: dict) -> str:
    lines = [
        f"REPLAY COMPARISON (not a live AWS experiment) - ramp fraction {result['ramp_fraction']:.0%}, seed {result['seed']}",
        f"delays: instance {result['timing']['ec2_delay_minutes']} min, provisioned concurrency {result['timing']['pc_delay_minutes']} min, "
        f"metric lag {result['timing']['metric_lag_minutes']} min; cost = capacity-hours x unverified list prices",
        "",
        f"EC2 / catalog_service ({result[CATALOG]['_minutes']} minutes)",
        f"  {'arm':<20}{'over 70%':>9}{'saturated':>10}{'demand>70%':>11}{'avg inst':>9}{'max':>4}{'changes':>8}{'inst-h':>8}{'cost $':>8}",
    ]
    for arm, m in result[CATALOG].items():
        if arm.startswith("_"):
            continue
        lines.append(
            f"  {arm:<20}{m['over_target_minutes_pct']:>8.1f}%{m['saturated_minutes_pct']:>9.1f}%{m['demand_above_target_pct']:>10.1f}%"
            f"{m['mean_instances']:>9.2f}{m['max_instances']:>4}{m['changes']:>8}{m['instance_hours']:>8.1f}{m['cost_usd']:>8.3f}"
        )
    lines += [
        "",
        f"Lambda / thumbnail_service ({result[THUMBNAIL]['_minutes']} minutes)",
        f"  {'arm':<24}{'bursts':>7}{'cold/burst':>11}{'0-cold %':>9}{'cold total':>11}{'PC share':>9}{'PC unit-h':>10}{'cost $':>8}",
    ]
    for arm, m in result[THUMBNAIL].items():
        if arm.startswith("_"):
            continue
        lines.append(
            f"  {arm:<24}{m['bursts']:>7}{m['cold_starts_per_burst_mean']:>11.2f}{m['bursts_with_zero_cold_starts_pct']:>8.0f}%"
            f"{m['cold_starts_total']:>11}{m['served_by_provisioned_pct']:>8.1f}%{m['provisioned_unit_hours']:>10.1f}{m['cost_usd']:>8.3f}"
        )
    return "\n".join(lines)


EC2_KEYS = ("over_target_minutes_pct", "demand_above_target_pct", "mean_instances", "cost_usd")
LAMBDA_KEYS = ("cold_starts_per_burst_mean", "bursts_with_zero_cold_starts_pct", "provisioned_unit_hours", "cost_usd")


def aggregate(results: list[dict]) -> dict:
    """Across seeds (same ramp fraction): per arm and metric, the median and the lowest/highest value seen."""
    out: dict = {}
    for service, keys in ((CATALOG, EC2_KEYS), (THUMBNAIL, LAMBDA_KEYS)):
        arms = [a for a in results[0][service] if not a.startswith("_")]
        out[service] = {
            arm: {k: {"median": float(np.median([r[service][arm][k] for r in results])),
                      "min": float(min(r[service][arm][k] for r in results)),
                      "max": float(max(r[service][arm][k] for r in results))} for k in keys}
            for arm in arms
        }
    return out


def format_aggregate(agg: dict, ramp_fraction: float, n_seeds: int) -> str:
    def cell(m: dict, fmt: str) -> str:
        return f"{m['median']:{fmt}} [{m['min']:{fmt}}-{m['max']:{fmt}}]"

    lines = [f"ACROSS {n_seeds} SEEDS, ramp fraction {ramp_fraction:.0%}: median [lowest-highest]", "", "EC2 / catalog_service"]
    lines.append(f"  {'arm':<20}{'minutes over 70%':>24}{'avg instances':>26}{'cost $':>26}")
    for arm, m in agg[CATALOG].items():
        lines.append(f"  {arm:<20}{cell(m['over_target_minutes_pct'], '.1f'):>24}{cell(m['mean_instances'], '.2f'):>26}{cell(m['cost_usd'], '.2f'):>26}")
    lines += ["", "Lambda / thumbnail_service", f"  {'arm':<24}{'cold starts / burst':>26}{'bursts with 0 cold %':>26}{'cost $':>26}"]
    for arm, m in agg[THUMBNAIL].items():
        lines.append(f"  {arm:<24}{cell(m['cold_starts_per_burst_mean'], '.2f'):>26}{cell(m['bursts_with_zero_cold_starts_pct'], '.0f'):>26}{cell(m['cost_usd'], '.2f'):>26}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ramp", type=float, nargs="+", default=[0.7, 0.0], help="fractions of bursts with a visible ramp to run (default 0.7 and 0.0)")
    p.add_argument("--seeds", type=int, nargs="+", default=[42], help="generator seeds; with several, a median/range summary is printed")
    p.add_argument("--out", type=Path, default=Path("replay_output") / "comparison.json")
    args = p.parse_args(argv)
    runs: dict[float, list[dict]] = {}
    for fraction in args.ramp:
        for seed in args.seeds:
            r = run_comparison(ramp_fraction=fraction, seed=seed)
            runs.setdefault(fraction, []).append(r)
            if len(args.seeds) == 1:
                print(format_comparison(r), end="\n\n")
    if len(args.seeds) > 1:
        for fraction, results in runs.items():
            print(format_aggregate(aggregate(results), fraction, len(results)), end="\n\n")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({str(k): v for k, v in runs.items()}, indent=2), encoding="utf-8")
    print(f"saved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
