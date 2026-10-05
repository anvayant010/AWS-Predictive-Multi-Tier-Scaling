from datetime import datetime, timezone

import pandas as pd
import pytest

from forecasting.burst_diagnostic import BURST_LEVEL, Burst, reconstruct_bursts, verify_against_series
from forecasting.data_pipeline.synthetic_data import generate_metrics


def _invocations(seed: int, days: int, ramp_fraction: float) -> pd.Series:
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = generate_metrics(start, days, 60, seed=seed, thumbnail_ramp_fraction=ramp_fraction)
    values = [r[3] for r in rows if r[2] == "Invocations"]
    return pd.Series(values, index=pd.date_range("2026-06-01", periods=len(values), freq="min"))


def test_reconstructed_onsets_match_generated_series() -> None:
    series = _invocations(seed=42, days=10, ramp_fraction=0.7)

    bursts = reconstruct_bursts(42, 10, ramp_fraction=0.7, ramp_minutes=4)

    assert len(bursts) > 0
    assert verify_against_series(bursts, series) >= 0.95
    assert all(series.iloc[b.onset_index] >= BURST_LEVEL for b in bursts)


def test_reconstructed_ramp_flags_match_the_ramp_in_the_series() -> None:
    series = _invocations(seed=7, days=14, ramp_fraction=0.7)

    bursts = reconstruct_bursts(7, 14, ramp_fraction=0.7, ramp_minutes=4)

    ramped = [b for b in bursts if b.ramped and b.onset_index >= 5]
    unramped = [b for b in bursts if not b.ramped and b.onset_index >= 5]
    assert ramped and unramped
    # One minute before onset: ramped bursts are already climbing, unramped are still quiet.
    assert all(series.iloc[b.onset_index - 1] > 8 for b in ramped)
    assert all(series.iloc[b.onset_index - 1] < 8 for b in unramped)


def test_verify_against_series_rejects_a_mismatched_seed() -> None:
    series = _invocations(seed=42, days=10, ramp_fraction=0.7)
    wrong = reconstruct_bursts(43, 10, ramp_fraction=0.7, ramp_minutes=4)

    with pytest.raises(ValueError, match="seed or ramp settings differ"):
        verify_against_series(wrong, series)


def test_burst_is_a_plain_value_object() -> None:
    assert Burst(onset_index=5, ramped=True) == Burst(onset_index=5, ramped=True)


def test_size_stats_perfect_and_blind_forecasts() -> None:
    from forecasting.burst_diagnostic import size_stats

    actual = pd.Series([100.0, 200.0, 400.0])

    perfect = size_stats(actual, actual * 1.1, actual)
    blind = size_stats(pd.Series([2.0] * 3), pd.Series([3.0] * 3), actual)

    assert perfect["corr_p50_actual"] == pytest.approx(1.0)
    assert perfect["p90_covers_actual"] == 1.0
    assert perfect["p90_within_2x"] == 1.0
    assert blind["p90_covers_actual"] == 0.0
    assert blind["p90_within_2x"] == 0.0
    assert blind["median_p50_over_actual"] < 0.05
