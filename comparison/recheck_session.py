"""One supervised AWS session for the connection re-test: apply -> measure -> ALWAYS tear down -> analyse.

Same lifecycle guarantees as the first calibration session (comparison/calibration_session.py, whose teardown and
dependency bundle it reuses): nothing is applied unless the preflight is clean; once a stack may exist, the
teardown (remove leftover provisioned concurrency, destroy, verify) runs on every path, including a crash, Ctrl-C
and the time limit; raw data is saved as soon as the stage finishes.

What is measured and how it is read is in comparison/recheck.py and comparison/recheck_stages.py.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comparison import recheck as rc
from comparison import recheck_stages as rs
from comparison.calibration_session import SessionDeps, _teardown, _utc
from comparison.calibration_stages import fetch_asg_cpu
from comparison.cost import estimate_before_run

BOOT_MINUTES = 3.0  # asking for a second instance until it is healthy (measured: ~1.5 min) plus margin


@dataclass(frozen=True)
class RecheckConfig:
    out_dir: Path
    plan: tuple[rc.RecheckCell, ...] = rc.DEFAULT_PLAN
    max_minutes: float = 60.0
    settle_s: float = 30.0
    measure_s: float = 150.0
    setup_minutes: float = 12.0  # apply + health wait + destroy + verify

    def __post_init__(self) -> None:
        if self.max_minutes <= 0 or not self.plan or self.settle_s < 0 or self.measure_s <= 0 or self.setup_minutes < 0:
            raise ValueError("invalid re-test configuration")


def expected_minutes(cfg: RecheckConfig) -> dict[str, float]:
    steps = sum(len(b.rates) for b in cfg.plan)
    changes, current = 0, 1
    for block in cfg.plan:
        changes += block.instances != current
        current = block.instances
    measuring = steps * (cfg.settle_s + cfg.measure_s) / 60.0 + changes * BOOT_MINUTES
    return {"steps": steps, "measuring_minutes": measuring, "total_stack_minutes": measuring + cfg.setup_minutes}


def spend_estimate(cfg: RecheckConfig) -> dict:
    return estimate_before_run(
        scenario_minutes=cfg.max_minutes, setup_minutes=cfg.setup_minutes, max_instances=2, max_provisioned=0, expected_thumbnail_requests=0,
    )


def run_recheck(cfg: RecheckConfig, deps: SessionDeps) -> dict:
    """Run the re-test. Returns the result dict (also saved). Never raises for a stage failure."""
    result: dict = {"status": "not_started", "errors": {}, "problems": [], "teardown": None}
    problems = deps.preflight()
    if problems:
        result.update(status="aborted_preflight", problems=problems)
        deps.log("PREFLIGHT FAILED - nothing was applied: " + "; ".join(problems))
        return result

    raw: dict = {}
    names: dict = {}
    env = None
    deadline: threading.Timer | None = None
    started = time.monotonic()
    try:
        outputs = deps.apply()
        result["status"] = "applied"
        names = deps.outputs_to_names(outputs)
        env = deps.make_env(outputs)
        deps.wait_healthy(env)

        deadline = threading.Timer(cfg.max_minutes * 60.0, env.stop.set)
        deadline.daemon = True
        deadline.start()
        started_wall = env.wall()
        aws = deps.aws

        deps.log("stage recheck: start")
        try:
            raw["recheck"] = rs.run_connection_recheck(
                env, aws.autoscaling, aws.elbv2, names["asg_name"], names["target_group_arn"], cfg.plan,
                settle_s=cfg.settle_s, measure_s=cfg.measure_s,
            )
            deps.save("recheck", raw["recheck"])
            deps.log("stage recheck: done")
        except Exception as exc:  # noqa: BLE001  (the teardown must still run)
            result["errors"]["recheck"] = f"{type(exc).__name__}: {exc}"
            deps.log(f"stage recheck: FAILED {exc!r}")
        try:
            raw["cpu"] = fetch_asg_cpu(aws.cloudwatch, names["asg_name"], _utc(started_wall), _utc(env.wall()))
            deps.save("cpu", raw["cpu"])
        except Exception as exc:  # noqa: BLE001
            result["errors"]["cpu"] = f"{type(exc).__name__}: {exc}"
        result["status"] = "measured" if not env.stop.is_set() else "stopped_early"
    except BaseException as exc:  # noqa: BLE001  (including Ctrl-C: the stack must still be torn down)
        result["status"] = "crashed"
        result["errors"]["session"] = f"{type(exc).__name__}: {exc}"
        deps.log(f"SESSION ERROR: {exc!r}")
    finally:
        if deadline is not None:
            deadline.cancel()  # the stage runs in this thread, so nothing is in flight by the time the stack is destroyed
        result["teardown"] = _teardown(deps, names)
        result["minutes"] = (time.monotonic() - started) / 60.0

    if "recheck" in raw:
        result["analysis"] = analyze_recheck(raw, cfg.plan)
        deps.save("recheck_analysis", result["analysis"])
    deps.save("session", result)
    return result


def analyze_recheck(raw: dict, plan: tuple[rc.RecheckCell, ...] | None = None) -> dict:
    block = raw.get("recheck", {})
    cells = block.get("cells", [])
    cpu = raw.get("cpu", [])
    return {
        "cells": cells, "skipped": block.get("skipped", []),
        # the verdict compares reused vs new connections; a plan without a new-connection step (the knee hunt) has none
        "verdict": rc.recheck_verdict(cells) if plan is None or any(b.mode == "per_request" for b in plan) else None,
        "keep_alive_knee": rc.keep_alive_knee(cells),
        "cpu_max_percent": max((p["maximum"] for p in cpu), default=None),
        "generator_limited_steps": [(c["mode"], c["instances"], c["offered_rpm"]) for c in cells if c["fidelity"] < rc.MIN_FIDELITY],
    }


# --------------------------------------------------------------------------------------------- reporting


def format_plan(cfg: RecheckConfig) -> str:
    mins = expected_minutes(cfg)
    spend = spend_estimate(cfg)
    names = {"keep_alive": "reused connections", "per_request": "new connection per request"}
    lines = ["CONNECTION RE-TEST PLAN (nothing has been applied yet)", ""]
    for b in cfg.plan:
        lines.append(f"  {b.instances} instance(s), {names[b.mode]}: " + ", ".join(f"{r:g}" for r in b.rates) + " rpm")
    lines += [
        "",
        f"  {mins['steps']} steps of {cfg.settle_s:g}s settle + {cfg.measure_s:g}s measured; "
        + ("the catalog ASG goes 1 -> 2 once (never above 2)" if any(b.instances > 1 for b in cfg.plan) else "the catalog ASG stays at 1 instance"),
        f"  expected: measuring ~{mins['measuring_minutes']:.0f} min, stack up ~{mins['total_stack_minutes']:.0f} min",
        f"  HARD LIMIT: measuring is stopped after {cfg.max_minutes:g} min; teardown + verifier then run regardless",
        f"  spend: upper bound ~${spend['total_usd']:.2f} (prices unverified); realistic ~$0.05-0.15",
        "",
        "  Capacity is changed directly through the capped actuator; it does NOT go through the scaling loop or its",
        "  kill switch. The loop is not running.",
    ]
    return "\n".join(lines)


def format_result(result: dict) -> str:
    lines = [f"status: {result['status']}  ({result.get('minutes', 0):.0f} min)"]
    for name, err in result.get("errors", {}).items():
        lines.append(f"  stage error - {name}: {err}")
    t = result.get("teardown")
    if t:
        lines.append(f"teardown: destroyed={t['destroyed']} verifier_exit={t['verifier_exit']} clean={t['clean']}")
    a = result.get("analysis")
    if a:
        lines += ["", f"{'connections':<12} {'inst':>4} {'rpm':>6} {'got%':>5} {'p50':>6} {'p95':>6} {'slow%':>6} {'err%':>5} {'conn/req':>8}"]
        for c in a["cells"]:
            conn = "-" if c["connects_per_request"] is None else f"{c['connects_per_request']:.3f}"
            lines.append(
                f"{c['mode']:<12} {c['instances']:>4} {c['offered_rpm']:>6.0f} {100 * c['fidelity']:>5.0f} {_ms(c['p50_ms']):>6} {_ms(c['p95_ms']):>6} "
                f"{100 * (c['slow_share'] or 0):>6.2f} {100 * c['error_rate']:>5.2f} {conn:>8}"
            )
        v = a["verdict"]
        if v is not None:
            lines += ["", f"conclusion: {v['conclusion']} - {v['explanation']}"]
        k = a["keep_alive_knee"]
        if k.get("available"):
            kind = "at least" if k["knee_is_lower_bound"] else "highest passing step"
            lines.append(f"reused-connection knee (P95 rule, one instance): {kind} {k['knee_rpm']:g} rpm; first failing: {k['first_failing_rpm']}")
        if a["generator_limited_steps"]:
            lines.append(f"  WARNING: the laptop did not deliver the offered load in: {a['generator_limited_steps']}")
        for s in a["skipped"]:
            lines.append(f"  skipped block: {s}")
    return "\n".join(lines)


def _ms(x: Any) -> str:
    return "-" if x is None else f"{x:.0f}"
