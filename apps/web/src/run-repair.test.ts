import { describe, expect, it } from "vitest";
import {
  approvedProposalIds,
  canOpenRepair,
  derivedRunHref,
  repairProjectHref,
  sessionsForRun,
} from "./run-repair";
import type { RemediationSession } from "./types";
import type { RunWorkspace } from "./run-workspace";

function session(overrides: Partial<RemediationSession> = {}): RemediationSession {
  return {
    session_id: "repair_1",
    owner: "alice",
    state: "awaiting_approval",
    version: 2,
    source_run_id: "run_failed",
    source_contract_id: "contract_1",
    source_diagnosis_digest: "diag",
    source_evidence_digest: "evidence",
    automation_policy: "manual_approval",
    provider: "none",
    lease: { owner: null, expires_at: null },
    budget: {
      max_attempts: 3,
      max_submissions: 1,
      max_wall_time_seconds: 900,
      max_llm_calls: 3,
      max_llm_tokens: 4000,
    },
    usage: {
      attempts: 0,
      submissions: 0,
      wall_time_seconds: 0,
      llm_calls: 0,
      llm_tokens: 0,
    },
    stop_reason: null,
    takeover_reason: null,
    turns: [],
    proposals: [],
    decisions: [],
    executions: [],
    evaluations: [],
    created_at: "2026-09-04T01:00:00Z",
    updated_at: "2026-09-04T01:00:00Z",
    ...overrides,
  };
}

function workspace(overrides: Partial<RunWorkspace> = {}): RunWorkspace {
  return {
    run: {
      run_id: "run_failed",
      owner: "alice",
      job_name: "failed job",
      created_at: "2026-09-04T00:00:00Z",
      updated_at: "2026-09-04T01:00:00Z",
      exit_code: "1:0",
      terminal_state: "FAILED",
      attempt: 1,
    },
    states: {
      execution: "FAILED",
      collection: "succeeded",
      diagnosis: "succeeded",
      capsule: "none",
      result: "failed",
    },
    outcome: { kind: "failed", summary: "计算失败。" },
    attention: { severity: "critical", title: "依赖缺失", detail: null },
    next_action: {
      kind: "prepare_repair",
      label: "准备修复",
      detail: "已有持久化诊断。",
    },
    evidence_summary: {
      object_count: 2,
      result_count: 0,
      diagnosis_count: 1,
      stdout_available: true,
      stderr_available: true,
      capsule_available: false,
    },
    provenance: {
      contract_id: "contract_1",
      contract_digest: null,
      workdir: "/work/alice/demo",
      job_id: "123",
      parent_run_id: null,
      lineage_reason: null,
      remediation_plan_id: null,
      workspace_revision: null,
      workspace_digest: null,
      source_revision: null,
      platform_snapshot_ref: null,
    },
    ...overrides,
  };
}

describe("run-local repair helpers", () => {
  it("filters by source run and orders newest first", () => {
    const items = sessionsForRun([
      session({ session_id: "old", updated_at: "2026-09-04T01:00:00Z" }),
      session({ session_id: "other", source_run_id: "run_other", updated_at: "2026-09-04T03:00:00Z" }),
      session({ session_id: "new", updated_at: "2026-09-04T02:00:00Z" }),
    ], "run_failed");
    expect(items.map((item) => item.session_id)).toEqual(["new", "old"]);
  });

  it("only opens repair when the workspace has a persisted diagnosis", () => {
    expect(canOpenRepair(workspace())).toBe(true);
    expect(canOpenRepair(workspace({
      evidence_summary: { ...workspace().evidence_summary, diagnosis_count: 0 },
    }))).toBe(false);
    expect(canOpenRepair(workspace({
      next_action: { kind: "inspect_failure", label: "检查失败", detail: "先看证据" },
    }))).toBe(false);
  });

  it("derives approved proposal ids from durable decisions", () => {
    const ids = approvedProposalIds(session({
      decisions: [
        { decision_id: "d1", proposal_id: "p1", actor: "alice", decision: "approve", note: null, created_at: "2026-09-04T01:00:00Z" },
        { decision_id: "d2", proposal_id: "p2", actor: "alice", decision: "reject", note: null, created_at: "2026-09-04T01:01:00Z" },
      ],
    }));
    expect([...ids]).toEqual(["p1"]);
  });

  it("keeps repair project and derived run links bound to the source run", () => {
    expect(derivedRunHref("alice", "run failed", "run derived")).toBe(
      "/runs/run%20derived?user=alice&tab=compare&compare=run%20failed",
    );
    expect(repairProjectHref("alice", "project 1", "session 1", "run failed")).toContain(
      "repair_run=run+failed",
    );
  });
});
