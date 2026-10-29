"""Run the Phase 7 connection re-test (what limited the first calibration's capacity "knee"?).

    python -m scripts.run_recheck                 # shows the plan and the spend bound, applies NOTHING
    python -m scripts.run_recheck --confirm       # applies the stack, measures, ALWAYS destroys + verifies

Same safety model as scripts/run_calibration.py (whose wiring of terraform / boto3 it reuses): real AWS actions
only with --confirm, only after the preflight passes (empty Terraform state, kill switch ENGAGED, the scoped
project identity), teardown on every exit path that Python can handle. Run it in your own terminal and keep the
laptop awake: a hard kill of the process (out of memory, closing the window) is the one thing it cannot survive,
and would leave the stack up (then: `python -m scripts.verify_teardown`, `cd infra; terraform destroy`).
Exit codes: 0 clean / 3 preflight abort / 4 teardown not clean.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import boto3

from comparison.calibration_session import make_file_saver
from comparison.recheck import PLANS
from comparison.recheck_session import RecheckConfig, format_plan, format_result, run_recheck
from scripts.run_calibration import PROFILE, REGION, REPO, build_deps


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--confirm", action="store_true", help="actually apply, measure and tear down (without it: plan only)")
    p.add_argument("--out", type=Path, default=None, help="output directory (default calibration_output/recheck-<timestamp>)")
    p.add_argument("--plan", choices=sorted(PLANS), default="default",
                   help="default = the connection re-test; knee = one instance, reused connections, 400..25000 rpm")
    p.add_argument("--max-minutes", type=float, default=60.0, help="hard limit on the measuring phase")
    p.add_argument("--measure-s", type=float, default=150.0)
    return p.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> RecheckConfig:
    prefix = "recheck-" if args.plan == "default" else f"{args.plan}-"
    out = args.out or (REPO / "calibration_output" / (prefix + datetime.now().strftime("%Y%m%d-%H%M%S")))
    return RecheckConfig(out_dir=out, plan=PLANS[args.plan], max_minutes=args.max_minutes, measure_s=args.measure_s)


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
    result = run_recheck(cfg, deps)
    print("\n" + format_result(result))
    if result["status"] == "aborted_preflight":
        return 3
    teardown = result.get("teardown") or {}
    return 0 if teardown.get("clean") else 4


if __name__ == "__main__":
    sys.exit(main())
