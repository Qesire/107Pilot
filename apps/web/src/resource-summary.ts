import type { PlatformJobSnapshot, PlatformNodeSnapshot } from "./types";

export interface StateCount {
  state: string;
  count: number;
}

export interface CpuAllocation {
  allocated: number;
  total: number;
}

// Human-facing labels for normalized Slurm node states (see
// pilot107.core.platform_snapshot.NormalizedNodeState). Unknown states fall
// back to the raw value.
export const NODE_STATE_LABELS: Record<string, string> = {
  idle: "空闲",
  mixed: "部分占用",
  allocated: "已分配",
  completing: "释放中",
  draining: "排空中",
  down: "离线",
  unknown: "未知",
};

export function nodeStateLabel(state: string): string {
  return NODE_STATE_LABELS[state.toLowerCase()] ?? state;
}

export function jobStateLabel(state: string): string {
  const labels: Record<string, string> = {
    RUNNING: "运行中",
    PENDING: "排队中",
    COMPLETING: "结束中",
    COMPLETED: "已完成",
    FAILED: "失败",
    CANCELLED: "已取消",
    TIMEOUT: "超时",
    NODE_FAIL: "节点故障",
  };
  return labels[state.toUpperCase()] ?? state;
}

/** Count nodes grouped by normalized state, most frequent first. */
export function nodesByState(
  nodes: PlatformNodeSnapshot[] | undefined,
): StateCount[] {
  const counts = new Map<string, number>();
  for (const node of nodes ?? []) {
    const state = (node.state_normalized || "unknown").toLowerCase();
    counts.set(state, (counts.get(state) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([state, count]) => ({ state, count }))
    .sort((a, b) => b.count - a.count || a.state.localeCompare(b.state));
}

/** Sum CPU allocation across all observed nodes. */
export function cpuAllocation(
  nodes: PlatformNodeSnapshot[] | undefined,
): CpuAllocation {
  let allocated = 0;
  let total = 0;
  for (const node of nodes ?? []) {
    if (typeof node.cpus_total === "number") total += node.cpus_total;
    if (typeof node.cpus_allocated === "number") allocated += node.cpus_allocated;
  }
  return { allocated, total };
}

/** Count squeue jobs grouped by raw state, most frequent first. */
export function jobsByState(
  jobs: PlatformJobSnapshot[] | undefined,
): StateCount[] {
  const counts = new Map<string, number>();
  for (const job of jobs ?? []) {
    const state = (job.state_raw || "UNKNOWN").toUpperCase();
    counts.set(state, (counts.get(state) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([state, count]) => ({ state, count }))
    .sort((a, b) => b.count - a.count || a.state.localeCompare(b.state));
}

/** Format a byte count with binary units up to TiB (storage cards). */
export function formatStorageBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "—";
  const kib = 1024;
  const mib = kib * 1024;
  const gib = mib * 1024;
  const tib = gib * 1024;
  if (bytes < kib) return `${bytes} B`;
  if (bytes < mib) return `${(bytes / kib).toFixed(1)} KiB`;
  if (bytes < gib) return `${(bytes / mib).toFixed(1)} MiB`;
  if (bytes < tib) return `${(bytes / gib).toFixed(2)} GiB`;
  return `${(bytes / tib).toFixed(2)} TiB`;
}
