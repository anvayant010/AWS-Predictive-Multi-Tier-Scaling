import { SCHEMA_VERSION, type DashboardData } from "./types";

/** The data file is missing, stale or inconsistent. The message says what to do about it. */
export class DataError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DataError";
  }
}

export const EXPORT_COMMAND = "python -m scripts.export_dashboard_data";

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/**
 * Checks the parts the page relies on so a stale or broken file fails with a clear message instead of a blank chart.
 * (The exporter validates deeper before writing; this is the browser-side seatbelt.)
 */
export function parseDashboardData(raw: unknown): DashboardData {
  if (!isRecord(raw)) throw new DataError(`The data file is not a JSON object. Regenerate it with: ${EXPORT_COMMAND}`);
  if (raw.schema_version !== SCHEMA_VERSION) {
    throw new DataError(`The data file has schema version ${String(raw.schema_version)}, this page expects ${SCHEMA_VERSION}. Regenerate it with: ${EXPORT_COMMAND}`);
  }
  const meta = raw.meta;
  if (!isRecord(meta) || meta.synthetic_traffic !== true || meta.replay_not_live_aws !== true) {
    throw new DataError("The data file does not declare that it is a replay on synthetic traffic, so it is not shown.");
  }
  if (!Array.isArray(raw.episodes) || raw.episodes.length === 0) throw new DataError("The data file contains no episodes.");
  for (const ep of raw.episodes) {
    if (!isRecord(ep) || typeof ep.id !== "string" || typeof ep.minutes !== "number") throw new DataError("An episode in the data file is malformed.");
    for (const key of ["traffic", "need"]) {
      const series = ep[key];
      if (!Array.isArray(series) || series.length !== ep.minutes) throw new DataError(`Episode "${ep.id}": ${key} should have ${ep.minutes} values.`);
    }
    if (!isRecord(ep.arms) || !isRecord(ep.forecast)) throw new DataError(`Episode "${ep.id}" is missing its approaches or forecast.`);
  }
  if (!isRecord(raw.accuracy) || !isRecord(raw.aws)) throw new DataError("The data file is missing its accuracy or AWS measurements.");
  return raw as unknown as DashboardData;
}
