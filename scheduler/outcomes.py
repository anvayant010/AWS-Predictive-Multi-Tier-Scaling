"""Attach what actually happened to each logged decision (Phase 6e).

A decision is made BEFORE its outcome exists, so the outcome fields are filled in afterwards, once the real
traffic over the decision's lookahead window is known. In a replay the whole series is already available,
so it can be done straight away; in a live run it would be done later, from real metrics, by the same
function.

Grading uses the shared definition of "needed" (decision_engine.grading), identical to the Phase 5c replay,
so numbers stay comparable. It grades the capacity the decision REQUESTED (`desired`), whether or not it was
actually applied: a decision blocked by the kill switch or taken in dry-run is still a decision worth grading.

If the full lookahead window is not (yet) known, the outcome fields are left empty. They are never guessed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

import pandas as pd

from decision_engine.engine import ROUTES, Route
from decision_engine.grading import LAMBDA_TRUTH_THRESHOLD, needed_capacity
from scheduler.records import STATUS_ERROR, DecisionRecord


def _lookahead_and_floor(route: Route) -> tuple[int, int]:
    if route.ec2 is not None:
        return route.ec2.lookahead_minutes, route.ec2.min_instances
    assert route.lambda_ is not None
    return route.lambda_.lookahead_minutes, route.lambda_.min_concurrency


def _as_naive_utc(moment) -> pd.Timestamp:
    stamp = pd.Timestamp(moment)
    return stamp.tz_convert("UTC").tz_localize(None) if stamp.tzinfo is not None else stamp


def attach_outcomes(
    records: list[DecisionRecord],
    series_by_service: Mapping[str, pd.Series],
    routes: Mapping[str, Route] = ROUTES,
    truth_threshold: float = LAMBDA_TRUTH_THRESHOLD,
) -> list[DecisionRecord]:
    """Return the records with actual_peak / needed / shortfall filled in wherever the outcome is known."""
    graded: list[DecisionRecord] = []
    for record in records:
        series = series_by_service.get(record.service)
        if record.actuation_status == STATUS_ERROR or series is None:
            graded.append(record)  # nothing was decided, or no data for this service: nothing to grade
            continue

        route = routes[record.service]
        steps, _floor = _lookahead_and_floor(route)
        first = _as_naive_utc(record.time) + pd.Timedelta(minutes=1)
        window = series.reindex(pd.date_range(first, periods=steps, freq="min"))
        if window.isna().any():
            graded.append(record)  # the window has not fully happened (or is missing): leave it unknown
            continue

        peak = float(window.max())
        needed = needed_capacity(route, peak, truth_threshold=truth_threshold)
        graded.append(replace(record, actual_peak=peak, needed=needed, shortfall=record.desired - needed))
    return graded


def summarize_outcomes(records: list[DecisionRecord], routes: Mapping[str, Route] = ROUTES) -> dict[str, dict[str, float]]:
    """Per service: how many decisions were graded, and how often they were short / over / exactly right."""
    out: dict[str, dict[str, float]] = {}
    for service in sorted({r.service for r in records}):
        mine = [r for r in records if r.service == service]
        graded = [r for r in mine if r.shortfall is not None]
        _steps, floor = _lookahead_and_floor(routes[service])
        needing = [r for r in graded if r.needed is not None and r.needed > floor]
        n = len(graded)
        out[service] = {
            "decisions": len(mine),
            "graded": n,
            "ungraded": len(mine) - n,
            "under_rate": sum(r.shortfall < 0 for r in graded) / n if n else float("nan"),
            "over_rate": sum(r.shortfall > 0 for r in graded) / n if n else float("nan"),
            "exact_rate": sum(r.shortfall == 0 for r in graded) / n if n else float("nan"),
            "under_when_needed": sum(r.shortfall < 0 for r in needing) / len(needing) if needing else float("nan"),
            "mean_shortfall": sum(r.shortfall for r in graded) / n if n else float("nan"),
        }
    return out


def format_outcome_summary(summary: dict[str, dict[str, float]]) -> str:
    lines = [f"  {'service':<19}{'graded':>8}{'under':>8}{'over':>8}{'exact':>8}{'under|needed':>14}{'mean short':>12}"]
    for service, s in summary.items():
        lines.append(
            f"  {service:<19}{int(s['graded']):>8}{s['under_rate']:>8.1%}{s['over_rate']:>8.1%}{s['exact_rate']:>8.1%}"
            f"{s['under_when_needed']:>14.1%}{s['mean_shortfall']:>12.2f}"
            + (f"   ({int(s['ungraded'])} ungraded)" if s["ungraded"] else "")
        )
    return "\n".join(lines)
