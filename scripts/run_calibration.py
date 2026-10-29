"""Run the Phase 7 calibration session (decisions D5 / D9).

    python -m scripts.run_calibration                 # shows the plan and the spend bound, applies NOTHING
    python -m scripts.run_calibration --confirm       # applies the stack, measures, ALWAYS destroys + verifies

Real AWS actions only happen with --confirm, and only after the preflight passes (empty Terraform state,
kill switch ENGAGED so no scaling loop can interfere, the scoped project identity). Teardown runs on every
path once the stack exists; if it cannot be confirmed clean the script says so loudly and exits non-zero.
Raw measurements are written under --out as each stage finishes (default calibration_output/<timestamp>/).

Everything decision-relevant lives in comparison/calibration_session.py (tested with fakes); this file only
wires the real terraform / boto3 / network pieces into it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import boto3

from comparison.calibration_session import (
    DEFAULT_RATES,
    SessionConfig,
    SessionDeps,
    format_plan,
    format_result,
    make_file_saver,
    run_session,
)
from comparison.calibration_stages import StageEnv, urllib_get
from decision_engine.kill_switch import read_kill_switch

REPO = Path(__file__).resolve().parent.parent
INFRA = REPO / "infra"
PROFILE = os.environ.get("AWS_PROFILE", "predictive-scaling")
REGION = os.environ.get("AWS_REGION", "us-east-1")
EXPECTED_IDENTITY = "predictive-scaling-engine"
HEALTH_TIMEOUT_S = 900.0


def _tf(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["terraform", *args], cwd=INFRA, text=True, capture_output=capture, encoding="utf-8")


def build_deps(session: boto3.Session) -> SessionDeps:
    aws = SimpleNamespace(
        autoscaling=session.client("autoscaling"), elbv2=session.client("elbv2"), lambda_=session.client("lambda"),
        logs=session.client("logs"), cloudwatch=session.client("cloudwatch"),
    )
    ssm, sts = session.client("ssm"), session.client("sts")

    def preflight() -> list[str]:
        problems = []
        if shutil.which("terraform") is None:
            return ["terraform is not on PATH"]
        state = _tf("state", "list", capture=True)
        if state.returncode != 0:
            problems.append(f"could not read the Terraform state: {state.stderr.strip()[:200]}")
        elif state.stdout.strip():
            problems.append("the Terraform state is not empty: a stack already exists (destroy it first)")
        if not read_kill_switch(ssm).engaged:
            problems.append("the kill switch is RELEASED; engage it first (`make kill`) so no scaling loop can interfere")
        try:
            arn = sts.get_caller_identity()["Arn"]
            if EXPECTED_IDENTITY not in arn:
                problems.append(f"running as {arn}, not the scoped project user {EXPECTED_IDENTITY}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"could not determine the AWS identity: {exc}")
        return problems

    def apply() -> dict:
        proc = subprocess.run(
            ["terraform", "apply", "-auto-approve", "-input=false", "-var", "scaling_mode=predictive"], cwd=INFRA, text=True
        )
        if proc.returncode != 0:
            raise RuntimeError(f"terraform apply failed (exit {proc.returncode}); the stack may be partly created")
        out = _tf("output", "-json", capture=True)
        if out.returncode != 0:
            raise RuntimeError(f"terraform output failed: {out.stderr.strip()[:200]}")
        return {k: v["value"] for k, v in json.loads(out.stdout).items()}

    def outputs_to_names(o: dict) -> dict:
        return {
            "asg_name": o["catalog_asg_name"], "target_group_arn": o["catalog_target_group_arn"],
            "function_name": o["thumbnail_lambda_function_name"], "alias": o["thumbnail_lambda_alias_name"],
            "log_group": f"/aws/lambda/{o['thumbnail_lambda_function_name']}",
        }

    def make_env(o: dict) -> StageEnv:
        return StageEnv(catalog_url=f"http://{o['catalog_alb_dns_name']}", thumbnail_url=str(o["thumbnail_api_invoke_url"]).rstrip("/"))

    def wait_healthy(env: StageEnv) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            catalog = urllib_get(env.catalog_url + "/health")
            thumb = urllib_get(env.thumbnail_url + "/thumbnail?image_id=warmup&width=64&height=64")
            if catalog.status == 200 and thumb.status == 200:
                return
            time.sleep(10)
        raise RuntimeError("the stack did not become healthy in time")

    def destroy() -> bool:
        return subprocess.run(["terraform", "destroy", "-auto-approve", "-input=false"], cwd=INFRA, text=True).returncode == 0

    def verify() -> int:
        return subprocess.run([sys.executable, "-m", "scripts.verify_teardown"], cwd=REPO, text=True).returncode

    return SessionDeps(
        preflight=preflight, apply=apply, make_env=make_env, wait_healthy=wait_healthy, aws=aws,
        outputs_to_names=outputs_to_names, destroy=destroy, verify=verify,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--confirm", action="store_true", help="actually apply, measure and tear down (without it: plan only)")
    p.add_argument("--out", type=Path, default=None, help="output directory (default calibration_output/<timestamp>)")
    p.add_argument("--max-minutes", type=float, default=150.0, help="hard limit on the measuring phase")
    p.add_argument("--rates", type=str, default=",".join(str(r) for r in DEFAULT_RATES), help="knee steps in requests/min")
    p.add_argument("--idle-gaps", type=str, default="3,6,10,15,20,30", help="idle-container staircase gaps, minutes")
    p.add_argument("--measure-s", type=float, default=150.0)
    p.add_argument("--boot-repeats", type=int, default=3)
    p.add_argument("--pc-repeats", type=int, default=3)
    return p.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> SessionConfig:
    out = args.out or (REPO / "calibration_output" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    return SessionConfig(
        out_dir=out, max_minutes=args.max_minutes, rates=tuple(float(r) for r in args.rates.split(",")),
        idle_gaps=tuple(float(g) for g in args.idle_gaps.split(",")), measure_s=args.measure_s,
        boot_repeats=args.boot_repeats, provisioned_repeats=args.pc_repeats,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = config_from_args(args)
    print(format_plan(cfg))
    if not args.confirm:
        print("\nPlan only. Re-run with --confirm to apply, measure and tear down.")
        return 0

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    deps = build_deps(session)
    deps.save = make_file_saver(cfg.out_dir)
    print(f"\nrunning for real; raw data goes to {cfg.out_dir}\n")
    result = run_session(cfg, deps)
    print("\n" + format_result(result))
    if result["status"] == "aborted_preflight":
        return 3
    teardown = result.get("teardown") or {}
    return 0 if teardown.get("clean") else 4


if __name__ == "__main__":
    sys.exit(main())
