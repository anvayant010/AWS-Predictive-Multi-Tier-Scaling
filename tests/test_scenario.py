import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import comparison.scenario as sc
from comparison.scenario import (
    CatalogParams,
    Scenario,
    ThumbnailParams,
    build_catalog_scenario,
    build_thumbnail_scenario,
    find_episodes,
    load_scenario,
    save_scenario,
    steepest_rise_start,
)
from forecasting.data_pipeline.synthetic_data import generate_metrics

START = datetime(2026, 6, 1, tzinfo=timezone.utc)  # a Monday


@pytest.fixture(scope="module")
def recorded() -> dict[str, pd.Series]:
    """30 days of the production-style synthetic data (seed 42, ramp 0.7), as 1-minute series."""
    rows = generate_metrics(START, 30, 60, seed=42, thumbnail_ramp_fraction=0.7)
    out: dict[str, list] = {"catalog_service": [], "thumbnail_service": []}
    for t, service, metric, value, _source in rows:
        if (service, metric) in {("catalog_service", "RequestCount"), ("thumbnail_service", "Invocations")}:
            out[service].append((t.replace(tzinfo=None), value))
    return {s: pd.Series(dict(v), dtype="float64").sort_index() for s, v in out.items()}


def thumb_params(**over) -> ThumbnailParams:
    base = dict(n_bursts=6, gap_minutes=40, target_peak_concurrency=8.0, billed_seconds=0.052)
    base.update(over)
    return ThumbnailParams(**base)


def catalog_params(**over) -> CatalogParams:
    base = dict(capacity_rpm_per_instance=1750.0)
    base.update(over)
    return CatalogParams(**base)


def test_the_held_out_week_is_the_same_one_the_tuning_used(recorded) -> None:
    from decision_engine.tune_lambda import split_points

    series = recorded["thumbnail_service"]
    assert build_thumbnail_scenario(series, thumb_params()).train_cutoff == split_points(series)[1]


# --- finding bursts ------------------------------------------------------------------------------------


def test_find_episodes_returns_inclusive_runs_above_the_threshold() -> None:
    v = np.array([1, 1, 30, 40, 30, 1, 1, 1, 1, 1, 1, 1, 50, 1.0])

    assert find_episodes(v, 20.0, merge_gap_minutes=0) == [(2, 4), (12, 12)]


def test_a_short_dip_inside_a_burst_does_not_split_it() -> None:
    v = np.array([1, 40, 40, 5, 5, 40, 40, 1, 1.0])

    assert find_episodes(v, 20.0, merge_gap_minutes=3) == [(1, 6)]
    assert find_episodes(v, 20.0, merge_gap_minutes=1) == [(1, 2), (5, 6)]


def test_find_episodes_on_quiet_data_is_empty() -> None:
    assert find_episodes(np.full(50, 2.0), 20.0, 3) == []


# --- thumbnail scenario --------------------------------------------------------------------------------


def _held_out_runs(series: pd.Series) -> tuple[pd.Series, list[tuple[int, int]]]:
    test_start = series.index.max() - pd.Timedelta(days=7)
    week = series.loc[test_start:]
    return week, find_episodes(week.to_numpy(), 20.0, 3)


def test_the_selected_bursts_are_the_first_n_in_chronological_order(recorded) -> None:
    series = recorded["thumbnail_service"]
    episodes = build_thumbnail_scenario(series, thumb_params(n_bursts=5)).episodes
    week, runs = _held_out_runs(series)

    assert [e.source_onset for e in episodes] == [week.index[a] for a, _ in runs[:5]]
    assert [e.source_onset for e in episodes] == sorted(e.source_onset for e in episodes)


def test_the_selection_ignores_how_big_a_burst_is(recorded) -> None:
    """A later, bigger burst must never displace an earlier, smaller one (no cherry-picking)."""
    series = recorded["thumbnail_service"]
    episodes = build_thumbnail_scenario(series, thumb_params(n_bursts=4)).episodes
    week, runs = _held_out_runs(series)
    peaks = [float(week.to_numpy()[a : b + 1].max()) for a, b in runs]

    assert [e.peak for e in episodes] == peaks[:4]
    assert max(peaks) > max(peaks[:4])  # bigger bursts exist later in the week and were not preferred


def test_asking_for_more_bursts_than_exist_is_an_error_not_a_shorter_scenario(recorded) -> None:
    with pytest.raises(ValueError, match="only .* bursts"):
        build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params(n_bursts=10_000))


def test_each_burst_is_kept_at_real_speed_with_its_ramp_and_aftermath(recorded) -> None:
    series = recorded["thumbnail_service"]
    s = build_thumbnail_scenario(series, thumb_params())
    for e in s.episodes:
        in_scenario = s.series.loc[e.segment_start : e.segment_end].to_numpy()
        first = e.source_onset - pd.Timedelta(minutes=10)
        last = e.source_onset + (e.end - e.onset) + pd.Timedelta(minutes=5)
        assert np.array_equal(in_scenario, series.loc[first:last].to_numpy())
        assert e.segment_start <= e.onset <= e.end <= e.segment_end


def test_every_cut_stretch_is_exactly_gap_minutes_long(recorded) -> None:
    s = build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params(gap_minutes=37))
    eps = s.episodes

    assert eps[0].segment_start - s.series.index[0] == pd.Timedelta(minutes=37)  # lead-in gap
    for a, b in zip(eps, eps[1:]):
        assert b.segment_start - a.segment_end - pd.Timedelta(minutes=1) == pd.Timedelta(minutes=37)
    assert s.series.index[-1] - eps[-1].segment_end == pd.Timedelta(minutes=37)  # trailing gap


def test_gaps_longer_than_the_natural_ones_are_still_filled_with_quiet_minutes(recorded) -> None:
    s = build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params(n_bursts=3, gap_minutes=600))
    eps = s.episodes
    gap = s.series.loc[eps[0].segment_end + pd.Timedelta(minutes=1) : eps[1].segment_start - pd.Timedelta(minutes=1)]

    assert len(gap) == 600 and gap.max() < 20.0  # nothing that looks like a burst leaked into a gap


def test_gaps_hold_no_burst_or_ramp_minutes(recorded) -> None:
    s = build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params(n_bursts=8, gap_minutes=60))
    inside = pd.Series(False, index=s.series.index)
    for e in s.episodes:
        inside.loc[e.segment_start : e.segment_end] = True

    assert s.series[~inside].max() < 20.0


def test_the_scenario_continues_the_history_on_one_gapless_clock(recorded) -> None:
    s = build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params())

    assert s.series.index[0] - s.history.index[-1] == pd.Timedelta(minutes=1)
    stitched = s.stitched()
    assert stitched.index.is_unique and stitched.index.is_monotonic_increasing
    assert (np.diff(stitched.index.values.astype("datetime64[s]").astype("int64")) == 60).all()


def test_history_is_a_real_week_that_ends_before_the_held_out_week_begins(recorded) -> None:
    series = recorded["thumbnail_service"]
    s = build_thumbnail_scenario(series, thumb_params())

    assert len(s.history) == 7 * 1440
    assert s.history.index[-1] < s.train_cutoff
    assert s.history.equals(series.loc[s.history.index[0] : s.history.index[-1]])  # untouched real data


def test_a_burst_dense_scenario_is_much_shorter_than_the_week_it_came_from(recorded) -> None:
    s = build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params(n_bursts=8, gap_minutes=40))

    assert s.duration_minutes < 7 * 1440 / 5


def test_thumbnail_load_scale_makes_the_loudest_selected_burst_need_the_target_concurrency(recorded) -> None:
    s = build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params(target_peak_concurrency=8.0, billed_seconds=0.052))

    loudest = max(e.peak for e in s.episodes)
    assert loudest * s.load_scale / 60.0 * 0.052 == pytest.approx(8.0)
    assert s.load_scale > 1.0
    assert s.natural_capacity_per_instance_rpm is None and s.static_peak_instances is None


def test_the_load_scale_depends_only_on_the_selected_bursts_not_on_louder_ones_elsewhere(recorded) -> None:
    few = build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params(n_bursts=1))
    many = build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params(n_bursts=6))

    assert few.load_scale == pytest.approx(8.0 * 60.0 / (0.052 * few.episodes[0].peak))
    assert many.load_scale == pytest.approx(8.0 * 60.0 / (0.052 * max(e.peak for e in many.episodes)))


def test_the_policy_duration_in_natural_units_is_the_billed_duration_times_the_scale(recorded) -> None:
    s = build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params())

    assert s.natural_duration_seconds == pytest.approx(0.052 * s.load_scale)
    # the policy's needed units for the loudest burst then equal the target concurrency
    assert max(e.peak for e in s.episodes) / 60.0 * s.natural_duration_seconds == pytest.approx(8.0)


def test_a_catalog_scenario_has_no_policy_duration(recorded) -> None:
    assert build_catalog_scenario(recorded["catalog_service"], catalog_params()).natural_duration_seconds is None


def test_overlapping_selected_bursts_are_refused(recorded) -> None:
    with pytest.raises(ValueError, match="closer together"):
        build_thumbnail_scenario(
            recorded["thumbnail_service"], thumb_params(n_bursts=8, lead_minutes=60, tail_minutes=60)
        )


def test_the_parameters_that_calibration_decides_have_no_defaults() -> None:
    with pytest.raises(TypeError):
        ThumbnailParams()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        CatalogParams()  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [
    dict(n_bursts=0), dict(gap_minutes=0), dict(lead_minutes=-1), dict(history_days=0),
    dict(target_peak_concurrency=0), dict(target_peak_concurrency=10.5), dict(billed_seconds=0), dict(billed_seconds=-0.1),
])
def test_bad_thumbnail_parameters_are_rejected(bad) -> None:
    with pytest.raises(ValueError):
        thumb_params(**bad)


def test_the_builder_is_deterministic(recorded) -> None:
    a = build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params())
    b = build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params())

    assert a.series.equals(b.series) and a.episodes == b.episodes


def test_a_series_with_a_gap_is_refused(recorded) -> None:
    series = recorded["thumbnail_service"]
    broken = series.drop(series.index[5000])

    with pytest.raises(ValueError, match="gapless"):
        build_thumbnail_scenario(broken, thumb_params())


# --- catalog scenario ----------------------------------------------------------------------------------


def test_steepest_rise_start_finds_the_ramp() -> None:
    v = np.concatenate([np.full(100, 10.0), np.linspace(10, 200, 120), np.full(100, 200.0)])

    start = steepest_rise_start(v, window=120, smooth=15)

    assert 90 <= start <= 110  # the window that begins just as the ramp begins


def test_steepest_rise_start_prefers_the_largest_rise_not_the_highest_level() -> None:
    flat_high = np.full(200, 500.0)
    ramp = np.concatenate([np.full(50, 10.0), np.linspace(10, 100, 120), np.full(30, 100.0)])

    start = steepest_rise_start(np.concatenate([flat_high, ramp]), window=120, smooth=15)

    assert start >= 200  # the flat-but-high stretch has zero rise


def test_catalog_window_is_two_hours_inside_the_first_full_weekday_of_the_held_out_week(recorded) -> None:
    series = recorded["catalog_service"]
    s = build_catalog_scenario(series, catalog_params())
    test_start = series.index.max() - pd.Timedelta(days=7)
    first_day = test_start.ceil("D")

    assert s.duration_minutes == 120
    assert first_day <= s.series.index[0] and s.series.index[-1] < first_day + pd.Timedelta(days=1)
    assert s.series.index[0].weekday() < 5


def test_a_weekend_start_of_the_held_out_week_is_skipped_to_the_next_weekday() -> None:
    # 16 days from Thu 2026-06-04: the held-out week then begins on Sat 2026-06-13.
    rows = generate_metrics(datetime(2026, 6, 4, tzinfo=timezone.utc), 16, 60, seed=7)
    series = pd.Series(
        {t.replace(tzinfo=None): v for t, svc, m, v, _ in rows if (svc, m) == ("catalog_service", "RequestCount")}
    ).sort_index()
    test_start = series.index.max() - pd.Timedelta(days=7)
    assert test_start.ceil("D").weekday() == 5  # the premise: the first full day is a Saturday

    s = build_catalog_scenario(series, catalog_params(history_days=1))

    assert s.series.index[0].weekday() < 5 and s.series.index[0].normalize() == pd.Timestamp("2026-06-15")


def test_catalog_window_really_is_the_steepest_two_hours_of_that_day(recorded) -> None:
    series = recorded["catalog_service"]
    s = build_catalog_scenario(series, catalog_params())
    day = s.series.index[0].normalize()
    values = series.loc[day : day + pd.Timedelta(hours=23, minutes=59)].to_numpy()

    def rise(start: int) -> float:
        return values[start + 105 : start + 120].mean() - values[start : start + 15].mean()

    chosen = int((s.series.index[0] - day) / pd.Timedelta(minutes=1))
    assert all(rise(chosen) >= rise(other) for other in range(0, len(values) - 120 + 1, 7))


def test_catalog_scenario_rises_through_the_window(recorded) -> None:
    s = build_catalog_scenario(recorded["catalog_service"], catalog_params())
    v = s.series.to_numpy()

    assert v[-15:].mean() > 1.5 * v[:15].mean()  # a sine rise over 2 h from the trough side


def test_catalog_load_is_scaled_so_the_peak_needs_the_target_number_of_instances(recorded) -> None:
    p = catalog_params(target_peak_instances=4)
    s = build_catalog_scenario(recorded["catalog_service"], p)
    peak = s.offered_rpm.rolling(15).mean().max()

    assert peak / (p.capacity_rpm_per_instance * p.utilization) == pytest.approx(4.0)


def test_the_policy_capacity_in_natural_units_matches_the_real_one_after_scaling(recorded) -> None:
    s = build_catalog_scenario(recorded["catalog_service"], catalog_params())

    # needing N instances in natural units must mean needing N instances in real units
    n_natural = s.series.max() / s.natural_capacity_per_instance_rpm
    n_real = s.offered_rpm.max() / (s.capacity_rpm_per_instance * s.utilization)
    assert n_natural == pytest.approx(n_real)


def test_static_peak_instances_cover_the_highest_minute_and_respect_the_cap(recorded) -> None:
    s = build_catalog_scenario(recorded["catalog_service"], catalog_params(target_peak_instances=4))

    assert s.static_peak_instances * s.capacity_rpm_per_instance * s.utilization >= s.offered_rpm.max()
    assert 4 <= s.static_peak_instances <= 10
    huge = dataclasses.replace(s, load_scale=s.load_scale * 10)
    assert huge.static_peak_instances == 10  # never above the hard cap


def test_catalog_history_is_the_real_week_right_before_the_window(recorded) -> None:
    series = recorded["catalog_service"]
    s = build_catalog_scenario(series, catalog_params())

    assert s.series.index[0] - s.history.index[-1] == pd.Timedelta(minutes=1)
    assert len(s.history) == 7 * 1440
    assert s.history.equals(series.loc[s.history.index[0] : s.history.index[-1]])
    assert s.train_cutoff <= s.series.index[0]


@pytest.mark.parametrize(
    "bad",
    [
        dict(capacity_rpm_per_instance=0),
        dict(utilization=0),
        dict(utilization=1.5),
        dict(target_peak_instances=1),
        dict(target_peak_instances=6),
    ],
)
def test_bad_catalog_parameters_are_rejected(bad) -> None:
    with pytest.raises(ValueError):
        catalog_params(**bad)


# --- save / load / fingerprint -------------------------------------------------------------------------


def make_scenario(recorded) -> Scenario:
    return Scenario(
        {
            "thumbnail_service": build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params()),
            "catalog_service": build_catalog_scenario(recorded["catalog_service"], catalog_params()),
        }
    )


def test_a_saved_scenario_loads_back_identical(recorded, tmp_path: Path) -> None:
    original = make_scenario(recorded)
    save_scenario(original, tmp_path)

    loaded = load_scenario(tmp_path)

    assert loaded.fingerprint() == original.fingerprint()
    for name, s in original.services.items():
        back = loaded.services[name]
        assert np.allclose(back.series.to_numpy(), s.series.to_numpy())
        assert back.series.index.equals(s.series.index) and back.episodes == s.episodes
        assert back.load_scale == s.load_scale and back.train_cutoff == s.train_cutoff


def test_a_tampered_scenario_file_is_detected(recorded, tmp_path: Path) -> None:
    save_scenario(make_scenario(recorded), tmp_path)
    path = tmp_path / "thumbnail_service_series.csv"
    lines = path.read_text(encoding="utf-8").split("\n")
    lines[3] = lines[3].rsplit(",", 1)[0] + ",999.0"
    path.write_text("\n".join(lines), encoding="utf-8", newline="")

    with pytest.raises(ValueError, match="fingerprint"):
        load_scenario(tmp_path)


def test_the_fingerprint_changes_when_the_scenario_changes(recorded) -> None:
    a = make_scenario(recorded)
    other = Scenario(
        {
            "thumbnail_service": build_thumbnail_scenario(recorded["thumbnail_service"], thumb_params(n_bursts=7)),
            "catalog_service": a.services["catalog_service"],
        }
    )

    assert a.fingerprint() != other.fingerprint()


def test_the_scenario_builder_needs_no_aws_database_or_model() -> None:
    source = Path(sc.__file__).read_text(encoding="utf-8")

    assert "boto3" not in source and "psycopg2" not in source and "sklearn" not in source
