"""Phase 7 connection re-test: what limited the first calibration's "knee"? (pure analysis, no AWS, no network)

The first calibration found a knee of 3200 rpm, but its failing steps were not slow because the instance
queued work: the median stayed flat (~527 ms from 200 to 5000 rpm) and CPU peaked at 27%. They failed
because a growing share of requests took ~3.5 s, i.e. a 3 s TCP connection retry plus the normal round trip.
That driver opened a NEW connection for every request; the real replay (Locust) reuses connections. So the
knee may describe connection set-up (laptop ports, router/NAT, or the load balancer's new-connection rate),
not the instance.

This module holds the rule that reads the re-test, written down BEFORE the run:

* SLOW REQUEST: failed, or took longer than SLOW_MS (2.5 s). `slow_share` is the share of all requests.
* REFERENCE: the first run's configuration (new connection per request, 1 instance, 5000 rpm), re-measured in
  the same session so day-to-day network differences cancel out.
* "MUCH LOWER": the slow share is at most half the reference AND at least 1 percentage point lower.
* Two questions are asked of the reference: does REUSING CONNECTIONS (1 instance, 5000 rpm) fix it, and does a
  SECOND INSTANCE (new connections, 5000 rpm) fix it?
    - only reuse helps      -> "connection_setup": the old knee was a connection-set-up limit, not the instance
    - only a 2nd instance   -> "instance_side": the instance (or what sits in front of it per instance) is the limit
    - both help             -> "both"
    - neither helps         -> "neither": the limit is elsewhere (laptop link, ALB...) and needs more work
* If the reference itself shows a slow share below 2%, the original problem did not reappear and nothing can be
  concluded from this session ("not_reproduced").

Nothing here decides the per-instance capacity used by the comparison: it tells us whether the old number can
be trusted, and gives the keep-alive knee (same P95 rule as the first calibration) for the next decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from comparison.calibration import LoadStep, find_knee, summarize_step

SLOW_MS = 2500.0
MODES = ("per_request", "keep_alive")
REFERENCE = ("per_request", 1, 5000.0)
NOT_REPRODUCED_BELOW = 0.02
MUCH_LOWER_FACTOR = 0.5
MIN_ABSOLUTE_DROP = 0.01
MIN_FIDELITY = 0.95
MAX_INSTANCES = 2


@dataclass(frozen=True)
class RecheckCell:
    """One block of the re-test: a connection mode and an instance count, measured at several rates."""

    mode: str
    instances: int
    rates: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if not 1 <= self.instances <= MAX_INSTANCES:
            raise ValueError(f"instances must be between 1 and {MAX_INSTANCES}")
        if not self.rates or any(r <= 0 for r in self.rates):
            raise ValueError("rates must be a non-empty list of positive numbers")


DEFAULT_PLAN = (
    RecheckCell("keep_alive", 1, (400.0, 4000.0, 5000.0, 6500.0, 8000.0)),
    RecheckCell("per_request", 1, (5000.0,)),
    RecheckCell("keep_alive", 2, (5000.0, 6500.0, 8000.0)),
    RecheckCell("per_request", 2, (5000.0,)),
)


# KNEE HUNT (second session, after the first re-test showed the knee is NOT connection-limited: reused connections
# stayed flat up to 8000 rpm on one instance). One instance, reused connections only, rising rates. The rule is
# fixed BEFORE the run: the knee is the first calibration's rule (P95 <= 2x the unloaded P95 measured in THIS
# session at the lowest step, errors < 1%, laptop delivers >= 95% of the offered load). If the top step still passes
# the knee is only a lower bound. If the laptop stops delivering first, the result is "generator limited": the
# instance capacity is then at least the last passing step and the laptop is the ceiling for any real run.
KNEE_PLAN = (RecheckCell("keep_alive", 1, (400.0, 8000.0, 10000.0, 12000.0, 16000.0, 20000.0, 25000.0)),)

PLANS = {"default": DEFAULT_PLAN, "knee": KNEE_PLAN}


def slow_share(rows: list[dict], threshold_ms: float = SLOW_MS) -> float | None:
    """Share of ALL requests that failed or took longer than `threshold_ms`; None if there were no requests."""
    if not rows:
        return None
    return sum(1 for r in rows if (not r["ok"]) or r["ms"] > threshold_ms) / len(rows)


def cell_record(mode: str, instances: int, rows: list[dict], offered_rpm: float, measure_s: float, *, connects_per_request: float | None) -> dict:
    """The measured rows of one step -> one table row (the LoadStep fields plus the slow share)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    step = summarize_step(rows, offered_rpm, measure_s)
    return {
        "mode": mode, "instances": instances, **asdict(step),
        "error_rate": step.error_rate, "achieved_rpm": step.achieved_rpm, "fidelity": step.fidelity,
        "slow_share": slow_share(rows), "connects_per_request": connects_per_request,
    }


def _find(cells: list[dict], mode: str, instances: int, rpm: float) -> dict | None:
    for c in cells:
        if c["mode"] == mode and c["instances"] == instances and c["offered_rpm"] == rpm and c.get("slow_share") is not None:
            return c
    return None


def much_lower(reference_share: float, other_share: float) -> bool:
    return other_share <= MUCH_LOWER_FACTOR * reference_share and reference_share - other_share >= MIN_ABSOLUTE_DROP


def recheck_verdict(cells: list[dict], reference: tuple[str, int, float] = REFERENCE) -> dict:
    """Apply the pre-declared rule (see module docstring). Always returns a conclusion code and a plain-words explanation."""
    mode, instances, rpm = reference
    ref = _find(cells, mode, instances, rpm)
    if ref is None:
        return _verdict("incomplete", f"the reference step ({mode}, {instances} instance, {rpm:g} rpm) was not measured", None, None, None)
    if ref["fidelity"] < MIN_FIDELITY:
        return _verdict("incomplete", f"the laptop did not deliver the reference load (fidelity {ref['fidelity']:.2f})", ref, None, None)
    if ref["slow_share"] < NOT_REPRODUCED_BELOW:
        return _verdict(
            "not_reproduced",
            f"the original problem did not reappear (slow share {ref['slow_share']:.1%} < {NOT_REPRODUCED_BELOW:.0%}), so this session cannot say what caused it",
            ref, None, None,
        )

    keep = _find(cells, "keep_alive", instances, rpm)
    second = _find(cells, mode, instances + 1, rpm)
    if keep is None or second is None:
        missing = [name for name, cell in (("reused-connection", keep), ("second-instance", second)) if cell is None]
        return _verdict("incomplete", "the " + " and ".join(missing) + " comparison step was not measured", ref, keep, second)

    reuse_helps = much_lower(ref["slow_share"], keep["slow_share"])
    second_helps = much_lower(ref["slow_share"], second["slow_share"])
    code = {(True, False): "connection_setup", (False, True): "instance_side", (True, True): "both", (False, False): "neither"}[(reuse_helps, second_helps)]
    return _verdict(code, _EXPLANATIONS[code], ref, keep, second)


_EXPLANATIONS = {
    "connection_setup": "reusing connections removed most of the slow requests but a second instance did not: the old knee was a "
    "connection-set-up limit (laptop ports, router/NAT or the load balancer), not the instance. The real replay reuses connections.",
    "instance_side": "a second instance removed most of the slow requests but reusing connections did not: the limit scales with "
    "instances, so the old per-instance number is plausible.",
    "both": "both reusing connections and adding an instance removed most of the slow requests: set-up cost and instance load both matter.",
    "neither": "neither reusing connections nor a second instance fixed it: the limit is somewhere else (laptop link, load balancer) "
    "and the old number cannot be trusted as instance capacity.",
}


def _verdict(code: str, explanation: str, ref: dict | None, keep: dict | None, second: dict | None) -> dict:
    return {
        "conclusion": code, "explanation": explanation,
        "reference_slow_share": None if ref is None else ref["slow_share"],
        "reused_connection_slow_share": None if keep is None else keep["slow_share"],
        "second_instance_slow_share": None if second is None else second["slow_share"],
        "rule": {"slow_ms": SLOW_MS, "much_lower_factor": MUCH_LOWER_FACTOR, "min_absolute_drop": MIN_ABSOLUTE_DROP,
                 "not_reproduced_below": NOT_REPRODUCED_BELOW},
    }


def keep_alive_knee(cells: list[dict]) -> dict:
    """The first calibration's P95 knee rule applied to the reused-connection, one-instance steps.

    The lowest such step is the unloaded baseline. With only a few widely spaced rates this is a coarse bracket
    ("at least X, failed at Y"), and it can say "fine" while a few percent of requests are slow: read it
    together with the slow share in the table.
    """
    steps = [
        LoadStep(c["offered_rpm"], c["sent"], c["errors"], c["p50_ms"], c["p95_ms"], c["p99_ms"], c["duration_s"])
        for c in cells if c["mode"] == "keep_alive" and c["instances"] == 1
    ]
    if len(steps) < 2:
        return {"available": False, "reason": "fewer than two reused-connection steps on one instance"}
    try:
        knee = find_knee(steps)
    except ValueError as exc:
        return {"available": False, "reason": str(exc)}
    return {
        "available": True, "knee_rpm": knee["knee_rpm"], "knee_is_lower_bound": knee["knee_is_lower_bound"],
        "first_failing_rpm": knee["first_failing_rpm"], "first_failing_reason": knee["first_failing_reason"],
        "unloaded_p95_ms": knee["unloaded_p95_ms"], "generator_limited": knee["generator_limited"],
    }
