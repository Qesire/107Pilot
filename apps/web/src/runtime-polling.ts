import { useSyncExternalStore } from "react";
import type { RuntimeWatchState } from "./types";

export type RuntimePollingChannel = "summary" | "logs" | "alerts";
export type RuntimeViewerVisibility = "visible" | "hidden";

const VISIBLE_INTERVALS: Record<
  RuntimeWatchState,
  Record<RuntimePollingChannel, number | false>
> = {
  watching: { summary: 5_000, logs: 5_000, alerts: 10_000 },
  waiting_for_log: { summary: 10_000, logs: 10_000, alerts: 10_000 },
  active: { summary: 3_000, logs: 3_000, alerts: 5_000 },
  quiet_backoff: { summary: 15_000, logs: 15_000, alerts: 15_000 },
  degraded: { summary: 10_000, logs: 10_000, alerts: 10_000 },
  finalizing: { summary: 2_000, logs: 2_000, alerts: 5_000 },
  stopped: { summary: false, logs: false, alerts: false },
};

/**
 * Browser polling policy for one persisted Runtime Watch.
 *
 * The Worker owns remote-log collection. The browser only polls persisted
 * read models, so a hidden viewer must not create background request load.
 * Child streams remain disabled until the watch summary exists.
 */
export function runtimePollingInterval(
  channel: RuntimePollingChannel,
  state: RuntimeWatchState | null | undefined,
  visibility: RuntimeViewerVisibility = "visible",
): number | false {
  if (visibility === "hidden") return false;
  if (!state) return channel === "summary" ? 5_000 : false;
  return VISIBLE_INTERVALS[state][channel];
}

function subscribeDocumentVisibility(onStoreChange: () => void): () => void {
  if (typeof document === "undefined") return () => undefined;
  document.addEventListener("visibilitychange", onStoreChange);
  return () => document.removeEventListener("visibilitychange", onStoreChange);
}

function readDocumentVisibility(): RuntimeViewerVisibility {
  if (typeof document === "undefined") return "visible";
  return document.visibilityState === "hidden" ? "hidden" : "visible";
}

export function useDocumentVisibility(): RuntimeViewerVisibility {
  return useSyncExternalStore(
    subscribeDocumentVisibility,
    readDocumentVisibility,
    () => "visible",
  );
}
