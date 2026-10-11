"""The predict -> decide -> actuate loop (Phase 6d).

One cycle per call to `run_cycle`. Everything it needs is passed in (clock, metrics source,
forecasters, AWS clients, state), so it has no hidden global state and is testable without real time
or real AWS.

Three independent layers keep it from changing anything by accident:
  1. DRY_RUN (default TRUE). Only the literal text "false" turns it off; a typo stays safe.
  2. The kill switch, read once at the start of EVERY cycle, failing closed (see decision_engine.kill_switch).
  3. Offline mode (no AWS clients): decisions are logged and nothing can be actuated. Real actuation
     requires clients, and refuses to start without them.
Real scaling happens only when DRY_RUN=false AND the kill switch is RELEASED AND clients are present.

Where the metrics come from: for now a ReplaySource (history truncated at "now", so the forecaster can
never see the future). A live CloudWatch source is deliberately not built yet: the GBM needs about a week
of history that a freshly deployed stack does not have.

Run (offline, dry-run, replaying the local DB):
    DRY_RUN=true python -m scheduler.loop_handler --ticks 60
Add --aws to also read the kill switch and validate against the real ASG/function (still dry-run unless
DRY_RUN=false AND the kill switch is released).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Mapping, Protocol

import pandas as pd

from decision_engine.actuation import ActuationResult
from decision_engine.actuator_ec2 import apply_ec2_decision, current_desired_capacity
from decision_engine.actuator_lambda import apply_lambda_decision, provisioned_status
from decision_engine.damping import DEFAULT_DAMPING, NO_DAMPING, DampingConfig, DampingState, damp
from decision_engine.engine import ROUTES, Route, decide
from decision_engine.kill_switch import DEFAULT_PARAMETER, KillSwitchStatus, read_kill_switch
from decision_engine.models import Aggressiveness, Forecast
from scheduler.outcomes import attach_outcomes, format_outcome_summary, summarize_outcomes
from scheduler.records import STATUS_BLOCKED, STATUS_ERROR, STATUS_OFFLINE, DecisionRecord

CADENCE_MINUTES = {"catalog_service": 5, "thumbnail_service": 1}


def parse_dry_run(value: str | None) -> bool:
    """True (dry-run) unless the value is exactly 'false'. Unset, empty or misspelled stays safe."""
    if value is None:
        return True
    return value.strip().lower() != "false"


@dataclass(frozen=True)
class LoopConfig:
    dry_run: bool = True
    aggressiveness: Aggressiveness = Aggressiveness.BALANCED
    asg_name: str = "predictive-scaling-catalog-asg"
    function_name: str = "predictive-scaling-thumbnail-service"
    alias: str = "live"
    kill_switch_parameter: str = DEFAULT_PARAMETER
    cadence_minutes: Mapping[str, int] = field(default_factory=lambda: dict(CADENCE_MINUTES))
    # Per-service damping of capacity releases (see decision_engine.damping). A service missing here is
    # not damped. Use NO_DAMPING to switch a service's damping off explicitly.
    damping: Mapping[str, DampingConfig] = field(default_factory=lambda: dict(DEFAULT_DAMPING))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LoopConfig":
        env = os.environ if env is None else env
        return cls(
            dry_run=parse_dry_run(env.get("DRY_RUN")),
            aggressiveness=Aggressiveness(env.get("AGGRESSIVENESS", Aggressiveness.BALANCED.value)),
            asg_name=env.get("CATALOG_ASG_NAME", cls.asg_name),
            function_name=env.get("THUMBNAIL_FUNCTION_NAME", cls.function_name),
            alias=env.get("THUMBNAIL_ALIAS", cls.alias),
            kill_switch_parameter=env.get("KILL_SWITCH_PARAMETER", DEFAULT_PARAMETER),
        )


@dataclass(frozen=True)
class Clients:
    """The AWS clients the loop may use. Absent (None) = offline mode: nothing can be actuated."""

    autoscaling: object
    lambda_: object
    ssm: object


@dataclass
class LoopState:
    """What the loop remembers between cycles when it cannot read capacity from AWS (offline mode)."""

    simulated: dict[str, int] = field(default_factory=dict)
    damping: dict[str, DampingState] = field(default_factory=dict)  # per-service hold / confirmation memory


class MetricsSource(Protocol):
    def history(self, service: str, now: pd.Timestamp) -> pd.Series: ...


class ReplaySource:
    """Serves recorded series truncated at `now`, so a replayed forecast can never see the future."""

    def __init__(self, series_by_service: Mapping[str, pd.Series]) -> None:
        self._series = dict(series_by_service)

    def history(self, service: str, now: pd.Timestamp) -> pd.Series:
        return self._series[service].loc[:now]


class Forecaster(Protocol):
    def forecast(self, series: pd.Series, origin: pd.Timestamp) -> Forecast: ...


def _is_due(service: str, now: pd.Timestamp, config: LoopConfig) -> bool:
    return now.minute % config.cadence_minutes[service] == 0


def _current_capacity(service: str, route: Route, config: LoopConfig, clients: Clients | None, state: LoopState) -> int:
    if clients is None:
        floor = route.ec2.min_instances if route.ec2 else route.lambda_.min_concurrency
        return state.simulated.get(service, floor)
    if route.ec2 is not None:
        return current_desired_capacity(clients.autoscaling, config.asg_name)
    status = provisioned_status(clients.lambda_, config.function_name, config.alias)
    return status.requested if status else 0


def _capacity_limits(route: Route) -> tuple[int, int]:
    if route.ec2 is not None:
        return route.ec2.min_instances, route.ec2.max_instances
    return route.lambda_.min_concurrency, route.lambda_.max_concurrency


def _actuate(route: Route, scaling, config: LoopConfig, clients: Clients, *, dry_run: bool) -> ActuationResult:
    if route.ec2 is not None:
        return apply_ec2_decision(clients.autoscaling, config.asg_name, scaling, route.ec2, dry_run=dry_run)
    return apply_lambda_decision(clients.lambda_, config.function_name, config.alias, scaling, route.lambda_, dry_run=dry_run)


def run_cycle(
    now: pd.Timestamp,
    config: LoopConfig,
    source: MetricsSource,
    forecasters: Mapping[str, Forecaster],
    clients: Clients | None,
    state: LoopState,
    routes: Mapping[str, Route] = ROUTES,
) -> list[DecisionRecord]:
    """One predict -> decide -> actuate pass over every service that is due at `now`."""
    if not config.dry_run and clients is None:
        raise ValueError("DRY_RUN=false requires AWS clients; offline mode can never actuate")

    # Layer 2: the kill switch, read fresh at the start of every cycle (before anything else).
    kill: KillSwitchStatus | None = None
    if clients is not None:
        kill = read_kill_switch(clients.ssm, config.kill_switch_parameter)

    mode = "offline" if clients is None else "aws"
    records: list[DecisionRecord] = []
    for service, forecaster in forecasters.items():
        if not _is_due(service, now, config):
            continue
        route = routes[service]
        try:
            forecast = forecaster.forecast(source.history(service, now), now)
            current = _current_capacity(service, route, config, clients, state)
            decision = decide(forecast, current, config.aggressiveness, routes)
            scaling = decision.scaling

            # Damping sits between the policy and the actuator: it can delay a release, never a cap.
            low, high = _capacity_limits(route)
            damped = damp(
                scaling.desired, current, now.to_pydatetime(),
                state.damping.setdefault(service, DampingState()),
                config.damping.get(service, NO_DAMPING), min_capacity=low, max_capacity=high,
            )
            if damped.desired != scaling.desired:
                scaling = replace(scaling, desired=damped.desired, reason=f"{scaling.reason} | damping: {damped.note}")

            if clients is None:
                status, detail = STATUS_OFFLINE, "no AWS clients: decision logged only"
            else:
                allowed = (not config.dry_run) and not kill.engaged
                result = _actuate(route, scaling, config, clients, dry_run=not allowed)
                status, detail = result.status, result.detail
                if not config.dry_run and kill.engaged:
                    status, detail = STATUS_BLOCKED, f"{kill.reason}; {detail}"
            state.simulated[service] = scaling.desired

            records.append(
                DecisionRecord(
                    time=now.to_pydatetime(), service=service, mode=mode, dry_run=config.dry_run,
                    kill_engaged=None if kill is None else kill.engaged,
                    kill_reason="" if kill is None else kill.reason,
                    shape=decision.classification.shape.value, forecast_demand=scaling.demand,
                    current=current, proposed=damped.proposed, desired=scaling.desired,
                    uncapped=scaling.uncapped, cap_applied=scaling.cap_applied,
                    actuation_status=status, actuation_detail=detail, reason=scaling.reason, damping=damped.note,
                )
            )
        except Exception as exc:  # one service failing must not stop the other; it is recorded, not hidden
            records.append(
                DecisionRecord(
                    time=now.to_pydatetime(), service=service, mode=mode, dry_run=config.dry_run,
                    kill_engaged=None if kill is None else kill.engaged,
                    kill_reason="" if kill is None else kill.reason,
                    shape="", forecast_demand=0.0, current=0, proposed=0, desired=0, uncapped=0, cap_applied=False,
                    actuation_status=STATUS_ERROR, actuation_detail="", reason="",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return records


def append_records(path: Path, records: list[DecisionRecord]) -> None:
    """Append one JSON object per line (JSONL), creating the folder if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), default=lambda o: o.isoformat()) + "\n")


def run_replay(
    start: pd.Timestamp,
    ticks: int,
    config: LoopConfig,
    source: MetricsSource,
    forecasters: Mapping[str, Forecaster],
    clients: Clients | None,
    log_path: Path | None = None,
    sleep_seconds: float = 0.0,
    outcome_series: Mapping[str, pd.Series] | None = None,
) -> list[DecisionRecord]:
    """Run `ticks` consecutive one-minute cycles starting at `start`.

    With `outcome_series` (the full recorded history, which in a replay already contains the "future"),
    each record is graded against what really happened (scheduler.outcomes) before it is logged. Without
    it the outcome fields stay empty.
    """
    state, everything = LoopState(), []
    for i in range(ticks):
        records = run_cycle(start + pd.Timedelta(minutes=i), config, source, forecasters, clients, state)
        if outcome_series is not None:
            records = attach_outcomes(records, outcome_series)
        if log_path is not None:
            append_records(log_path, records)
        everything += records
        if sleep_seconds:
            time.sleep(sleep_seconds)
    return everything


def _summary(records: list[DecisionRecord]) -> str:
    by_status: dict[str, int] = {}
    for r in records:
        by_status[r.actuation_status] = by_status.get(r.actuation_status, 0) + 1
    errors = [r for r in records if r.actuation_status == STATUS_ERROR]
    lines = [f"{len(records)} decisions: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items()))]
    lines += [f"  ERROR {r.service} @ {r.time:%Y-%m-%d %H:%M}: {r.error}" for r in errors[:5]]
    return "\n".join(lines)


def select_services(available: Mapping[str, object], names: str) -> list[str]:
    """Which services to run: all of them when `names` is empty, else exactly the comma-separated ones.

    Lets a real-AWS test touch ONE service at a time. An unknown name is a loud error, never ignored.
    """
    if not names.strip():
        return list(available)
    chosen = [n.strip() for n in names.split(",") if n.strip()]
    unknown = [n for n in chosen if n not in available]
    if unknown or not chosen:
        raise ValueError(f"unknown service(s) {unknown or names!r}; choose from {sorted(available)}")
    return [n for n in available if n in chosen]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the predictive-scaling loop over replayed history.")
    parser.add_argument("--ticks", type=int, default=60, help="number of one-minute cycles to run")
    parser.add_argument("--holdout-days", type=int, default=7, help="replay starts this many days before the data ends")
    parser.add_argument("--aws", action="store_true", help="use real AWS clients (kill switch + actuator validation)")
    parser.add_argument("--sleep-seconds", type=float, default=0.0, help="pause between cycles (0 = as fast as possible)")
    parser.add_argument("--log", default="loop_output/decisions.jsonl", help="JSONL decision log")
    parser.add_argument("--services", default="", help="comma-separated services to run (default: all)")
    parser.add_argument("--start-offset-minutes", type=int, default=0, help="begin this many minutes after the replay start")
    args = parser.parse_args(argv)

    config = LoopConfig.from_env()
    # Imported here so importing this module (and the tests) needs no DB or model stack.
    from forecasting.data_pipeline.db import get_connection
    from forecasting.predictor import GbmForecaster
    from forecasting.train import TARGET_SERIES, load_series, split_train_holdout

    conn = get_connection()
    series = {svc: load_series(conn, svc, metric) for svc, metric in TARGET_SERIES.items()}
    first = next(iter(series.values()))
    train, _ = split_train_holdout(first, args.holdout_days)
    start = train.index.max() + pd.Timedelta(minutes=1 + args.start_offset_minutes)
    selected = select_services(series, args.services)
    forecasters = {svc: GbmForecaster.fit(svc, series[svc], train.index.max()) for svc in selected}

    clients = None
    if args.aws:
        import boto3

        session = boto3.Session(
            profile_name=os.environ.get("AWS_PROFILE", "predictive-scaling"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        clients = Clients(session.client("autoscaling"), session.client("lambda"), session.client("ssm"))

    print(f"dry_run={config.dry_run} mode={'aws' if clients else 'offline'} start={start} ticks={args.ticks}")
    records = run_replay(start, args.ticks, config, ReplaySource(series), forecasters, clients,
                         Path(args.log), args.sleep_seconds, outcome_series=series)
    print(_summary(records))
    print("\nOUTCOMES (each decision graded against what the real traffic then needed):")
    print(format_outcome_summary(summarize_outcomes(records)))
    return 1 if any(r.actuation_status == STATUS_ERROR for r in records) else 0


if __name__ == "__main__":
    sys.exit(main())
