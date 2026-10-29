"""Export the data the dashboard replays (no AWS, no database).

    python -m scripts.export_dashboard_data
    python -m scripts.export_dashboard_data --comparison replay_output/comparison_5seeds.json

Writes dashboard/public/data/dashboard.json. Takes about a minute: it regenerates the synthetic month in memory, trains
the forecaster on the first weeks and replays the held-out week through the decision engine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from comparison.dashboard_export import build_dashboard_data, validate_dashboard_data, write_dashboard_data

REPO = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=REPO / "dashboard" / "public" / "data" / "dashboard.json")
    p.add_argument("--comparison", type=Path, default=REPO / "replay_output" / "comparison_5seeds.json",
                   help="saved output of `python -m comparison.replay_compare --seeds ...` (skipped, with a warning, if missing)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--ramp", type=float, default=0.7)
    p.add_argument("--days", type=int, default=30)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.comparison.exists():
        print(f"WARNING: {args.comparison} not found - the comparison section will be missing. Run: python -m comparison.replay_compare --seeds 42 1 2 3 4 --out {args.comparison}")
    data = build_dashboard_data(ramp_fraction=args.ramp, seed=args.seed, days=args.days, comparison_path=args.comparison)
    problems = validate_dashboard_data(data)
    if problems:
        print("EXPORT REFUSED - the data is inconsistent:")
        for line in problems:
            print("  -", line)
        return 1
    size = write_dashboard_data(data, args.out)
    print(f"wrote {args.out} ({size / 1024:.0f} KB): {len(data['episodes'])} episodes")
    for ep in data["episodes"]:
        print(f"  {ep['id']:<22} {ep['minutes']} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
