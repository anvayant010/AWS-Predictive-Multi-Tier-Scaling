"""Pure pacing logic for replaying a Phase 7 scenario (no Locust, AWS or network imports).

Why not just "N users in a loop". Locust users are CLOSED-loop: a user waits for its reply before sending
the next request, so when a server slows down the offered load quietly drops and the latency damage is
under-reported. A comparison between scaling arms must offer the SAME load whatever the server does.
Pacing here is therefore open-loop, by thinning: every active user wakes once a second and sends a request
with probability rate / active_users. The users' wake-ups are spread evenly over the second (a random start
offset per user), so the superposition is close to a Poisson arrival process at the offered rate. It also avoids the artefact of
many users firing at the same instant (which would invent cold starts that real traffic would not cause).

Time is NOT compressed in a real run: scenario minute k is played during real minute k (decision D4: the AWS
delays are fixed). `speedup` exists only so tests can replay a tiny scenario in a few seconds.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Keep the per-user firing probability at or below this, so a user finishing a slow request never makes
# the offered rate fall short.
MAX_FIRE_PROBABILITY = 0.25
MIN_USERS = 20


@dataclass(frozen=True)
class RatePlan:
    """Offered requests per minute for each scenario minute, in REAL units (already multiplied by load_scale)."""

    rpm: np.ndarray
    tail_seconds: float = 0.0

    def __post_init__(self) -> None:
        if len(self.rpm) < 1:
            raise ValueError("a rate plan needs at least one minute")
        if (np.asarray(self.rpm) < 0).any() or not np.isfinite(self.rpm).all():
            raise ValueError("offered rates must be finite and non-negative")
        if self.tail_seconds < 0:
            raise ValueError("tail_seconds cannot be negative")

    @property
    def minutes(self) -> int:
        return len(self.rpm)

    @property
    def max_rps(self) -> float:
        return float(np.max(self.rpm)) / 60.0

    def duration_seconds(self, speedup: float = 1.0) -> float:
        return (self.minutes * 60.0) / speedup + self.tail_seconds

    def minute_at(self, elapsed_seconds: float, speedup: float = 1.0) -> int | None:
        """Scenario minute being played at `elapsed_seconds`, or None once the run is over.

        During the tail (after the last minute) the load stays at the last minute's level.
        """
        if elapsed_seconds < 0:
            raise ValueError("elapsed time cannot be negative")
        if elapsed_seconds >= self.duration_seconds(speedup):
            return None
        return min(int(elapsed_seconds * speedup // 60), self.minutes - 1)

    def rpm_at(self, elapsed_seconds: float, speedup: float = 1.0) -> float | None:
        minute = self.minute_at(elapsed_seconds, speedup)
        return None if minute is None else float(self.rpm[minute])


def plan_from_scenario(scenario, tail_seconds: float = 0.0) -> RatePlan:
    """Build a plan from a comparison.scenario.ServiceScenario (uses its load_scale)."""
    return RatePlan(rpm=scenario.offered_rpm.to_numpy(dtype="float64"), tail_seconds=tail_seconds)


def users_needed(plan: RatePlan) -> int:
    """Smallest user pool that keeps every user's firing probability within MAX_FIRE_PROBABILITY."""
    return max(MIN_USERS, math.ceil(plan.max_rps / MAX_FIRE_PROBABILITY))


def fire_probability(rpm: float, active_users: int) -> float:
    """Chance that ONE user sends a request in a given one-second slice."""
    if active_users <= 0:
        return 0.0
    return min(1.0, (rpm / 60.0) / active_users)


def should_fire(rpm: float, active_users: int, rng: random.Random) -> bool:
    return rng.random() < fire_probability(rpm, active_users)


# --------------------------------------------------------------------------------------- response markers


@dataclass(frozen=True)
class Marker:
    cold_start: bool | None
    container_id: str | None
    init_type: str | None


NO_MARKER = Marker(None, None, None)


def parse_marker(body: str | None) -> Marker:
    """Read the cold-start marker the thumbnail handler adds to every response (see Phase 7 step 1)."""
    if not body:
        return NO_MARKER
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return NO_MARKER
    if not isinstance(data, dict) or "cold_start" not in data:
        return NO_MARKER
    cold = data.get("cold_start")
    return Marker(
        cold_start=cold if isinstance(cold, bool) else None,
        container_id=data.get("container_id") if isinstance(data.get("container_id"), str) else None,
        init_type=data.get("init_type") if isinstance(data.get("init_type"), str) else None,
    )


# ----------------------------------------------------------------------------------------- request log


class RequestLog:
    """Append-only JSON-lines file of every request: the raw material the results collector analyses."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8", newline="\n")

    def write(self, row: dict) -> None:
        self._file.write(json.dumps(row, separators=(",", ":")) + "\n")

    def close(self) -> None:
        self._file.flush()
        self._file.close()


def read_request_log(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows
