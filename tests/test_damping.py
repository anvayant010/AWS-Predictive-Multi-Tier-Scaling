import random
from datetime import datetime, timedelta

import pytest

from decision_engine.damping import (
    DEFAULT_DAMPING,
    NO_DAMPING,
    DampingConfig,
    DampingState,
    damp,
)

T0 = datetime(2026, 6, 1, 12, 0)
CAPS = {"min_capacity": 0, "max_capacity": 10}


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def step(state, proposed, current, minutes, config, **caps):
    return damp(proposed, current, at(minutes), state, config, **(caps or CAPS))


# --- increases and no-ops -----------------------------------------------------------------------


def test_increases_pass_through_immediately_and_start_the_hold() -> None:
    state, config = DampingState(), DampingConfig(hold_minutes=10)

    result = step(state, proposed=3, current=0, minutes=0, config=config)

    assert (result.desired, result.held) == (3, False)
    assert state.last_increase == at(0)


def test_an_unchanged_target_is_a_noop() -> None:
    result = step(DampingState(), 2, 2, 0, DampingConfig())

    assert result.desired == 2 and not result.held


# --- the hold --------------------------------------------------------------------------------------


def test_a_decrease_is_held_until_the_hold_has_passed() -> None:
    state, config = DampingState(), DampingConfig(hold_minutes=10)
    step(state, 3, 0, 0, config)  # raised at t=0

    early = step(state, 0, 3, 4, config)
    late = step(state, 0, 3, 10, config)

    assert (early.desired, early.held) == (3, True) and "holding" in early.note
    assert (late.desired, late.held) == (0, False)


def test_the_hold_restarts_on_every_increase() -> None:
    state, config = DampingState(), DampingConfig(hold_minutes=10)
    step(state, 2, 0, 0, config)
    step(state, 4, 2, 8, config)  # increased again at t=8

    result = step(state, 0, 4, 12, config)  # 12 min after the first rise, but only 4 after the second

    assert result.desired == 4 and result.held


def test_a_decrease_with_no_prior_increase_is_not_held() -> None:
    # e.g. the loop starts while capacity is already high: there is no recent rise to protect
    result = step(DampingState(), 1, 5, 0, DampingConfig(hold_minutes=10, down_confirmations=1))

    assert result.desired == 1


# --- confirmations ---------------------------------------------------------------------------------


def test_a_decrease_needs_consecutive_confirmations() -> None:
    state, config = DampingState(), DampingConfig(hold_minutes=0, down_confirmations=3)

    first = step(state, 2, 6, 0, config)
    second = step(state, 2, 6, 5, config)
    third = step(state, 2, 6, 10, config)

    assert [r.desired for r in (first, second, third)] == [6, 6, 2]
    assert "1 of 3" in first.note and "2 of 3" in second.note


def test_a_non_decrease_resets_the_streak() -> None:
    state, config = DampingState(), DampingConfig(hold_minutes=0, down_confirmations=3)
    step(state, 2, 6, 0, config)
    step(state, 2, 6, 5, config)
    step(state, 6, 6, 10, config)  # back to "no change" breaks the streak

    after = step(state, 2, 6, 15, config)

    assert after.desired == 6 and "1 of 3" in after.note  # counting starts over


def test_the_release_goes_only_to_the_highest_target_proposed_in_the_streak() -> None:
    state, config = DampingState(), DampingConfig(hold_minutes=0, down_confirmations=3)
    step(state, 1, 8, 0, config)
    step(state, 4, 8, 5, config)
    result = step(state, 2, 8, 10, config)

    assert result.desired == 4  # not 1 or 2: never drop further than the evidence supports


def test_confirmations_are_not_counted_during_the_hold() -> None:
    state, config = DampingState(), DampingConfig(hold_minutes=10, down_confirmations=2)
    step(state, 5, 0, 0, config)
    step(state, 1, 5, 3, config)  # held, must not count toward the streak
    step(state, 1, 5, 6, config)
    after_hold = step(state, 1, 5, 11, config)

    assert after_hold.desired == 5 and "1 of 2" in after_hold.note


# --- caps beat damping -----------------------------------------------------------------------------


def test_a_current_capacity_above_the_cap_is_forced_down_even_during_the_hold() -> None:
    state, config = DampingState(), DampingConfig(hold_minutes=60, down_confirmations=5)
    step(state, 9, 0, 0, config, min_capacity=0, max_capacity=10)

    result = step(state, 9, 9, 1, config, min_capacity=0, max_capacity=4)  # cap lowered to 4

    assert result.desired == 4 and result.held


def test_the_release_never_goes_below_the_minimum() -> None:
    state, config = DampingState(), DampingConfig(hold_minutes=0, down_confirmations=1)

    result = step(state, 0, 4, 0, config, min_capacity=2, max_capacity=10)

    assert result.desired == 2


# --- invariants ------------------------------------------------------------------------------------


def test_no_damping_is_exactly_the_identity() -> None:
    rng = random.Random(3)
    state = DampingState()
    current = 0
    for minute in range(500):
        proposed = rng.randint(0, 10)
        result = damp(proposed, current, at(minute), state, NO_DAMPING, **CAPS)
        assert result.desired == proposed and not result.held
        current = result.desired


def test_damped_output_always_stays_within_the_caps_and_between_current_and_proposed() -> None:
    rng = random.Random(8)
    for seed in range(60):
        config = DampingConfig(hold_minutes=rng.randint(0, 30), down_confirmations=rng.randint(1, 4))
        state, current = DampingState(), rng.randint(0, 10)
        for minute in range(200):
            proposed = rng.randint(0, 10)
            result = damp(proposed, current, at(minute), state, config, **CAPS)
            assert 0 <= result.desired <= 10
            assert min(current, proposed) <= result.desired <= max(current, proposed)
            current = result.desired


def test_damping_never_increases_flapping_on_an_oscillating_proposal() -> None:
    config = DampingConfig(hold_minutes=10, down_confirmations=1)
    undamped_changes = damped_changes = 0
    state, current, previous_raw = DampingState(), 0, 0
    for minute in range(120):
        proposed = 2 if minute % 2 == 0 else 0  # flips every minute
        undamped_changes += proposed != previous_raw
        previous_raw = proposed
        result = damp(proposed, current, at(minute), state, config, **CAPS)
        damped_changes += result.desired != current
        current = result.desired

    assert damped_changes < undamped_changes / 4


# --- configuration -----------------------------------------------------------------------------------


@pytest.mark.parametrize("kwargs", [{"hold_minutes": -1}, {"down_confirmations": 0}])
def test_invalid_damping_configuration_is_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        DampingConfig(**kwargs)


def test_every_routed_service_has_a_default_damping_config() -> None:
    from decision_engine.engine import ROUTES

    assert set(DEFAULT_DAMPING) == set(ROUTES)
