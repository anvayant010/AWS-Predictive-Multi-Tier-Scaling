"""Phase 7 scenario builder: the fixed traffic that every comparison arm is run against.

Pure functions over a 1-minute pandas series. No AWS, no database and no model fitting happen here, so the
scenario can be built, inspected and saved before any money is spent.

Rules (decision D4). They are fixed in advance and apply no judgement about which events are "good":

* THUMBNAIL (Lambda): take the first `n_bursts` bursts of the held-out (last 7 days) week IN CHRONOLOGICAL
  ORDER. Each is kept at real speed: `lead_minutes` before its first loud minute (the ramp) and
  `tail_minutes` after its last (the aftermath). Only the long quiet stretches between bursts are cut, and
  every cut stretch is replaced by exactly `gap_minutes` of real quiet minutes taken from the same week.
* CATALOG (EC2): take the 2 steepest hours of the daily rise on the first full weekday of the held-out week.
* STITCHING: the scenario is placed straight after a real recorded week of history, on one continuous
  1-minute clock, so the forecaster sees realistic seasonal/lag inputs. The forecaster must be trained only
  on data before `train_cutoff`.

The numbers that depend on the real-AWS calibration (burst count/gap length, measured per-instance
capacity) are REQUIRED parameters with no defaults, so an assumed value cannot slip into a real run.

Units. The scenario series stay in the generator's own ("natural") units, which is what the forecaster
was trained on. `load_scale` is applied only when generating real load: offered requests/min =
series * load_scale. For the policy the matching capacity per instance is therefore
`natural_capacity_per_instance_rpm` (measured capacity * utilization / load_scale), so the predictive and
reactive arms use the same 0.7 factor (decision D3).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from decision_engine.grading import LAMBDA_TRUTH_THRESHOLD

TEST_DAYS = 7  # same held-out week as decision_engine.tune_lambda.split_points
MINUTE = pd.Timedelta(minutes=1)
ABSOLUTE_MAX_INSTANCES = 10  # the project-wide hard cap
ABSOLUTE_MAX_LAMBDA_CONCURRENCY = 10  # the project-wide provisioned-concurrency cap

THUMBNAIL = "thumbnail_service"
CATALOG = "catalog_service"


# --------------------------------------------------------------------------------------------- parameters


@dataclass(frozen=True)
class ThumbnailParams:
    """`n_bursts` and `gap_minutes` are filled in after calibration (idle-container lifetime)."""

    n_bursts: int
    gap_minutes: int
    # Load scaling. At the recorded volumes (<= 400 invocations/min) a 52 ms handler needs well under ONE container
    # (400/60 x 0.052 = 0.35), so every arm would see ~1 cold start per burst and provisioned concurrency could not
    # matter. The load is therefore scaled so the loudest selected burst needs `target_peak_concurrency` containers.
    target_peak_concurrency: float
    billed_seconds: float  # measured billed handler duration (calibration)
    lead_minutes: int = 10  # the planted ramp is 4 minutes; 10 keeps all of it plus some quiet before
    tail_minutes: int = 5
    burst_threshold: float = LAMBDA_TRUTH_THRESHOLD
    merge_gap_minutes: int = 3  # dips shorter than this inside a burst do not split it
    history_days: int = 7

    def __post_init__(self) -> None:
        if self.n_bursts < 1:
            raise ValueError("n_bursts must be at least 1")
        if self.gap_minutes < 1:
            raise ValueError("gap_minutes must be at least 1")
        if not 0 < self.target_peak_concurrency <= ABSOLUTE_MAX_LAMBDA_CONCURRENCY:
            raise ValueError(f"target_peak_concurrency must be in (0, {ABSOLUTE_MAX_LAMBDA_CONCURRENCY}] (the provisioned-concurrency cap)")
        if not self.billed_seconds > 0:
            raise ValueError("billed_seconds must be positive")
        if self.lead_minutes < 0 or self.tail_minutes < 0 or self.merge_gap_minutes < 0:
            raise ValueError("lead/tail/merge minutes cannot be negative")
        if self.history_days < 1:
            raise ValueError("history_days must be at least 1")


@dataclass(frozen=True)
class CatalogParams:
    """`capacity_rpm_per_instance` is the value MEASURED in calibration (no default on purpose)."""

    capacity_rpm_per_instance: float
    utilization: float = 0.7
    target_peak_instances: int = 4
    window_minutes: int = 120
    smooth_minutes: int = 15
    history_days: int = 7

    def __post_init__(self) -> None:
        if not self.capacity_rpm_per_instance > 0:
            raise ValueError("capacity_rpm_per_instance must be positive")
        if not 0 < self.utilization <= 1:
            raise ValueError("utilization must be in (0, 1]")
        if not 2 <= self.target_peak_instances <= 5:
            raise ValueError("target_peak_instances must be 2-5 (decision D4)")
        if self.window_minutes < 2 * self.smooth_minutes:
            raise ValueError("window_minutes must be at least twice smooth_minutes")
        if self.history_days < 1:
            raise ValueError("history_days must be at least 1")


# ------------------------------------------------------------------------------------------------ results


@dataclass(frozen=True)
class Episode:
    """One burst in scenario time. `onset`..`end` are the loud minutes; the segment adds lead and tail."""

    index: int
    segment_start: pd.Timestamp
    onset: pd.Timestamp
    end: pd.Timestamp
    segment_end: pd.Timestamp
    peak: float
    source_onset: pd.Timestamp  # where this burst started in the original recorded series


@dataclass(frozen=True)
class ServiceScenario:
    service: str
    history: pd.Series  # real recorded week before the scenario (natural units)
    series: pd.Series  # the scenario itself (natural units), starts right after `history`
    train_cutoff: pd.Timestamp  # forecasters must be fitted on data strictly before this
    load_scale: float  # real requests/min = series * load_scale
    episodes: tuple[Episode, ...] = ()
    capacity_rpm_per_instance: float | None = None  # measured; catalog only
    utilization: float | None = None
    params: dict = field(default_factory=dict)

    @property
    def duration_minutes(self) -> int:
        return len(self.series)

    @property
    def offered_rpm(self) -> pd.Series:
        """What the load generator should send, in real requests per minute."""
        return self.series * self.load_scale

    @property
    def natural_capacity_per_instance_rpm(self) -> float | None:
        """capacity_per_instance_rpm to give the predictive policy (it works in natural units)."""
        if self.capacity_rpm_per_instance is None or self.utilization is None:
            return None
        return self.capacity_rpm_per_instance * self.utilization / self.load_scale

    @property
    def natural_duration_seconds(self) -> float | None:
        """avg_duration_seconds to give the predictive Lambda policy (it works in natural units): billed duration x load_scale."""
        billed = self.params.get("billed_seconds")
        return None if billed is None else billed * self.load_scale

    @property
    def static_peak_instances(self) -> int | None:
        """Instances a fixed fleet needs to carry the scenario's highest minute at the target utilization."""
        if self.capacity_rpm_per_instance is None or self.utilization is None:
            return None
        needed = math.ceil(float(self.offered_rpm.max()) / (self.capacity_rpm_per_instance * self.utilization))
        return max(1, min(ABSOLUTE_MAX_INSTANCES, needed))

    def stitched(self) -> pd.Series:
        """History followed by the scenario on one gapless clock: what the loop's replay source reads."""
        joined = pd.concat([self.history, self.series])
        _require_minute_grid(joined)
        return joined


@dataclass(frozen=True)
class Scenario:
    services: dict[str, ServiceScenario]

    def fingerprint(self) -> str:
        """Content hash: two runs used the same scenario iff this matches."""
        digest = hashlib.sha256()
        for name in sorted(self.services):
            s = self.services[name]
            digest.update(name.encode("utf-8"))
            digest.update(repr(s.load_scale).encode("utf-8"))
            digest.update(_csv_text(s.history).encode("utf-8"))
            digest.update(_csv_text(s.series).encode("utf-8"))
        return digest.hexdigest()


# ------------------------------------------------------------------------------------------------ helpers


def _csv_text(series: pd.Series) -> str:
    return series.to_csv(lineterminator="\n", header=["value"], index_label="time")


def _require_minute_grid(series: pd.Series) -> None:
    if len(series) < 2:
        raise ValueError("series is too short")
    steps = np.diff(series.index.values.astype("datetime64[s]").astype("int64"))
    if not (steps == 60).all():
        raise ValueError("series must be a gapless 1-minute grid (no gaps, duplicates or reordering)")
    if series.isna().any():
        raise ValueError("series contains missing values")


def _test_window(series: pd.Series, history_days: int) -> tuple[pd.Timestamp, int]:
    """(test_start, position of test_start). The history before it must fit inside the recording."""
    _require_minute_grid(series)
    test_start = series.index.max() - pd.Timedelta(days=TEST_DAYS)
    if test_start - pd.Timedelta(days=history_days) < series.index.min():
        raise ValueError(f"the recording is too short for {history_days} days of history before the held-out week")
    return test_start, int(series.index.get_loc(test_start))


def find_episodes(values: np.ndarray, threshold: float, merge_gap_minutes: int) -> list[tuple[int, int]]:
    """Inclusive (first, last) positions of runs at or above `threshold`; near-touching runs are merged."""
    above = values >= threshold
    runs: list[list[int]] = []
    i, n = 0, len(above)
    while i < n:
        if above[i]:
            j = i
            while j + 1 < n and above[j + 1]:
                j += 1
            runs.append([i, j])
            i = j + 1
        else:
            i += 1
    merged: list[list[int]] = []
    for first, last in runs:
        if merged and first - merged[-1][1] - 1 <= merge_gap_minutes:
            merged[-1][1] = last
        else:
            merged.append([first, last])
    return [(a, b) for a, b in merged]


# --------------------------------------------------------------------------------------- thumbnail (Lambda)


def build_thumbnail_scenario(series: pd.Series, params: ThumbnailParams) -> ServiceScenario:
    test_start, p0 = _test_window(series, params.history_days)
    values = series.to_numpy()

    runs = [(p0 + a, p0 + b) for a, b in find_episodes(values[p0:], params.burst_threshold, params.merge_gap_minutes)]
    if len(runs) < params.n_bursts:
        raise ValueError(f"the held-out week has only {len(runs)} bursts; {params.n_bursts} were requested")

    # Quiet minutes: everything in the held-out week that is not near ANY burst (selected or not). The
    # cut stretches are refilled from this pool, in order, so no ramp or aftermath leaks into a gap.
    near_burst = np.zeros(len(values), dtype=bool)
    for a, b in runs:
        near_burst[max(p0, a - params.lead_minutes) : b + params.tail_minutes + 1] = True
    pool = values[p0:][~near_burst[p0:]]
    if len(pool) < 1:
        raise ValueError("no quiet minutes in the held-out week to build the gaps from")

    chosen = runs[: params.n_bursts]  # the selection rule: the first N, chronologically
    segments = [(max(p0, a - params.lead_minutes), b + params.tail_minutes) for a, b in chosen]
    for (_, prev_end), (next_start, _) in zip(segments, segments[1:]):
        if next_start <= prev_end:
            raise ValueError("two selected bursts are closer together than lead_minutes + tail_minutes")

    pieces: list[np.ndarray] = []
    offsets: list[tuple[int, int]] = []  # (segment start offset, segment end offset) in the scenario
    cursor = 0
    pool_pos = 0

    def take_quiet() -> np.ndarray:
        nonlocal pool_pos
        idx = (pool_pos + np.arange(params.gap_minutes)) % len(pool)
        pool_pos = (pool_pos + params.gap_minutes) % len(pool)
        return pool[idx]

    for start, end in segments:
        gap = take_quiet()
        pieces.append(gap)
        cursor += len(gap)
        segment = values[start : end + 1]
        pieces.append(segment)
        offsets.append((cursor, cursor + len(segment) - 1))
        cursor += len(segment)
    pieces.append(take_quiet())

    scenario_values = np.concatenate(pieces)
    index = pd.date_range(test_start, periods=len(scenario_values), freq="min")
    scenario = pd.Series(scenario_values, index=index, name=series.name, dtype="float64")

    episodes = []
    for k, ((a, b), (seg_a, seg_b), (off_a, off_b)) in enumerate(zip(chosen, segments, offsets)):
        onset_off = off_a + (a - seg_a)
        end_off = off_a + (b - seg_a)
        episodes.append(
            Episode(
                index=k,
                segment_start=index[off_a],
                onset=index[onset_off],
                end=index[end_off],
                segment_end=index[off_b],
                peak=float(values[a : b + 1].max()),
                source_onset=series.index[a],
            )
        )

    loudest = max(e.peak for e in episodes)
    if loudest <= 0:
        raise ValueError("the selected bursts carry no traffic")
    load_scale = params.target_peak_concurrency * 60.0 / (params.billed_seconds * loudest)

    history = series.loc[test_start - pd.Timedelta(days=params.history_days) : test_start - MINUTE]
    return ServiceScenario(
        service=THUMBNAIL,
        history=history,
        series=scenario,
        train_cutoff=test_start,
        load_scale=load_scale,
        episodes=tuple(episodes),
        params=asdict(params),
    )


# ------------------------------------------------------------------------------------------- catalog (EC2)


def _first_full_weekday(test_start: pd.Timestamp, last: pd.Timestamp) -> pd.Timestamp:
    day = test_start.ceil("D")
    while day + pd.Timedelta(days=1) - MINUTE <= last:
        if day.weekday() < 5:
            return day
        day += pd.Timedelta(days=1)
    raise ValueError("the held-out week has no full weekday")


def steepest_rise_start(values: np.ndarray, window: int, smooth: int) -> int:
    """Start position of the `window`-minute stretch whose end is highest above its start.

    The rise is the mean of the last `smooth` minutes minus the mean of the first `smooth` minutes. The first
    best window wins ties. Positions are relative to `values`.
    """
    if len(values) < window:
        raise ValueError("not enough data for one window")
    csum = np.concatenate([[0.0], np.cumsum(values)])
    starts = np.arange(0, len(values) - window + 1)
    first = (csum[starts + smooth] - csum[starts]) / smooth
    last = (csum[starts + window] - csum[starts + window - smooth]) / smooth
    return int(np.argmax(last - first))


def build_catalog_scenario(series: pd.Series, params: CatalogParams) -> ServiceScenario:
    test_start, _p0 = _test_window(series, params.history_days)
    day = _first_full_weekday(test_start, series.index.max())
    day_pos = int(series.index.get_loc(day))
    day_values = series.to_numpy()[day_pos : day_pos + 1440]

    start = day_pos + steepest_rise_start(day_values, params.window_minutes, params.smooth_minutes)
    window_start = series.index[start]
    if window_start - pd.Timedelta(days=params.history_days) < series.index.min():
        raise ValueError("not enough history before the steepest-rise window")

    scenario = series.iloc[start : start + params.window_minutes].copy()
    smoothed_peak = float(scenario.rolling(params.smooth_minutes).mean().max())
    if smoothed_peak <= 0:
        raise ValueError("the chosen window carries no traffic")
    load_scale = params.target_peak_instances * params.utilization * params.capacity_rpm_per_instance / smoothed_peak

    history = series.loc[window_start - pd.Timedelta(days=params.history_days) : window_start - MINUTE]
    return ServiceScenario(
        service=CATALOG,
        history=history,
        series=scenario,
        train_cutoff=test_start,
        load_scale=load_scale,
        capacity_rpm_per_instance=params.capacity_rpm_per_instance,
        utilization=params.utilization,
        params=asdict(params),
    )


# ------------------------------------------------------------------------------------------ save / load


def save_scenario(scenario: Scenario, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    meta: dict = {"fingerprint": scenario.fingerprint(), "services": {}}
    for name, s in scenario.services.items():
        (directory / f"{name}_history.csv").write_text(_csv_text(s.history), encoding="utf-8", newline="")
        (directory / f"{name}_series.csv").write_text(_csv_text(s.series), encoding="utf-8", newline="")
        meta["services"][name] = {
            "train_cutoff": s.train_cutoff.isoformat(),
            "load_scale": s.load_scale,
            "capacity_rpm_per_instance": s.capacity_rpm_per_instance,
            "utilization": s.utilization,
            "params": s.params,
            "episodes": [
                {
                    "index": e.index,
                    "segment_start": e.segment_start.isoformat(),
                    "onset": e.onset.isoformat(),
                    "end": e.end.isoformat(),
                    "segment_end": e.segment_end.isoformat(),
                    "peak": e.peak,
                    "source_onset": e.source_onset.isoformat(),
                }
                for e in s.episodes
            ],
        }
    (directory / "scenario.json").write_text(json.dumps(meta, indent=2), encoding="utf-8", newline="")


def _read_series(path: Path) -> pd.Series:
    frame = pd.read_csv(path, parse_dates=["time"], index_col="time", encoding="utf-8")
    series = frame["value"].astype("float64")
    series.index.freq = None
    return series


def load_scenario(directory: Path) -> Scenario:
    meta = json.loads((directory / "scenario.json").read_text(encoding="utf-8"))
    services: dict[str, ServiceScenario] = {}
    for name, m in meta["services"].items():
        services[name] = ServiceScenario(
            service=name,
            history=_read_series(directory / f"{name}_history.csv"),
            series=_read_series(directory / f"{name}_series.csv"),
            train_cutoff=pd.Timestamp(m["train_cutoff"]),
            load_scale=m["load_scale"],
            episodes=tuple(
                Episode(
                    index=e["index"],
                    segment_start=pd.Timestamp(e["segment_start"]),
                    onset=pd.Timestamp(e["onset"]),
                    end=pd.Timestamp(e["end"]),
                    segment_end=pd.Timestamp(e["segment_end"]),
                    peak=e["peak"],
                    source_onset=pd.Timestamp(e["source_onset"]),
                )
                for e in m["episodes"]
            ),
            capacity_rpm_per_instance=m["capacity_rpm_per_instance"],
            utilization=m["utilization"],
            params=m["params"],
        )
    loaded = Scenario(services)
    if loaded.fingerprint() != meta["fingerprint"]:
        raise ValueError("scenario files do not match their recorded fingerprint (edited or corrupted)")
    return loaded
