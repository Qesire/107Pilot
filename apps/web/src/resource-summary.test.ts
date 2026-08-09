import { describe, expect, it } from "vitest";
import {
  cpuAllocation,
  formatStorageBytes,
  jobsByState,
  nodeStateLabel,
  nodesByState,
} from "./resource-summary";
import type { PlatformJobSnapshot, PlatformNodeSnapshot } from "./types";

function node(state: string, cpusTotal: number | null, cpusAllocated: number | null): PlatformNodeSnapshot {
  return {
    node_name: `n-${Math.random()}`,
    state_normalized: state,
    cpus_total: cpusTotal,
    cpus_allocated: cpusAllocated,
  };
}

describe("nodesByState", () => {
  it("groups by normalized state, most frequent first", () => {
    const nodes = [node("IDLE", 8, 0), node("idle", 8, 0), node("MIXED", 8, 4)];
    expect(nodesByState(nodes)).toEqual([
      { state: "idle", count: 2 },
      { state: "mixed", count: 1 },
    ]);
  });

  it("returns empty for undefined", () => {
    expect(nodesByState(undefined)).toEqual([]);
  });

  it("falls back to unknown for missing state", () => {
    expect(nodesByState([node("", 8, 0)])).toEqual([{ state: "unknown", count: 1 }]);
  });
});

describe("cpuAllocation", () => {
  it("sums allocated and total, ignoring nulls", () => {
    const nodes = [node("IDLE", 16, 0), node("MIXED", 16, 10), node("UNKNOWN", null, null)];
    expect(cpuAllocation(nodes)).toEqual({ allocated: 10, total: 32 });
  });

  it("returns zeros for undefined", () => {
    expect(cpuAllocation(undefined)).toEqual({ allocated: 0, total: 0 });
  });
});

describe("jobsByState", () => {
  it("groups by raw state uppercased", () => {
    const jobs: PlatformJobSnapshot[] = [
      { job_id: "1", state_raw: "RUNNING" },
      { job_id: "2", state_raw: "running" },
      { job_id: "3", state_raw: "PENDING" },
    ];
    expect(jobsByState(jobs)).toEqual([
      { state: "RUNNING", count: 2 },
      { state: "PENDING", count: 1 },
    ]);
  });
});

describe("formatStorageBytes", () => {
  it.each<[number | null, string]>([
    [0, "0 B"],
    [512, "512 B"],
    [2048, "2.0 KiB"],
    [5 * 1024 * 1024, "5.0 MiB"],
    [3 * 1024 * 1024 * 1024, "3.00 GiB"],
    [2 * 1024 ** 4, "2.00 TiB"],
    [null, "—"],
  ])("formats %s as %s", (input, expected) => {
    expect(formatStorageBytes(input)).toBe(expected);
  });
});

describe("nodeStateLabel", () => {
  it("maps known states to Chinese labels", () => {
    expect(nodeStateLabel("idle")).toBe("空闲");
    expect(nodeStateLabel("MIXED")).toBe("部分占用");
  });

  it("falls back to the raw value for unknown states", () => {
    expect(nodeStateLabel("weird")).toBe("weird");
  });
});
