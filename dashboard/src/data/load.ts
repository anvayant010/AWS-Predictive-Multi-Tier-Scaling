import { DataError, EXPORT_COMMAND, parseDashboardData } from "./parse";
import type { DashboardData } from "./types";

export const DATA_URL = `${import.meta.env.BASE_URL}data/dashboard.json`;

export async function loadDashboardData(signal?: AbortSignal): Promise<DashboardData> {
  let response: Response;
  try {
    response = await fetch(DATA_URL, { signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new DataError(`Could not reach the data file. Is the page being served? (${String(error)})`);
  }
  if (!response.ok) {
    throw new DataError(`The data file was not found (HTTP ${response.status}). Generate it with: ${EXPORT_COMMAND}`);
  }
  let raw: unknown;
  try {
    raw = await response.json();
  } catch {
    throw new DataError(`The data file is not valid JSON. Regenerate it with: ${EXPORT_COMMAND}`);
  }
  return parseDashboardData(raw);
}
