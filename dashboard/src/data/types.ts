// Shapes of dashboard.json as written by comparison/dashboard_export.py (schema_version 1).
// Keep in step with that module: its validator and these types describe the same file.

export const SCHEMA_VERSION = 1;

export type Ec2ArmName = "predictive" | "reactive_standard" | "reactive_aggressive" | "static_peak";
export type LambdaArmName = "predictive" | "reactive_pc_tracking" | "reactive_on_demand_only" | "static_peak";

/** Per-minute series for one approach, aligned with the episode's minutes. */
export interface Ec2Arm {
  capacity: number[]; // instances in service
  booting: number[]; // requested earlier, not usable yet
  asked: number[]; // newly requested this minute
  over_plan: number[]; // 1 when traffic exceeds the 70% capacity plan
  saturated: number[]; // 1 when traffic exceeds the declared capacity
}
export interface LambdaArm {
  capacity: number[]; // provisioned containers ready
  booting: number[];
  asked: number[];
  cold: number[]; // cold starts this minute
  warm: number[]; // on-demand containers still warm
}

/** The model's 15-step forecast issued at each minute of the episode (traffic units). */
export interface ForecastFan {
  p10: number[][];
  p50: number[][];
  p90: number[][];
}

interface EpisodeBase {
  id: string;
  service: "catalog_service" | "thumbnail_service";
  title: string;
  start: string; // ISO, naive clock of the replay
  minutes: number;
  traffic: number[];
  need: number[];
  forecast: ForecastFan;
}

export interface Ec2Episode extends EpisodeBase {
  kind: "ec2";
  arms: Record<Ec2ArmName, Ec2Arm>;
  context: Record<Ec2ArmName, { minutes_over_plan_here: number; minutes_over_plan_week_pct: number; instance_hours_week: number }>;
  units: { traffic: string; need: string; plan_rpm_per_instance: number; full_rpm_per_instance: number };
}

export interface AllBurstStats {
  bursts: number;
  median_cold_starts: Record<string, number>;
  mean_cold_starts: Record<string, number>;
  predictive_vs_reactive?: { better: number; same: number; worse: number };
}

export interface LambdaEpisode extends EpisodeBase {
  kind: "lambda";
  arms: Record<LambdaArmName, LambdaArm>;
  burst: { onset: number; full_onset: number; end: number; ramp_visible: boolean; peak: number };
  context: Record<LambdaArmName, { cold_starts_in_this_burst: number }>;
  all_bursts: AllBurstStats;
  units: { traffic: string; need: string };
}

export type Episode = Ec2Episode | LambdaEpisode;

export interface Spread {
  median: number;
  min: number;
  max: number;
}
/** metric name -> spread across seeds, per approach. */
export type ArmSpreads = Record<string, Record<string, Spread>>;
export interface ComparisonRun {
  seeds: number;
  catalog_service: ArmSpreads;
  thumbnail_service: ArmSpreads;
}
export interface Comparison {
  "0.7"?: ComparisonRun;
  "0.0"?: ComparisonRun;
  hero?: { predictive_inside_plan_pct: number; reactive_standard_inside_plan_pct: number };
}

export interface Accuracy {
  service: string;
  horizon_minutes: number;
  points: number;
  mae: number;
  interval_coverage_pct: number;
  mae_same_time_last_week?: number;
  mape_pct?: number;
  burst_warning?: Record<"ramp_visible" | "no_ramp", { flagged: number; bursts: number; pct: number | null }>;
}

export interface AwsFacts {
  instance_healthy_seconds: number;
  provisioned_ready_seconds: number;
  cold_start_penalty_ms: number;
  lambda_billed_ms: number;
  idle_container_expiry_minutes: number;
  single_instance_rpm_served: number;
  single_instance_p95_ms: number;
  single_instance_cpu_pct: number;
  cold_starts_matched_aws_logs: { marker: number; aws_init_lines: number };
  region: string;
  measured_on: string;
  also_real: string[];
}

export interface DashboardMeta {
  generated_at: string;
  seed: number;
  ramp_fraction: number;
  synthetic_traffic: boolean;
  replay_not_live_aws: boolean;
  prices_verified: boolean;
  load_scale: { catalog_service: number; thumbnail_service: number };
  billed_seconds: number;
  delays_minutes: { instance: number; provisioned_concurrency: number; metric_lag: number };
  plan_utilization: number;
  declared_rpm_per_instance: number;
  forecast_horizon_minutes: number;
  episode_rules: Record<string, string>;
}

export interface DashboardData {
  schema_version: number;
  meta: DashboardMeta;
  episodes: Episode[];
  accuracy: Record<string, Accuracy>;
  aws: AwsFacts;
  comparison?: Comparison;
}
