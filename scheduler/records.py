"""The loop's decision record and its status values (kept in a tiny module so both the loop and the
outcome grader can import them without importing each other)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

STATUS_OFFLINE = "offline"
STATUS_BLOCKED = "blocked_by_kill_switch"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class DecisionRecord:
    time: datetime
    service: str
    mode: str  # "aws" | "offline"
    dry_run: bool
    kill_engaged: bool | None  # None when not checked (offline)
    kill_reason: str
    shape: str
    forecast_demand: float
    current: int
    proposed: int  # what the policy asked for, before damping
    desired: int  # what was (or would be) requested after damping
    uncapped: int
    cap_applied: bool
    actuation_status: str  # applied | noop | dry_run | blocked_by_kill_switch | offline | error
    actuation_detail: str
    reason: str
    damping: str = ""  # why damping did or did not change the proposal
    error: str = ""
    # Filled in later by scheduler.outcomes, once the real traffic is known. None = not known (yet).
    actual_peak: float | None = None  # highest real demand over the same lookahead window as the decision
    needed: int | None = None  # capacity that real peak actually required
    shortfall: int | None = None  # desired - needed: NEGATIVE = under-provisioned, positive = over-provisioned
