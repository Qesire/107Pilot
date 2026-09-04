import { describe, expect, it } from "vitest";
import {
  buildValidationEnvelope,
  builderSessionLocation,
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
  validationResourceDefaults,
} from "./AgentProjectPanel";
import type {
  AgentTask,
  CapabilityProfile,
  EntitlementSnapshot,
  FormalRunCandidate,
  WorkspaceChangeSet,
} from "./types";

type GateReceiptFixture = {
  task_id: string;
  run_id: string;
  run_terminal_state: string;
  evidence_state: string;
  evidence_refs: string[];
  evidence_digest: string;
  integrity_verified_at: string;
  integrity_state: string;
  workspace_revision: number | null;
  workspace_digest: string;
  legacy_boundary: boolean;
  capsule_ref: string | null;
  capsule_state: string;
};

type GateTaskFixture = AgentTask & {
  completion_policy: "evidence_required" | "evidence_and_capsule_required";
  gate_state: "completed" | "awaiting_evidence" | "awaiting_integrity" | "awaiting_capsule";
  gate_receipt: GateReceiptFixture | null;
  legacy_gate_unverified: boolean;
};

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

function gateReceipt(input: {
  taskId: string;
  runId: string;
  evidenceRefs: string[];
}): GateReceiptFixture {
  return {
    task_id: input.taskId,
    run_id: input.runId,
    run_terminal_state: "completed",
    evidence_state: "finalized",
    evidence_refs: input.evidenceRefs,
    evidence_digest: "d".repeat(64),
    integrity_verified_at: "2026-08-25T12:01:00Z",
    integrity_state: "verified",
    workspace_revision: null,
    workspace_digest: "a".repeat(64),
    legacy_boundary: false,
    capsule_ref: null,
    capsule_state: "not_required",
  };
}

function task(overrides: Partial<GateTaskFixture> = {}): GateTaskFixture {
  const taskId = overrides.task_id ?? "task-1";
  const linkedRunId = overrides.linked_run_id ?? "run-1";
  const result = overrides.result ?? {
    status: "succeeded" as const,
    evidence_refs: ["evidence:1"],
    error_code: null,
    message: null,
  };
  const evidenceRefs = result.status === "succeeded" ? result.evidence_refs : ["evidence:1"];
  return {
    schema_version: "pilot107.agent-task/v1",
    task_id: taskId,
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
    linked_run_id: linkedRunId,
    result,
    lease: null,
    created_at: "2026-08-25T12:00:00Z",
    updated_at: "2026-08-25T12:01:00Z",
    completion_policy: "evidence_required",
    gate_state: "completed",
    gate_receipt: gateReceipt({
      taskId,
      runId: linkedRunId ?? "run-1",
      evidenceRefs,
    }),
    legacy_gate_unverified: false,
    ...overrides,
  };
}

describe("Agent Project review presentation", () => {
  it("keeps an experiment Builder session inside the engineering entry", () => {
    expect(builderSessionLocation(
      new URLSearchParams("mode=builder&project=project-old"),
      "project-1",
      "session-builder",
    )).toBe("/agent?mode=builder&project=project-1&session=session-builder");
  });

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

  it("derives validation defaults from fresh authoritative Slurm entitlement", () => {
    const capabilities: CapabilityProfile = {
      profile_id: "cpu-only-8c16g-vm-demo",
      source_authority: "vm-demo",
      captured_at: "2026-08-30T00:00:00Z",
      freshness_seconds: 300,
      default_partition: "CPU-RC",
      default_qos: "qos_cpu_rc",
      partitions: [{ name: "CPU-RC", allow_qos: ["qos_cpu_rc"] }],
      qos: [{ name: "qos_cpu_rc" }],
      dynamic_facts: [],
      limitations: [],
    };
    const entitlement: EntitlementSnapshot = {
      snapshot_id: "entitlement-1",
      captured_at: "2026-08-30T00:01:00Z",
      freshness: "fresh",
      data_quality: "authoritative",
      default_account: "competition",
      snapshot: {
        associations: [{
          account: "competition",
          partition: null,
          qos: ["qos_cpu_rc"],
          default_qos: "qos_cpu_rc",
        }],
      },
    };

    expect(validationResourceDefaults(capabilities, entitlement)).toEqual({
      partition: "CPU-RC",
      qos: "qos_cpu_rc",
    });
  });

  it("falls back to capability defaults when Slurm entitlement is stale", () => {
    const capabilities = {
      default_partition: "CPU-RC",
      default_qos: "qos_cpu_rc",
      partitions: [{ name: "CPU-RC", allow_qos: ["qos_cpu_rc"] }],
      qos: [{ name: "qos_cpu_rc" }],
    } as CapabilityProfile;
    const entitlement = {
      freshness: "stale",
      data_quality: "authoritative",
      snapshot: {
        associations: [{
          account: "legacy",
          partition: "debug",
          qos: ["normal"],
          default_qos: "normal",
        }],
      },
    } as EntitlementSnapshot;

    expect(validationResourceDefaults(capabilities, entitlement)).toEqual({
      partition: "CPU-RC",
      qos: "qos_cpu_rc",
    });
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

  it("selects only the newest verified task bound to the current approval scope", () => {
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

  it("rejects legacy success and nonterminal gate projections", () => {
    const binding = { projectId: "project-1", workspaceId: "workspace-1", sessionId: "session-1" };
    const legacy = task({
      task_id: "task-legacy",
      gate_receipt: null,
      legacy_gate_unverified: true,
    });
    const awaitingEvidence = task({
      task_id: "task-awaiting",
      gate_state: "awaiting_evidence",
    });

    expect(formalCandidateTask([legacy, awaitingEvidence], binding)).toBeNull();
    expect(canGenerateFormalCandidate(changeSet("published"), legacy, binding)).toBe(false);
  });

  it("rejects a succeeded task whose legacy result disagrees with the trusted gate", () => {
    const binding = { projectId: "project-1", workspaceId: "workspace-1", sessionId: "session-1" };
    const mismatch = task({
      task_id: "task-mismatch",
      result: {
        status: "succeeded",
        evidence_refs: ["evidence:legacy"],
        error_code: null,
        message: null,
      },
      gate_receipt: gateReceipt({
        taskId: "task-mismatch",
        runId: "run-1",
        evidenceRefs: ["evidence:trusted"],
      }),
    });

    expect(formalCandidateTask([mismatch], binding)).toBeNull();
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
