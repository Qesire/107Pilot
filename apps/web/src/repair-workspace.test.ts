import { describe, expect, it } from "vitest";
import {
  type RepairWorkspace,
  repairWorkspacePollInterval,
} from "./repair-workspace";

function workspace(
  overrides: Partial<RepairWorkspace["status"]> = {},
  derivedState?: string,
): RepairWorkspace {
  return {
    schema_version: "pilot107.repair-workspace/v1",
    source_run: {
      run_id: "run-source",
      owner: "alice",
      state: "FAILED",
      collection_state: "succeeded",
      diagnosis_state: "succeeded",
      result_status: "invalid",
      contract_id: "contract-1",
      updated_at: "2026-09-04T00:00:00Z",
    },
    diagnoses: [],
    remediation_sessions: [],
    agent: { advice: [], decisions: [], executions: [], truncated: false },
    repair_tickets: [],
    derived_runs: derivedState ? [{
      run_id: "run-derived",
      state: derivedState,
      collection_state: "pending",
      result_status: "unknown",
      lineage_reason: "agent_remediation",
      remediation_plan_id: "advice-1:action-1",
      attempt: 1,
      job_id: null,
      created_at: "2026-09-04T00:01:00Z",
      updated_at: "2026-09-04T00:01:00Z",
    }] : [],
    truncation: {
      remediation_sessions: false,
      agent_advice: false,
      repair_tickets: false,
    },
    status: {
      has_repair_activity: false,
      awaiting_approval: false,
      has_derived_run: Boolean(derivedState),
      has_successful_derived_run: false,
      ...overrides,
    },
    next_action: {
      kind: "inspect_failure",
      label: "查看失败证据",
      detail: "inspect",
    },
  };
}

describe("repairWorkspacePollInterval", () => {
  it("polls while the initial projection is loading", () => {
    expect(repairWorkspacePollInterval(undefined)).toBe(5_000);
  });

  it("polls while human approval is pending", () => {
    expect(repairWorkspacePollInterval(workspace({ awaiting_approval: true }))).toBe(5_000);
  });

  it("polls while a derived run is active", () => {
    expect(repairWorkspacePollInterval(workspace({}, "RUNNING"))).toBe(5_000);
  });

  it("stops polling once the repair graph is stable", () => {
    expect(repairWorkspacePollInterval(workspace({}, "SUCCEEDED"))).toBe(false);
  });
});
