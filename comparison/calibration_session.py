"""Orchestrates one Phase 7 calibration session: apply -> measure -> ALWAYS tear down -> analyse (decisions D5, D9).

Everything that touches the world (terraform, AWS, the network) is injected through `SessionDeps`, so the
properties that matter are tested with fakes:

* nothing is applied unless the preflight finds no problems;
* once the stack exists, teardown ALWAYS runs, whatever happens in between (a crashing stage, a stop request,
  the time limit), and a leftover provisioned-concurrency configuration is removed first because it would
  block `terraform destroy`;
* a hard time limit stops the measuring; teardown then still runs;
* two independent stage groups run side by side (they hit different services), and a failing stage is
  recorded without stopping the other stages.

Raw measurements are written to disk as soon as each stage finishes, so a later crash loses nothing.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from comparison import calibration as cal
from comparison import calibration_stages as stages
from comparison.calibration import IdleProbe, LoadStep
from comparison.cost import estimate_before_run

DEFAULT_RATES = (200, 400, 700, 1000, 1400, 1800, 2200, 2600, 3200, 4000, 5000)
LOG_DELIVERY_WAIT_S = 60.0  # CloudWatch Logs can lag a little behind the invocation


@dataclass(frozen=True)
class SessionConfig:
    out_dir: Path
    max_minutes: float = 150.0
    rates: tuple[float, ...] = DEFAULT_RATES
    settle_s: float = 30.0
    measure_s: float = 150.0
    idle_gaps: tuple[float, ...] = (3, 6, 10, 15, 20, 30)
    idle_parallel: int = 6
    boot_repeats: int = 3
    provisioned_repeats: int = 3
    setup_minutes: float = 12.0  # apply + health wait + destroy + verify

    def __post_init__(self) -> None:
        if self.max_minutes <= 0 or not self.rates or self.settle_s < 0 or self.measure_s <= 0:
            raise ValueError("invalid session configuration")
        if self.boot_repeats < 0 or self.provisioned_repeats < 0 or self.idle_parallel < 1:
            raise ValueError("invalid session configuration")


def expected_minutes(cfg: SessionConfig) -> dict[str, float]:
    """Rough duration of each stage group, for the plan shown before anything runs."""
    knee = len(cfg.rates) * (cfg.settle_s + cfg.measure_s) / 60.0
    boot = cfg.boot_repeats * 5.0
    cold = 1.0
    staircase = sum(cfg.idle_gaps)
    provisioned = cfg.provisioned_repeats * 4.5 + (LOG_DELIVERY_WAIT_S / 60.0)
    group_a, group_b = knee + boot, cold + staircase + provisioned
    return {
        "group_a_catalog_minutes": group_a, "group_b_lambda_minutes": group_b,
        "measuring_minutes": max(group_a, group_b), "total_stack_minutes": max(group_a, group_b) + cfg.setup_minutes,
    }


def spend_estimate(cfg: SessionConfig) -> dict:
    """Upper bound: at most 2 instances and 1 provisioned unit for the whole time limit plus setup."""
    return estimate_before_run(
        scenario_minutes=cfg.max_minutes, setup_minutes=cfg.setup_minutes, max_instances=stages.BOOT_MAX_INSTANCES,
        max_provisioned=stages.PROVISIONED_MAX, expected_thumbnail_requests=60_000,
    )


@dataclass
class SessionDeps:
    preflight: Callable[[], list[str]]  # problems; empty = fine to apply
    apply: Callable[[], dict]  # terraform apply -> outputs
    make_env: Callable[[dict], stages.StageEnv]
    wait_healthy: Callable[[stages.StageEnv], None]
    aws: Any  # object with .autoscaling .elbv2 .lambda_ .logs .cloudwatch
    outputs_to_names: Callable[[dict], dict]  # -> asg_name, target_group_arn, function_name, alias, log_group
    destroy: Callable[[], bool]
    verify: Callable[[], int]  # 0 clean / 1 billable found / 2 could not verify
    save: Callable[[str, Any], None] = field(default=lambda name, obj: None)
    log: Callable[[str], None] = field(default=print)


# --------------------------------------------------------------------------------------------- raw data


def _step_to_dict(step: LoadStep) -> dict:
    return asdict(step)


def _probe_to_dict(p: IdleProbe) -> dict:
    return {"gap_minutes": p.gap_minutes, "known_before": sorted(p.known_before), "seen_after": sorted(p.seen_after)}


def _probe_from_dict(d: dict) -> IdleProbe:
    return IdleProbe(d["gap_minutes"], frozenset(d["known_before"]), frozenset(d["seen_after"]))


# -------------------------------------------------------------------------------------------- the session


def run_session(cfg: SessionConfig, deps: SessionDeps) -> dict:
    """Run the whole session. Returns the result dict (also saved). Never raises for a stage failure."""
    result: dict = {"status": "not_started", "errors": {}, "problems": [], "teardown": None}
    problems = deps.preflight()
    if problems:
        result.update(status="aborted_preflight", problems=problems)
        deps.log("PREFLIGHT FAILED - nothing was applied: " + "; ".join(problems))
        return result

    raw: dict = {}
    names: dict = {}
    env: stages.StageEnv | None = None
    deadline: threading.Timer | None = None
    threads: list[threading.Thread] = []
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

        def record(name: str, data: Any) -> None:
            raw[name] = data
            deps.save(name, data)

        def guarded(name: str, fn: Callable[[], Any]) -> None:
            if env.stop.is_set():
                result["errors"][name] = "skipped: stop requested before it started"
                return
            deps.log(f"stage {name}: start")
            try:
                record(name, fn())
                deps.log(f"stage {name}: done")
            except Exception as exc:  # noqa: BLE001  (a failed stage must not stop the others or the teardown)
                result["errors"][name] = f"{type(exc).__name__}: {exc}"
                deps.log(f"stage {name}: FAILED {exc!r}")

        aws = deps.aws
        lambda_since_ms = int(env.wall() * 1000)

        def group_a() -> None:
            def knee() -> dict:
                out = stages.run_knee(env, list(cfg.rates), settle_s=cfg.settle_s, measure_s=cfg.measure_s)
                return {"steps": [_step_to_dict(s) for s in out["steps"]], "windows": out["windows"], "raw": out["raw"]}

            guarded("knee", knee)
            guarded("boot", lambda: stages.run_boot_timing(env, aws.autoscaling, aws.elbv2, names["asg_name"], names["target_group_arn"], cfg.boot_repeats))
            guarded("cpu", lambda: stages.fetch_asg_cpu(aws.cloudwatch, names["asg_name"], _utc(started_wall), _utc(env.wall())))

        def group_b() -> None:
            guarded("cold_probe", lambda: stages.run_cold_start_probe(env))
            guarded("idle", lambda: _idle_to_raw(stages.run_idle_staircase(env, list(cfg.idle_gaps), parallel=cfg.idle_parallel)))
            if env.wait(LOG_DELIVERY_WAIT_S):
                guarded("report_lines", lambda: stages.fetch_report_messages(aws.logs, names["log_group"], lambda_since_ms))
            guarded("provisioned", lambda: stages.run_provisioned_timing(env, aws.lambda_, names["function_name"], names["alias"], cfg.provisioned_repeats))

        started_wall = env.wall()
        threads += [threading.Thread(target=group_a, name="catalog-stages"), threading.Thread(target=group_b, name="lambda-stages")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        result["status"] = "measured" if not env.stop.is_set() else "stopped_early"
    except BaseException as exc:  # noqa: BLE001  (including Ctrl-C: the stack must still be torn down)
        result["status"] = "crashed"
        result["errors"]["session"] = f"{type(exc).__name__}: {exc}"
        deps.log(f"SESSION ERROR: {exc!r}")
    finally:
        if deadline is not None:
            deadline.cancel()
        if env is not None:
            env.stop.set()
        for t in threads:  # let in-flight stages notice the stop before their stack is destroyed
            t.join(timeout=90)
        result["teardown"] = _teardown(deps, names)
        result["minutes"] = (time.monotonic() - started) / 60.0

    if raw:
        result["analysis"] = analyze_session(raw)
        deps.save("calibration", result["analysis"])
    deps.save("session", result)
    return result


def _idle_to_raw(out: dict) -> dict:
    return {"probes": [_probe_to_dict(p) for p in out["probes"]], "rows": out["rows"]}


def _utc(epoch: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _teardown(deps: SessionDeps, names: dict) -> dict:
    """Runs on every path once a stack may exist. Never raises."""
    out: dict = {"provisioned_removed": None, "destroyed": False, "verifier_exit": None, "errors": []}
    if names:
        try:
            out["provisioned_removed"] = stages.remove_provisioned_concurrency(deps.aws.lambda_, names["function_name"], names["alias"])
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"provisioned-concurrency clean-up failed: {exc!r}")
    try:
        out["destroyed"] = bool(deps.destroy())
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"destroy raised: {exc!r}")
    try:
        out["verifier_exit"] = deps.verify()
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"verifier raised: {exc!r}")
    out["clean"] = bool(out["destroyed"] and out["verifier_exit"] == 0)
    if not out["clean"]:
        deps.log("!! TEARDOWN NOT CONFIRMED CLEAN - run `make destroy` and `make verify-teardown` NOW !!")
    return out


# ---------------------------------------------------------------------------------------------- analysis


def analyze_session(raw: dict) -> dict:
    """Raw stage outputs -> the calibration result (see comparison/calibration.py for the rules)."""
    missing: list[str] = []

    steps = [LoadStep(**s) for s in raw.get("knee", {}).get("steps", [])]
    try:
        knee = cal.find_knee(steps)
    except ValueError as exc:
        missing.append(f"capacity knee unavailable: {exc}")
        knee = {"knee_rpm": None, "knee_is_lower_bound": False, "generator_limited": False, "unloaded_p95_ms": None, "slo_ms": None, "steps": []}

    lambda_rows = list(raw.get("cold_probe", [])) + list(raw.get("idle", {}).get("rows", []))
    cold = cal.cold_start_stats(lambda_rows)
    report_rows = cal.parse_report_lines(raw.get("report_lines", []))
    if "report_lines" not in raw:
        missing.append("no REPORT log lines were collected: the cold-start cross-check was not done")
    crosscheck = cal.crosscheck_cold_starts(cold["cold_n"], report_rows)
    if "report_lines" not in raw:
        crosscheck["agree"] = False
    durations = cal.handler_duration_stats(report_rows)

    probes = [_probe_from_dict(p) for p in raw.get("idle", {}).get("probes", [])]
    idle = cal.idle_lifetime(probes)
    boot = cal.timing_stats(raw.get("boot", {}).get("seconds", []))
    provisioned = cal.timing_stats(raw.get("provisioned", {}).get("seconds", []))

    serve = raw.get("provisioned", {}).get("serve_checks", [])
    pc_serves = bool(serve) and all(c["init_type"] == "provisioned-concurrency" and c["cold"] is False for c in serve)
    if not serve:
        missing.append("provisioned concurrency was never confirmed to serve requests")
    elif not pc_serves:
        missing.append("a provisioned-concurrency container did not report init_type=provisioned-concurrency / cold=false: the marker assumption is wrong")

    cpu = raw.get("cpu", [])
    cpu_max = max((p["maximum"] for p in cpu), default=None)

    params = cal.derive_parameters(
        knee=knee, cold=cold, crosscheck=crosscheck, durations=durations, idle=idle, boot=boot,
        provisioned_ready=provisioned, cpu_max_percent_at_knee=cpu_max,
    )
    params["warnings"] = missing + params["warnings"]
    params["trustworthy"] = not params["warnings"]
    return {
        "parameters": params, "knee": knee, "cold_start": cold, "crosscheck": crosscheck, "handler_durations": durations,
        "idle_lifetime": idle, "boot": {**boot, "timeouts": raw.get("boot", {}).get("timeouts")},
        "provisioned_ready": {**provisioned, "timeouts": raw.get("provisioned", {}).get("timeouts")},
        "provisioned_serves_requests": pc_serves, "cpu_max_percent": cpu_max,
    }


# --------------------------------------------------------------------------------------------- reporting


def format_plan(cfg: SessionConfig) -> str:
    mins = expected_minutes(cfg)
    spend = spend_estimate(cfg)
    lines = [
        "CALIBRATION SESSION PLAN (nothing has been applied yet)",
        "",
        "  A  catalog   capacity knee: " + f"{len(cfg.rates)} steps up to {max(cfg.rates):g} rpm, {cfg.settle_s:g}s settle + {cfg.measure_s:g}s measured each (stops after 2 failing steps)",
        f"             instance boot x{cfg.boot_repeats}: ASG 1 -> 2 -> 1 (never above {stages.BOOT_MAX_INSTANCES})",
        "  B  thumbnail cold-start probe, then idle-container staircase with gaps " + ", ".join(f"{g:g}" for g in cfg.idle_gaps) + " min,",
        f"             REPORT-line cross-check, provisioned-concurrency readiness x{cfg.provisioned_repeats} (never above {stages.PROVISIONED_MAX} unit)",
        "",
        f"  expected: A ~{mins['group_a_catalog_minutes']:.0f} min, B ~{mins['group_b_lambda_minutes']:.0f} min in parallel => measuring ~{mins['measuring_minutes']:.0f} min,"
        f" stack up ~{mins['total_stack_minutes']:.0f} min",
        f"  HARD LIMIT: measuring is stopped after {cfg.max_minutes:g} min; teardown + verifier then run regardless",
        f"  spend: upper bound ~${spend['total_usd']:.2f} (prices unverified); realistic ~$0.10-0.30",
        "",
        "  This session changes capacity directly (ASG up to 2, provisioned concurrency up to 1) through the",
        "  capped actuators; it does NOT go through the scaling loop or its kill switch. The loop is not running.",
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
        p = a["parameters"]
        lines += [
            "",
            f"capacity knee: {p['capacity_rpm_per_instance']} rpm/instance  (70% = {p['capacity_rpm_per_instance_at_70pct']})   SLO {p['slo_ms']} ms",
            f"cold-start penalty: {p['lambda_cold_penalty_ms']} ms   AWS Init Duration median: {p['lambda_aws_init_ms_median']} ms",
            f"billed handler duration: {p['lambda_billed_ms_per_request']} ms",
            f"idle-container lifetime: recommended scenario gap {p['scenario_gap_minutes']} min",
            f"instance boot: {p['instance_boot_seconds_median']} s   provisioned ready: {p['provisioned_ready_seconds_median']} s",
            f"trustworthy: {p['trustworthy']}",
        ]
        lines += [f"  WARNING: {w}" for w in p["warnings"]]
    return "\n".join(lines)


def make_file_saver(out_dir: Path) -> Callable[[str, Any], None]:
    out_dir.mkdir(parents=True, exist_ok=True)

    def save(name: str, obj: Any) -> None:
        (out_dir / f"{name}.json").write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8", newline="")

    return save
