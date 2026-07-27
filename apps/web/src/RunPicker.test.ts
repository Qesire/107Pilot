import { describe, expect, it } from "vitest";
import { filterRuns, type RunPickerRun } from "./RunPicker";

describe("RunPicker filterRuns", () => {
  const runs: RunPickerRun[] = [
    { run_id: "run_1", state: "FAILED", created_at: "2026-07-18T10:00:00Z", recipe_version_id: "recipe_a" },
    { run_id: "run_2", state: "SUCCEEDED", created_at: "2026-07-18T11:00:00Z", recipe_version_id: "recipe_b" },
    { run_id: "run_3", state: "FAILED", created_at: "2026-07-18T12:00:00Z", recipe_version_id: "recipe_a" },
    { run_id: "run_4", state: "COLLECTION_FAILED", created_at: "2026-07-18T13:00:00Z", recipe_version_id: "recipe_a" },
  ];

  it("returns all runs when no filter", () => {
    expect(filterRuns(runs)).toEqual(runs);
    expect(filterRuns(runs, {})).toEqual(runs);
  });

  it("filters by state FAILED", () => {
    const filtered = filterRuns(runs, { state: "FAILED" });
    expect(filtered).toHaveLength(2);
    expect(filtered.every((r) => r.state === "FAILED")).toBe(true);
  });

  it("filters by state SUCCEEDED", () => {
    const filtered = filterRuns(runs, { state: "SUCCEEDED" });
    expect(filtered).toHaveLength(1);
    expect(filtered[0]!.run_id).toBe("run_2");
  });

  it("filters by the remediation terminal-state set", () => {
    const filtered = filterRuns(runs, { states: ["FAILED", "COLLECTION_FAILED"] });
    expect(filtered.map((run) => run.run_id)).toEqual(["run_1", "run_3", "run_4"]);
  });

  it("returns empty when no matches", () => {
    expect(filterRuns(runs, { state: "CANCELLED" })).toEqual([]);
  });

  it("handles empty runs array", () => {
    expect(filterRuns([], { state: "FAILED" })).toEqual([]);
    expect(filterRuns([])).toEqual([]);
  });
});
