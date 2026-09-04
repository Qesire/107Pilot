import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { AgentTask } from "./types";
import {
  AgentTaskPanel,
  agentTaskCancellation,
  agentTaskPollInterval,
} from "./AgentTaskPanel";
import { agentTaskGateView, isAgentTaskGateVerified } from "./agentTaskGate";

function task(overrides: Record<string, unknown> = {}): AgentTask {
  return {
    schema_version: "pilot107.agent-task/v1",
    task_id: "task-1",
    owner: "alice",
    session_id: "session-1",
    turn_id: "turn-1",
    project_id: "project-1",
    workspace_id: "workspace-1",
    task_kind: "slurm_validation",
    state: "succeeded",
    version: 4,
    request_key: "validate-1",
    completion_policy: "evidence_required",
    gate_state: "completed",
    legacy_gate_unverified: false,
    schedule_receipt: {
      receipt_id: "receipt-1",
      task_id: "task-1",
      owner: "alice",
      session_id: "session-1",
      originating_turn_id: "turn-1",
      request_digest: "a".repeat(64),
      idempotency_key: "validation-op-1",
      run_id: "run-agent-task",
      submit_state: "submitted",
      slurm_job_id: "48123",
      resource_envelope_id: "envelope-1",
      workspace_revision: null,
      workspace_digest: "b".repeat(64),
      completion_policy: "evidence_required",
      created_at: "2026-08-25T12:00:10Z",
      legacy_boundary: true,
    },
    gate_receipt: {
      task_id: "task-1",
      run_id: "run-agent-task",
      run_terminal_state: "completed",
      evidence_state: "finalized",
      evidence_refs: ["evidence:verified"],
      evidence_digest: "c".repeat(64),
      integrity_verified_at: "2026-08-25T12:00:25Z",
      integrity_state: "verified",
      workspace_revision: null,
      workspace_digest: "b".repeat(64),
      legacy_boundary: true,
      capsule_ref: null,
      capsule_state: "not_required",
    },
    cancel_requested: false,
    resource_envelope: {
      partition: "CPU-RC",
      qos: "qos_cpu_rc",
      cpus: 1,
      memory_mib: 512,
      gpu_type: null,
      gpus: 0,
      walltime_seconds: 300,
      max_tasks: 1,
      max_submissions: 1,
      workspace_snapshot_digest: "a".repeat(64),
      expires_at: "2026-08-25T13:00:00Z",
      approved_by: "alice",
    },
    linked_run_id: "run-agent-task",
    result: {
      status: "succeeded",
      evidence_refs: ["legacy-result:must-not-be-authority"],
      error_code: null,
      message: null,
    },
    lease: {
      owner: "private-worker-id",
      expires_at: "2026-08-25T12:01:00Z",
      fencing_token: 7,
    },
    created_at: "2026-08-25T12:00:00Z",
    updated_at: "2026-08-25T12:00:30Z",
    ...overrides,
  } as unknown as AgentTask;
}

function renderPanel(tasks: AgentTask[]): string {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  client.setQueryData(["agent-tasks", "alice", "session-1"], { items: tasks });
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <AgentTaskPanel user="alice" sessionId="session-1" />
    </QueryClientProvider>,
  );
}

describe("AgentTaskPanel", () => {
  it("renders only terminal-gate Evidence as verified completion", () => {
    const markup = renderPanel([task()]);

    expect(markup).toContain("1 CPU · 512 MiB · 0 GPU");
    expect(markup).toContain('/runs/run-agent-task?user=alice&amp;tab=overview');
    expect(markup).toContain("Job 48123");
    expect(markup).toContain("验证完成");
    expect(markup).toContain("已验证 Evidence");
    expect(markup).toContain("evidence:verified");
    expect(markup).not.toContain("legacy-result:must-not-be-authority");
    expect(markup).not.toContain("private-worker-id");
  });

  it("renders a scheduling receipt as non-terminal while Evidence is pending", () => {
    const pendingEvidence = task({
      state: "running",
      gate_state: "awaiting_evidence",
      gate_receipt: null,
      result: null,
      schedule_receipt: {
        ...(task() as unknown as { schedule_receipt: Record<string, unknown> }).schedule_receipt,
        submit_state: "submitted",
      },
    });
    const markup = renderPanel([pendingEvidence]);

    expect(markup).toContain("收集 Evidence");
    expect(markup).toContain("调度回执");
    expect(markup).toContain("不代表验证完成");
    expect(markup).not.toContain("验证完成");
    expect(isAgentTaskGateVerified(pendingEvidence)).toBe(false);
  });

  it("fails closed for a legacy succeeded task without a terminal gate receipt", () => {
    const legacy = task({
      completion_policy: undefined,
      gate_state: undefined,
      schedule_receipt: undefined,
      gate_receipt: undefined,
      legacy_gate_unverified: true,
      result: {
        status: "succeeded",
        evidence_refs: ["legacy:evidence"],
        error_code: null,
        message: null,
      },
    });
    const markup = renderPanel([legacy]);

    expect(markup).toContain("旧记录·未核验");
    expect(markup).toContain("不能据此生成新的正式实验结论");
    expect(markup).not.toContain("已验证 Evidence");
    expect(agentTaskGateView(legacy).verifiedComplete).toBe(false);
  });

  it("requires a READY Capsule when the completion policy requires one", () => {
    const withoutCapsule = task({
      completion_policy: "evidence_and_capsule_required",
      gate_receipt: {
        ...(task() as unknown as { gate_receipt: Record<string, unknown> }).gate_receipt,
        capsule_ref: null,
        capsule_state: "not_required",
      },
    });
    expect(isAgentTaskGateVerified(withoutCapsule)).toBe(false);
    expect(renderPanel([withoutCapsule])).toContain("完成状态待核验");

    const withCapsule = task({
      completion_policy: "evidence_and_capsule_required",
      gate_receipt: {
        ...(task() as unknown as { gate_receipt: Record<string, unknown> }).gate_receipt,
        capsule_ref: "capsule:ready-1",
        capsule_state: "READY",
      },
    });
    const markup = renderPanel([withCapsule]);
    expect(isAgentTaskGateVerified(withCapsule)).toBe(true);
    expect(markup).toContain("验证完成");
    expect(markup).toContain("Capsule: capsule:ready-1");
  });

  it("polls pending, running and cancelling tasks but stops for terminal tasks", () => {
    expect(agentTaskPollInterval([task({ state: "pending", result: null })])).toBe(2_000);
    expect(agentTaskPollInterval([task({ state: "running", result: null })])).toBe(2_000);
    expect(agentTaskPollInterval([
      task({ state: "running", cancel_requested: true, result: null }),
    ])).toBe(2_000);
    expect(agentTaskPollInterval([task()])).toBe(false);
    expect(agentTaskPollInterval([
      task({ state: "auth_required", result: null, linked_run_id: null }),
    ])).toBe(false);
  });

  it("offers version-bound cancellation only while work is active", () => {
    const pending = task({
      state: "pending",
      gate_state: "pending",
      gate_receipt: null,
      version: 8,
      result: null,
      linked_run_id: null,
    });

    expect(agentTaskCancellation(pending)).toEqual({ taskId: "task-1", expectedVersion: 8 });
    expect(agentTaskCancellation(task())).toBeNull();
    expect(renderPanel([pending])).toContain("取消验证");
    expect(renderPanel([task()])).not.toContain("取消验证");
  });

  it("explains authentication pauses and renders stable task errors", () => {
    const markup = renderPanel([task({
      state: "auth_required",
      gate_state: "input_required",
      gate_receipt: null,
      cancel_requested: false,
      result: {
        status: "auth_required",
        evidence_refs: [],
        error_code: "AUTH.REQUIRED",
        message: "cluster authentication is required",
      },
    })]);

    expect(markup).toContain("集群认证已失效");
    expect(markup).toContain("AUTH.REQUIRED");
    expect(markup).toContain("cluster authentication is required");
  });
});
