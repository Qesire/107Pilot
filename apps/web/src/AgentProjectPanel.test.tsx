import { describe, expect, it } from "vitest";
import {
  buildValidationEnvelope,
  buildFormalContract,
  boundProjectSessionId,
  canGenerateFormalCandidate,
  changeSetStateLabel,
  formalCandidateDefaults,
  formalCandidateTask,
  changeSetTone,
  isValidationEnvelopeInputValid,
  isChangeSetPublishable,
  originLabel,
  projectAgentProfileBinding,
  riskLabel,
} from "./AgentProjectPanel";
import type { AgentTask, FormalRunCandidate, WorkspaceChangeSet } from "./types";

function changeSet(state: WorkspaceChangeSet["state"]): WorkspaceChangeSet {
  return {
    schema_version: "pilot107.workspace-changeset/v1",
    change_set_id: "changeset-1",
    project_id: "project-1",
    workspace_id: "workspace-1",
    owner: "alice",
    base_snapshot_digest: "a".repeat(64),
    digest: "b".repeat(64),
    state,
    version: 1,
    files: [],
    sandbox_results: [],
    approval: null,
    created_at: "2026-08-19T00:00:00Z",
    updated_at: "2026-08-19T00:00:00Z",
  };
}

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
    version: 2,
    request_key: "validation-1",
    cancel_requested: false,
    resource_envelope: {
      partition: "CPU-RC",
      qos: "qos_cpu_rc",
      cpus: 4,
      memory_mib: 4096,
      gpu_type: null,
      gpus: 0,
      walltime_seconds: 600,
      max_tasks: 1,
      max_submissions: 1,
      workspace_snapshot_digest: "a".repeat(64),
      expires_at: "2026-08-25T13:00:00Z",
      approved_by: "alice",
    },
    linked_run_id: "run-1",
    result: {
      status: "succeeded",
      evidence_refs: ["evidence:1"],
      error_code: null,
      message: null,
    },
    lease: null,
    created_at: "2026-08-25T12:00:00Z",
    updated_at: "2026-08-25T12:01:00Z",
    ...overrides,
  };
}

describe("Agent Project review presentation", () => {
  it("mounts task lifecycle only for the session bound to the selected Project", () => {
    expect(boundProjectSessionId(
      new URLSearchParams("project=project-1&session=session-1"),
      "project-1",
    )).toBe("session-1");
    expect(boundProjectSessionId(
      new URLSearchParams("project=project-2&session=session-1"),
      "project-1",
    )).toBeNull();
    expect(boundProjectSessionId(new URLSearchParams("project=project-1"), "project-1"))
      .toBeNull();
  });

  it("labels project origins and explicit risk levels", () => {
    expect(originLabel("blank")).toBe("空白");
    expect(originLabel("existing")).toBe("现有目录");
    expect(riskLabel("low")).toBe("低风险");
    expect(riskLabel("high")).toBe("高风险");
  });

  it("binds failed Run Projects to the repair profile and approved session", () => {
    expect(projectAgentProfileBinding({
      origin: "failed_run",
      projectId: "project-repair",
      workspaceId: "workspace-repair",
      sourceRunId: "run-failed",
      remediationSessionId: "remsession-repair",
    })).toEqual({
      profile_id: "run_diagnosis_repair",
      source: {
        project_id: "project-repair",
        workspace_id: "workspace-repair",
        run_id: "run-failed",
        remediation_session_id: "remsession-repair",
      },
    });
  });

  it("distinguishes reviewable and failed ChangeSets", () => {
    expect(changeSetStateLabel("reviewable")).toBe("可审阅");
    expect(changeSetTone(changeSet("reviewable"))).toBe("success");
    expect(changeSetTone(changeSet("failed"))).toBe("danger");
    expect(isChangeSetPublishable(changeSet("reviewable"))).toBe(true);
    expect(isChangeSetPublishable(changeSet("published"))).toBe(false);
    expect(isChangeSetPublishable(changeSet("conflicted"))).toBe(false);
  });

  it("binds an approved validation envelope to the current Workspace snapshot", () => {
    const envelope = buildValidationEnvelope({
      owner: "alice",
      snapshotDigest: "a".repeat(64),
      partition: "debug",
      qos: "normal",
      cpus: 2,
      memoryMib: 2048,
      gpus: 0,
      walltimeSeconds: 300,
      now: new Date("2026-08-19T00:00:00Z"),
    });

    expect(envelope.workspace_snapshot_digest).toBe("a".repeat(64));
    expect(envelope.approved_by).toBe("alice");
    expect(envelope.expires_at).toBe("2026-08-19T01:00:00.000Z");
    expect(envelope.max_submissions).toBe(1);
  });

  it("rejects invalid or fractional validation resource inputs", () => {
    expect(isValidationEnvelopeInputValid({
      cpus: 1,
      memoryMib: 1024,
      gpus: 0,
      walltimeSeconds: 300,
    })).toBe(true);
    expect(isValidationEnvelopeInputValid({
      cpus: 0,
      memoryMib: 1024,
      gpus: 0,
      walltimeSeconds: 300,
    })).toBe(false);
    expect(isValidationEnvelopeInputValid({
      cpus: 1.5,
      memoryMib: 1024,
      gpus: 0,
      walltimeSeconds: 300,
    })).toBe(false);
  });

  it("materializes formal resources into an approval-ready Contract", () => {
    const contract = buildFormalContract({
      name: "formal test",
      workdir: "/public/home/alice/project",
      command: "python main.py",
      partition: "debug",
      qos: "normal",
      cpus: 2,
      memoryMib: 4096,
      gpus: 0,
      gpuType: "a100",
      walltimeSeconds: 3661,
    });
    expect(contract.resources).toMatchObject({
      cpus_per_task: 2,
      memory: "4096M",
      time_limit: "01:01:01",
    });
  });

  it("selects only the newest successful task bound to the current approval scope", () => {
    const failed = task({ task_id: "task-failed", state: "failed", updated_at: "2026-08-25T12:05:00Z" });
    const otherProject = task({ task_id: "task-other", project_id: "project-2", updated_at: "2026-08-25T12:06:00Z" });
    const older = task({ task_id: "task-older", updated_at: "2026-08-25T12:02:00Z" });
    const newest = task({ task_id: "task-newest", updated_at: "2026-08-25T12:04:00Z" });
    const binding = { projectId: "project-1", workspaceId: "workspace-1", sessionId: "session-1" };

    expect(formalCandidateTask([failed, otherProject, older, newest], binding)?.task_id)
      .toBe("task-newest");
    expect(canGenerateFormalCandidate(changeSet("reviewable"), newest, binding)).toBe(false);
    expect(canGenerateFormalCandidate(changeSet("published"), newest, binding)).toBe(true);
  });

  it("materializes server-derived lineage and scientific formal defaults", () => {
    const candidate: FormalRunCandidate = {
      validation_task_id: "task-1",
      validation_contract_id: "contract-validation",
      validation_run_id: "run-validation",
      validation_evidence_refs: ["evidence:one", "evidence:two"],
      published_workdir: "/public/home/alice/heat-demo",
      default_command: "bash scripts/run_experiment.sh",
      resource_hints: {
        partition: "CPU-RC",
        qos: "qos_cpu_rc",
        cpus_per_task: 4,
        memory_mib: 4096,
        gpus: 0,
        time_limit: "00:10:00",
      },
    };

    expect(formalCandidateDefaults(candidate)).toEqual({
      workdir: "/public/home/alice/heat-demo",
      command: "bash scripts/run_experiment.sh",
      partition: "CPU-RC",
      qos: "qos_cpu_rc",
      cpus: 4,
      memoryMib: 4096,
      gpus: 0,
      walltimeSeconds: 600,
    });
  });
});
