"""Does the ramp-up lead signal let a model predict burst *onset*?

The overall high-load scores in forecasting.gbm mix two things: minutes inside a burst already in
progress (trivially predictable by persistence) and the first minute of a burst (the only moment
that actually requires foresight). This scores onset minutes alone, split into bursts that had a
ramp and bursts that did not, at several lead times. It also reports a false-alarm rate, since
"flagged a burst" is meaningless without knowing how often a model flags when nothing happens.

The DB doesn't record which bursts ramped, so the schedule is rebuilt from the generator's seed
and checked against the stored series (reconstruct_bursts / verify_against_series).

Run: SYNTHETIC_SEED=42 python -m forecasting.burst_diagnostic
(seed and ramp settings must match what generated the DB data)
"""

from __future__ import annotations

import inspect
import math
import os
import random
from dataclasses import dataclass

import numpy as np
import pandas as pd

from forecasting.data_pipeline import synthetic_data as sd
from forecasting.data_pipeline.db import get_connection
from forecasting.gbm import build_dataset, fit_quantile_models, predict_quantiles
from forecasting.train import load_series, split_train_holdout

LEAD_MINUTES = (1, 3, 5, 10, 15)
BURST_LEVEL = 50.0  # forecast p90 at/above this counts as "flagging a burst"
QUIET_LEVEL = 8.0  # actual below this = nothing happening (excludes ramp minutes)


@dataclass(frozen=True)
class Burst:
    onset_index: int  # sample index (minutes since series start) of the first in-burst sample
    ramped: bool


def _generator_defaults() -> dict[str, float]:
    return {
        name: p.default
        for name, p in inspect.signature(sd.generate_metrics).parameters.items()
        if p.default is not inspect.Parameter.empty
    }


def reconstruct_bursts(
    seed: int, days: int, *, ramp_fraction: float, ramp_minutes: float, interval_seconds: int = 60
) -> list[Burst]:
    """Rebuild the burst schedule exactly as generate_metrics draws it (same rng order)."""
    d = _generator_defaults()
    rng = random.Random(seed)
    schedule = sd.build_burst_schedule(
        days * sd.SECONDS_PER_DAY,
        peak_min=d["thumbnail_peak_min"],
        peak_max=d["thumbnail_peak_max"],
        burst_min_minutes=d["thumbnail_burst_min_minutes"],
        burst_max_minutes=d["thumbnail_burst_max_minutes"],
        gap_min_minutes=d["thumbnail_gap_min_minutes"],
        gap_max_minutes=d["thumbnail_gap_max_minutes"],
        rng=rng,
    )
    ramping = sd.choose_ramping_bursts(
        schedule, ramp_fraction=ramp_fraction, ramp_seconds=ramp_minutes * 60, rng=rng
    )
    return [
        Burst(onset_index=math.ceil(start / interval_seconds), ramped=i in ramping)
        for i, (start, _end, _peak) in enumerate(schedule)
    ]


def verify_against_series(bursts: list[Burst], series: pd.Series, *, min_match: float = 0.95) -> float:
    """Fraction of reconstructed onsets that really are burst-level in the stored series."""
    values = series.to_numpy()
    in_range = [b for b in bursts if b.onset_index < len(values)]
    hits = sum(values[b.onset_index] >= BURST_LEVEL for b in in_range)
    fraction = hits / len(in_range) if in_range else 0.0
    if fraction < min_match:
        raise ValueError(
            f"Rebuilt burst schedule matches only {fraction:.0%} of the stored series; the seed or "
            "ramp settings differ from what generated the DB data."
        )
    return fraction


def _pct(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:.0%} ({numerator}/{denominator})" if denominator else "n/a (0)"


def _train(series: pd.Series, holdout_days: int, *, recent: bool):
    train, _ = split_train_holdout(series, holdout_days)
    train_end = train.index.max()
    X, y, _ = build_dataset(series, recent=recent, origin_stride=3, target_until=train_end)
    return fit_quantile_models(X, y), train_end


def _lead_forecast(series: pd.Series, models, train_end, h: int, *, recent: bool) -> pd.DataFrame:
    """p50/p90/actual for every holdout target minute, forecast `h` minutes ahead."""
    X, y, target_times = build_dataset(series, horizons=(h,), recent=recent, target_after=train_end)
    forecast = predict_quantiles(models, X)
    return pd.DataFrame(
        {"p50": forecast["p50"].to_numpy(), "p90": forecast["p90"].to_numpy(), "actual": y.to_numpy()},
        index=target_times,
    )


def _onset_times(series: pd.Series, bursts: list[Burst]) -> dict[str, set[pd.Timestamp]]:
    valid = [b for b in bursts if b.onset_index < len(series)]
    return {
        "ramped": {series.index[b.onset_index] for b in valid if b.ramped},
        "unramped": {series.index[b.onset_index] for b in valid if not b.ramped},
    }


def diagnose(series: pd.Series, bursts: list[Burst], holdout_days: int, *, recent: bool) -> list[str]:
    models, train_end = _train(series, holdout_days, recent=recent)
    onset_times = _onset_times(series, bursts)
    lines = []
    for h in LEAD_MINUTES:
        f = _lead_forecast(series, models, train_end, h, recent=recent)
        quiet = f["actual"] < QUIET_LEVEL
        false_alarm = _pct(int((f["p90"][quiet] >= BURST_LEVEL).sum()), int(quiet.sum()))
        row = [f"lead {h:>2} min"]
        for kind in ("ramped", "unramped"):
            onsets = f.index.intersection(list(onset_times[kind]))
            row.append(f"{kind}: flagged {_pct(int((f.loc[onsets, 'p90'] >= BURST_LEVEL).sum()), len(onsets))}")
        row.append(f"false alarms on quiet minutes: {false_alarm}")
        lines.append("  " + " | ".join(row))
    return lines


def size_stats(p50: pd.Series, p90: pd.Series, actual: pd.Series) -> dict[str, float]:
    """How well forecasts at burst onset track the burst's real size."""
    return {
        "n": float(len(actual)),
        "corr_p50_actual": float(np.corrcoef(p50, actual)[0, 1]) if len(actual) > 1 else float("nan"),
        "median_p50_over_actual": float((p50 / actual).median()),
        "median_p90_over_actual": float((p90 / actual).median()),
        "p90_covers_actual": float((actual <= p90).mean()),
        "p90_within_2x": float(((p90 >= actual / 2) & (p90 <= actual * 2)).mean()),
    }


def onset_size_report(series: pd.Series, bursts: list[Burst], holdout_days: int) -> list[str]:
    """Does the ramp tell the model how *big* the burst will be (not just that one is coming)?"""
    models, train_end = _train(series, holdout_days, recent=True)
    onset_times = _onset_times(series, bursts)
    lines = [
        f"  {'':<22}{'n':>4}{'corr(p50,actual)':>18}{'p50/actual':>12}{'p90/actual':>12}"
        f"{'p90 >= actual':>15}{'p90 within 2x':>15}"
    ]
    for h in (1, 3):
        f = _lead_forecast(series, models, train_end, h, recent=True)
        for kind in ("ramped", "unramped"):
            onsets = f.index.intersection(list(onset_times[kind]))
            s = size_stats(f.loc[onsets, "p50"], f.loc[onsets, "p90"], f.loc[onsets, "actual"])
            lines.append(
                f"  {f'lead {h} min, {kind}':<22}{int(s['n']):>4}{s['corr_p50_actual']:>18.2f}"
                f"{s['median_p50_over_actual']:>12.2f}{s['median_p90_over_actual']:>12.2f}"
                f"{s['p90_covers_actual']:>15.0%}{s['p90_within_2x']:>15.0%}"
            )
    return lines


if __name__ == "__main__":
    seed = int(os.environ["SYNTHETIC_SEED"])
    days = int(os.environ.get("SYNTHETIC_DAYS", "30"))
    ramp_fraction = float(os.environ.get("SYNTHETIC_RAMP_FRACTION", "0.7"))
    ramp_minutes = float(os.environ.get("SYNTHETIC_RAMP_MINUTES", "4"))
    holdout_days = int(os.environ.get("HOLDOUT_DAYS", "7"))

    series = load_series(get_connection(), "thumbnail_service", "Invocations")
    bursts = reconstruct_bursts(seed, days, ramp_fraction=ramp_fraction, ramp_minutes=ramp_minutes)
    match = verify_against_series(bursts, series)
    holdout_start = series.index.max() - pd.Timedelta(days=holdout_days)
    held = [b for b in bursts if b.onset_index < len(series) and series.index[b.onset_index] > holdout_start]
    print(
        f"schedule check: {match:.0%} of {len(bursts)} rebuilt onsets are burst-level in the DB series; "
        f"holdout has {sum(b.ramped for b in held)} ramped / {sum(not b.ramped for b in held)} unramped bursts"
    )
    for label, recent in (("gbm+history", True), ("gbm calendar-only", False)):
        print(f"\n{label}")
        print("\n".join(diagnose(series, bursts, holdout_days, recent=recent)))
    print("\nburst size at onset (gbm+history; actual = real first-minute burst value)")
    print("\n".join(onset_size_report(series, bursts, holdout_days)))
