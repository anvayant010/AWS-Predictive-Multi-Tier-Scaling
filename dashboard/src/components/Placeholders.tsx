import type { DashboardData } from "../data/types";
import { formatInt } from "../lib/format";

/** Temporary panels so each section shows that its data loaded. Replaced by the real sections in D4-D5. */

export function ComparisonPlaceholder({ data }: { data: DashboardData }) {
  const run = data.comparison?.["0.7"];
  return (
    <div className="placeholder">
      <h3>Comparison loaded · ledger arrives in D4</h3>
      {run ? `${run.seeds} seeds per approach, medians and ranges ready.` : "No comparison file was found: run python -m comparison.replay_compare --seeds 42 1 2 3 4."}
    </div>
  );
}

export function AwsPlaceholder({ data }: { data: DashboardData }) {
  const aws = data.aws;
  return (
    <div className="placeholder">
      <h3>AWS measurements loaded · panel arrives in D5</h3>
      instance healthy in {aws.instance_healthy_seconds} s · provisioned concurrency ready in {aws.provisioned_ready_seconds} s · cold start +
      {aws.cold_start_penalty_ms} ms · one instance served {formatInt(aws.single_instance_rpm_served)} requests/min
    </div>
  );
}
