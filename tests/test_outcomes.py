import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import decision_engine.dry_run as dry_run
import scheduler.outcomes as outcomes
from decision_engine.grading import LAMBDA_TRUTH_THRESHOLD, needed_capacity
from decision_engine.models import Forecast
from scheduler.loop_handler import LoopConfig, run_replay
from scheduler.outcomes import attach_outcomes, format_outcome_summary, summarize_outcomes
from scheduler.records import DecisionRecord

T0 = pd.Timestamp("2026-06-01 02:00")


def rec(service: str = "thumbnail_service", when=T0, desired: int = 1, status: str = "dry_run", **overrides) -> DecisionRecord:
    base = dict(
        time=when.to_pydatetime() if isinstance(when, pd.Timestamp) else when,
        service=service, mode="aws", dry_run=True, kill_engaged=False, kill_reason="", shape="sustained",
        forecast_demand=10.0, current=0, proposed=desired, desired=desired, uncapped=desired, cap_applied=False,
        actuation_status=status, actuation_detail="", reason="",
    )
    base.update(overrides)
    return DecisionRecord(**base)


def series_with(values: dict[pd.Timestamp, float], start=T0, minutes: int = 120, baseline: float = 2.0) -> pd.Series:
    s = pd.Series(baseline, index=pd.date_range(start, periods=minutes, freq="min"), dtype="float64")
    for ts, v in values.items():
        s[ts] = v
    return s


def minute(n: int) -> pd.Timestamp:
    return T0 + pd.Timedelta(minutes=n)


# --- grading -----------------------------------------------------------------------------------------


def test_a_decision_that_covered_a_real_burst_is_exactly_right() -> None:
    series = series_with({minute(11): 400.0})  # thumbnail lookahead is 3 min: minutes 11..13 after a decision at :10

    graded = attach_outcomes([rec(when=minute(10), desired=1)], {"thumbnail_service": series})[0]

    assert (graded.actual_peak, graded.needed, graded.shortfall) == (400.0, 1, 0)


def test_a_missed_burst_is_negative_shortfall_under_provisioned() -> None:
    series = series_with({minute(12): 400.0})

    graded = attach_outcomes([rec(when=minute(10), desired=0)], {"thumbnail_service": series})[0]

    assert graded.needed == 1 and graded.shortfall == -1


def test_capacity_held_through_quiet_traffic_is_positive_shortfall_over_provisioned() -> None:
    graded = attach_outcomes([rec(when=minute(10), desired=1)], {"thumbnail_service": series_with({})})[0]

    assert (graded.needed, graded.shortfall) == (0, 1)


def test_ec2_is_graded_over_its_own_ten_minute_lookahead() -> None:
    series = series_with({minute(19): 120.0}, baseline=10.0)  # inside minutes 11..20, outside a 3-minute window

    graded = attach_outcomes([rec("catalog_service", minute(10), desired=2)], {"catalog_service": series})[0]

    assert graded.actual_peak == 120.0 and graded.needed == 3 and graded.shortfall == -1  # ceil(120 / 50)


def test_the_window_starts_after_the_decision_not_at_it() -> None:
    series = series_with({minute(10): 400.0})  # the spike is AT the decision minute, before the window opens

    graded = attach_outcomes([rec(when=minute(10), desired=0)], {"thumbnail_service": series})[0]

    assert graded.actual_peak == 2.0 and graded.shortfall == 0


# --- unknown stays unknown ------------------------------------------------------------------------------


def test_a_window_that_runs_past_the_end_of_the_data_is_left_ungraded() -> None:
    series = series_with({}, minutes=12)  # data ends at :11, a decision at :10 needs :11..:13

    graded = attach_outcomes([rec(when=minute(10))], {"thumbnail_service": series})[0]

    assert (graded.actual_peak, graded.needed, graded.shortfall) == (None, None, None)


def test_a_gap_in_the_data_leaves_the_decision_ungraded_rather_than_guessing() -> None:
    series = series_with({}).drop(minute(12))

    graded = attach_outcomes([rec(when=minute(10))], {"thumbnail_service": series})[0]

    assert graded.shortfall is None


def test_error_records_are_not_graded() -> None:
    errored = rec(status="error", error="RuntimeError: boom", desired=0)

    graded = attach_outcomes([errored], {"thumbnail_service": series_with({})})[0]

    assert graded == errored and graded.shortfall is None


def test_a_service_without_data_is_left_alone() -> None:
    record = rec()

    assert attach_outcomes([record], {})[0] == record


def test_the_original_records_are_not_modified() -> None:
    record = rec(when=minute(10))

    attach_outcomes([record], {"thumbnail_service": series_with({})})

    assert record.shortfall is None


def test_a_timezone_aware_record_time_is_matched_to_a_naive_utc_series() -> None:
    aware = datetime(2026, 6, 1, 2, 10, tzinfo=timezone.utc)

    graded = attach_outcomes([rec(when=aware)], {"thumbnail_service": series_with({minute(11): 400.0})})[0]

    assert graded.actual_peak == 400.0


def test_the_requested_capacity_is_graded_whatever_happened_to_it() -> None:
    series = series_with({minute(11): 400.0})
    for status in ("applied", "dry_run", "blocked_by_kill_switch", "offline", "noop"):
        graded = attach_outcomes([rec(when=minute(10), desired=1, status=status)], {"thumbnail_service": series})[0]
        assert graded.shortfall == 0, status


# --- one definition of "needed" ---------------------------------------------------------------------------


def test_the_grader_uses_the_same_needed_function_as_the_replay() -> None:
    assert dry_run.needed_capacity is needed_capacity
    assert dry_run.LAMBDA_TRUTH_THRESHOLD == LAMBDA_TRUTH_THRESHOLD == 20.0


def test_the_truth_threshold_can_be_overridden_for_sensitivity_checks() -> None:
    series = series_with({minute(11): 30.0})

    loose = attach_outcomes([rec(when=minute(10), desired=0)], {"thumbnail_service": series}, truth_threshold=20.0)[0]
    strict = attach_outcomes([rec(when=minute(10), desired=0)], {"thumbnail_service": series}, truth_threshold=50.0)[0]

    assert loose.needed == 1 and strict.needed == 0


# --- summary ------------------------------------------------------------------------------------------------


def test_summary_counts_under_over_and_exact() -> None:
    series = series_with({minute(11): 400.0, minute(31): 400.0}, minutes=200)
    records = [
        rec(when=minute(10), desired=1),  # burst covered     -> exact
        rec(when=minute(30), desired=0),  # burst missed      -> under
        rec(when=minute(50), desired=1),  # held through quiet-> over
        rec(when=minute(70), desired=0),  # quiet, none       -> exact
    ]

    summary = summarize_outcomes(attach_outcomes(records, {"thumbnail_service": series}))["thumbnail_service"]

    assert (summary["graded"], summary["ungraded"]) == (4, 0)
    assert (summary["under_rate"], summary["over_rate"], summary["exact_rate"]) == (0.25, 0.25, 0.5)
    assert summary["under_when_needed"] == 0.5  # two bursts needed capacity, one was missed
    assert summary["mean_shortfall"] == 0.0  # -1 and +1 cancel


def test_summary_reports_ungraded_decisions_separately_from_the_rates() -> None:
    series = series_with({}, minutes=30)
    records = [rec(when=minute(10)), rec(when=minute(29))]  # the second one's window runs off the end

    summary = summarize_outcomes(attach_outcomes(records, {"thumbnail_service": series}))["thumbnail_service"]

    assert (summary["graded"], summary["ungraded"]) == (1, 1)
    assert "1 ungraded" in format_outcome_summary({"thumbnail_service": summary})


def test_summary_with_nothing_graded_is_nan_not_zero() -> None:
    summary = summarize_outcomes([rec()])["thumbnail_service"]

    assert summary["graded"] == 0 and np.isnan(summary["under_rate"])


# --- in the replay ------------------------------------------------------------------------------------------


class FixedForecaster:
    def __init__(self, service: str, level: float) -> None:
        self.service, self.level = service, level

    def forecast(self, series: pd.Series, origin: pd.Timestamp) -> Forecast:
        n = 15
        return Forecast(self.service, origin.to_pydatetime(), tuple([0.0] * n), tuple([self.level] * n), tuple([self.level] * n))


class Source:
    def __init__(self, series: pd.Series) -> None:
        self.series = series

    def history(self, service: str, now: pd.Timestamp) -> pd.Series:
        return self.series.loc[:now]


def test_run_replay_grades_every_decision_when_given_the_full_history(tmp_path) -> None:
    series = pd.Series(2.0, index=pd.date_range("2026-06-01", periods=300, freq="min"))
    forecasters = {"thumbnail_service": FixedForecaster("thumbnail_service", 600.0)}
    log = tmp_path / "decisions.jsonl"

    records = run_replay(
        pd.Timestamp("2026-06-01 02:00"), 5, LoopConfig(), Source(series), forecasters, None,
        log_path=log, outcome_series={"thumbnail_service": series},
    )

    assert all(r.shortfall is not None for r in records)
    assert all(r.shortfall > 0 for r in records)  # it provisioned for a burst that never came: over-provisioned
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert {"actual_peak", "needed", "shortfall"} <= set(row) and row["needed"] == 0


def test_run_replay_without_history_leaves_the_outcome_fields_empty() -> None:
    series = pd.Series(2.0, index=pd.date_range("2026-06-01", periods=300, freq="min"))
    forecasters = {"thumbnail_service": FixedForecaster("thumbnail_service", 600.0)}

    records = run_replay(pd.Timestamp("2026-06-01 02:00"), 3, LoopConfig(), Source(series), forecasters, None)

    assert all(r.shortfall is None and r.actual_peak is None for r in records)


# --- hygiene ------------------------------------------------------------------------------------------------


def test_the_outcome_grader_needs_no_database_model_or_aws_stack() -> None:
    source = Path(outcomes.__file__).read_text(encoding="utf-8")

    assert "boto3" not in source and "psycopg2" not in source
    assert "forecasting." not in source and "sklearn" not in source
