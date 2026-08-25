import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { AgentTask } from "./types";
import {
  AgentTaskPanel,
  agentTaskCancellation,
  agentTaskPollInterval,
} from "./AgentTaskPanel";

function task(overrides: Partial<AgentTask> = {}): AgentTask {
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
      evidence_refs: ["agent-task:task-1"],
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
  };
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
  it("renders linked Run, resources, evidence and terminal status", () => {
    const markup = renderPanel([task()]);

    expect(markup).toContain("1 CPU · 512 MiB · 0 GPU");
    expect(markup).toContain('/runs/run-agent-task?user=alice&amp;tab=overview');
    expect(markup).toContain("run-agent-task");
    expect(markup).toContain("agent-task:task-1");
    expect(markup).toContain("已成功");
    expect(markup).not.toContain("private-worker-id");
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
    const pending = task({ state: "pending", version: 8, result: null, linked_run_id: null });

    expect(agentTaskCancellation(pending)).toEqual({ taskId: "task-1", expectedVersion: 8 });
    expect(agentTaskCancellation(task())).toBeNull();
    expect(renderPanel([pending])).toContain("取消验证");
    expect(renderPanel([task()])).not.toContain("取消验证");
  });

  it("explains authentication pauses and renders stable task errors", () => {
    const markup = renderPanel([task({
      state: "auth_required",
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

