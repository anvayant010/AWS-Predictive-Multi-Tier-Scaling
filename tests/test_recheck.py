import pytest

import comparison.recheck as rc
from comparison.recheck import (
    DEFAULT_PLAN,
    RecheckCell,
    cell_record,
    keep_alive_knee,
    much_lower,
    recheck_verdict,
    slow_share,
)


def rows(n_fast: int, n_slow: int = 0, n_failed: int = 0, fast_ms: float = 500.0, slow_ms: float = 3500.0) -> list[dict]:
    return (
        [{"ms": fast_ms, "ok": True} for _ in range(n_fast)]
        + [{"ms": slow_ms, "ok": True} for _ in range(n_slow)]
        + [{"ms": 12.0, "ok": False} for _ in range(n_failed)]  # refused at once: failed, but not slow
    )


def cell(mode: str, instances: int, rpm: float, slow: float, fidelity: float = 1.0) -> dict:
    return {"mode": mode, "instances": instances, "offered_rpm": rpm, "slow_share": slow, "fidelity": fidelity}


# --- the plan --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [dict(mode="udp", instances=1, rates=(1.0,)), dict(mode="keep_alive", instances=0, rates=(1.0,)), dict(mode="keep_alive", instances=3, rates=(1.0,)),
     dict(mode="keep_alive", instances=1, rates=()), dict(mode="keep_alive", instances=1, rates=(100.0, 0.0)), dict(mode="keep_alive", instances=1, rates=(-5.0,))],
)
def test_nonsense_blocks_are_refused(kwargs) -> None:
    with pytest.raises(ValueError):
        RecheckCell(**kwargs)


def test_the_default_plan_contains_the_reference_and_both_comparison_steps_and_respects_the_instance_cap() -> None:
    steps = {(b.mode, b.instances, r) for b in DEFAULT_PLAN for r in b.rates}

    assert rc.REFERENCE in steps  # re-measures the first calibration's configuration
    assert ("keep_alive", 1, 5000.0) in steps  # does reusing connections help?
    assert ("per_request", 2, 5000.0) in steps  # does a second instance help?
    assert max(b.instances for b in DEFAULT_PLAN) <= 2
    assert DEFAULT_PLAN[0].instances == 1  # the stack starts with one instance


# --- slow share and the table row ----------------------------------------------------------------------------------------


def test_slow_share_counts_slow_and_failed_requests_among_all_requests() -> None:
    assert slow_share(rows(90, n_slow=6, n_failed=4)) == pytest.approx(0.10)


def test_a_request_at_exactly_the_threshold_is_not_slow() -> None:
    assert slow_share([{"ms": rc.SLOW_MS, "ok": True}]) == 0.0
    assert slow_share([{"ms": rc.SLOW_MS + 0.1, "ok": True}]) == 1.0


def test_slow_share_of_nothing_is_unknown_not_zero() -> None:
    assert slow_share([]) is None


def test_a_table_row_has_the_load_numbers_the_slow_share_and_the_connection_cost() -> None:
    r = cell_record("keep_alive", 2, rows(190, n_slow=10), 400.0, 30.0, connects_per_request=0.02)

    assert (r["mode"], r["instances"], r["offered_rpm"], r["sent"]) == ("keep_alive", 2, 400.0, 200)
    assert r["slow_share"] == pytest.approx(0.05) and r["error_rate"] == 0.0
    assert r["achieved_rpm"] == pytest.approx(400.0) and r["fidelity"] == pytest.approx(1.0)
    assert r["connects_per_request"] == 0.02 and r["p50_ms"] == 500.0 and r["p99_ms"] == pytest.approx(3500.0)


def test_a_table_row_for_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError):
        cell_record("pigeon", 1, rows(5), 100.0, 30.0, connects_per_request=None)


def test_much_lower_needs_at_most_half_and_at_least_a_point_less() -> None:
    assert much_lower(0.08, 0.04) and not much_lower(0.08, 0.0401)
    assert not much_lower(0.01, 0.004)  # at most half, but under a point lower: noise-sized


# --- the pre-declared verdict --------------------------------------------------------------------------------------------


def verdict(ref: float, keep: float | None, second: float | None, **kw) -> dict:
    cells = [cell("per_request", 1, 5000.0, ref, **kw)]
    if keep is not None:
        cells.append(cell("keep_alive", 1, 5000.0, keep))
    if second is not None:
        cells.append(cell("per_request", 2, 5000.0, second))
    return recheck_verdict(cells)


def test_only_reusing_connections_helping_means_the_old_knee_was_connection_setup() -> None:
    v = verdict(0.08, 0.004, 0.075)

    assert v["conclusion"] == "connection_setup" and "not the instance" in v["explanation"]
    assert (v["reference_slow_share"], v["reused_connection_slow_share"], v["second_instance_slow_share"]) == (0.08, 0.004, 0.075)


def test_only_a_second_instance_helping_means_the_limit_is_instance_side() -> None:
    assert verdict(0.08, 0.075, 0.01)["conclusion"] == "instance_side"


def test_both_helping_is_reported_as_both() -> None:
    assert verdict(0.08, 0.01, 0.02)["conclusion"] == "both"


def test_neither_helping_is_reported_as_neither_and_says_the_old_number_cannot_be_trusted() -> None:
    v = verdict(0.08, 0.07, 0.06)

    assert v["conclusion"] == "neither" and "cannot be trusted" in v["explanation"]


def test_if_the_original_problem_does_not_reappear_nothing_is_concluded() -> None:
    v = verdict(0.012, 0.0, 0.0)

    assert v["conclusion"] == "not_reproduced"


def test_exactly_two_percent_still_counts_as_the_problem_being_present() -> None:
    assert verdict(0.02, 0.0, 0.02)["conclusion"] == "connection_setup"


def test_a_missing_reference_or_comparison_step_is_incomplete_not_a_guess() -> None:
    assert recheck_verdict([])["conclusion"] == "incomplete"
    assert recheck_verdict([cell("keep_alive", 1, 5000.0, 0.0)])["conclusion"] == "incomplete"  # no reference
    v = verdict(0.08, None, 0.01)
    assert v["conclusion"] == "incomplete" and "reused-connection" in v["explanation"]
    v = verdict(0.08, 0.01, None)
    assert v["conclusion"] == "incomplete" and "second-instance" in v["explanation"]


def test_a_reference_the_laptop_could_not_deliver_is_incomplete() -> None:
    v = verdict(0.08, 0.0, 0.0, fidelity=0.80)

    assert v["conclusion"] == "incomplete" and "did not deliver" in v["explanation"]


def test_a_step_with_no_slow_share_is_ignored_rather_than_crashing() -> None:
    cells = [cell("per_request", 1, 5000.0, None), cell("per_request", 1, 5000.0, 0.08), cell("keep_alive", 1, 5000.0, 0.0), cell("per_request", 2, 5000.0, 0.07)]

    assert recheck_verdict(cells)["conclusion"] == "connection_setup"


def test_the_comparison_steps_must_be_at_the_same_rate_as_the_reference() -> None:
    cells = [cell("per_request", 1, 5000.0, 0.08), cell("keep_alive", 1, 4000.0, 0.0), cell("per_request", 2, 4000.0, 0.0)]

    assert recheck_verdict(cells)["conclusion"] == "incomplete"


def test_the_verdict_states_its_rule() -> None:
    rule = verdict(0.08, 0.0, 0.08)["rule"]

    assert rule == {"slow_ms": 2500.0, "much_lower_factor": 0.5, "min_absolute_drop": 0.01, "not_reproduced_below": 0.02}


# --- the keep-alive knee --------------------------------------------------------------------------------------------------


def step(mode: str, instances: int, rpm: float, p95: float, errors: int = 0) -> dict:
    return {"mode": mode, "instances": instances, "offered_rpm": rpm, "sent": 1000, "errors": errors, "p50_ms": 500.0, "p95_ms": p95,
            "p99_ms": p95 * 2, "duration_s": 60.0 * 1000 / rpm}


def test_the_keep_alive_knee_is_the_highest_passing_one_instance_reused_connection_step() -> None:
    cells = [step("keep_alive", 1, 400.0, 600.0), step("keep_alive", 1, 4000.0, 700.0), step("keep_alive", 1, 8000.0, 2000.0)]

    k = keep_alive_knee(cells)

    assert k["available"] and k["knee_rpm"] == 4000.0 and not k["knee_is_lower_bound"] and k["first_failing_rpm"] == 8000.0
    assert k["unloaded_p95_ms"] == 600.0


def test_the_keep_alive_knee_is_only_a_lower_bound_if_nothing_failed() -> None:
    cells = [step("keep_alive", 1, 400.0, 600.0), step("keep_alive", 1, 8000.0, 700.0)]

    k = keep_alive_knee(cells)

    assert k["knee_rpm"] == 8000.0 and k["knee_is_lower_bound"] is True


def test_the_keep_alive_knee_ignores_other_modes_and_the_two_instance_steps() -> None:
    cells = [
        step("keep_alive", 1, 400.0, 600.0), step("keep_alive", 1, 4000.0, 5000.0),  # fails at 4000
        step("keep_alive", 2, 100.0, 50.0), step("per_request", 1, 100.0, 50.0),  # irrelevant: would become the baseline
        step("keep_alive", 2, 8000.0, 100.0), step("per_request", 1, 8000.0, 100.0),
    ]

    k = keep_alive_knee(cells)

    assert k["knee_rpm"] == 400.0 and k["first_failing_rpm"] == 4000.0 and k["unloaded_p95_ms"] == 600.0


def test_the_keep_alive_knee_is_unavailable_without_a_baseline_and_a_second_step() -> None:
    assert keep_alive_knee([])["available"] is False
    assert keep_alive_knee([step("keep_alive", 1, 400.0, 600.0)])["available"] is False
    unhealthy_baseline = [step("keep_alive", 1, 400.0, 600.0, errors=500), step("keep_alive", 1, 4000.0, 700.0)]
    k = keep_alive_knee(unhealthy_baseline)
    assert k["available"] is False and "baseline" in k["reason"]
