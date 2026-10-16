import { useEffect, useState } from "react";
import { loadDashboardData } from "../data/load";
import { DataError } from "../data/parse";
import type { DashboardData } from "../data/types";

export type DataState = { status: "loading" } | { status: "error"; message: string } | { status: "ready"; data: DashboardData };

export function useDashboardData(): DataState {
  const [state, setState] = useState<DataState>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    loadDashboardData(controller.signal).then(
      (data) => setState({ status: "ready", data }),
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setState({ status: "error", message: error instanceof DataError ? error.message : `Unexpected error: ${String(error)}` });
      },
    );
    return () => controller.abort();
  }, []);

  return state;
}
