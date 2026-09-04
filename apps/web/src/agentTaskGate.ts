import type { AgentTask } from "./types";

export type AgentTaskCompletionPolicy =
  | "evidence_required"
  | "evidence_and_capsule_required";

export type AgentTaskGateState =
  | "created"
  | "admitted"
  | "submitting"
  | "pending"
  | "running"
  | "awaiting_run_terminal"
  | "awaiting_evidence"
  | "awaiting_integrity"
  | "awaiting_capsule"
  | "completed"
  | "input_required"
  | "cancelling"
  | "cancelled"
  | "failed"
  | "blocked"
  | "orphaned";

interface AgentTaskScheduleReceiptWire {
  receipt_id: string;
  task_id: string;
  owner: string;
  session_id: string;
  originating_turn_id: string;
  request_digest: string;
  idempotency_key: string;
  run_id: string;
  submit_state: "admitted" | "submitting" | "pending" | "submitted" | "submission_uncertain";
  slurm_job_id: string | null;
  resource_envelope_id: string;
  workspace_revision: number | null;
  workspace_digest: string;
  completion_policy: AgentTaskCompletionPolicy;
  created_at: string;
  legacy_boundary: boolean;
}

interface AgentTaskGateReceiptWire {
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
  platform_snapshot_ref?: string;
  source_revision?: string;
  terminal_at?: string;
  seal_digest?: string;
  seal_marker_ref?: string;
}

type AgentTaskGateWire = AgentTask & {
  completion_policy?: AgentTaskCompletionPolicy;
  gate_state?: AgentTaskGateState;
  schedule_receipt?: AgentTaskScheduleReceiptWire | null;
  gate_receipt?: AgentTaskGateReceiptWire | null;
  legacy_gate_unverified?: boolean;
};

export interface AgentTaskGateView {
  readonly label: string;
  readonly tone: "info" | "success" | "warning" | "danger" | "neutral";
  readonly gateState: AgentTaskGateState | null;
  readonly completionPolicy: AgentTaskCompletionPolicy | null;
  readonly verifiedComplete: boolean;
  readonly evidenceRefs: readonly string[];
  readonly capsuleRef: string | null;
  readonly scheduleSummary: string | null;
  readonly slurmJobId: string | null;
  readonly legacyUnverified: boolean;
}

const gateStates = new Set<AgentTaskGateState>([
  "created",
  "admitted",
  "submitting",
  "pending",
  "running",
  "awaiting_run_terminal",
  "awaiting_evidence",
  "awaiting_integrity",
  "awaiting_capsule",
  "completed",
  "input_required",
  "cancelling",
  "cancelled",
  "failed",
  "blocked",
  "orphaned",
]);

const completionPolicies = new Set<AgentTaskCompletionPolicy>([
  "evidence_required",
  "evidence_and_capsule_required",
]);

export function agentTaskGateView(task: AgentTask): AgentTaskGateView {
  const wire = task as AgentTaskGateWire;
  const gateState = gateStates.has(wire.gate_state as AgentTaskGateState)
    ? wire.gate_state as AgentTaskGateState
    : null;
  const completionPolicy = completionPolicies.has(
    wire.completion_policy as AgentTaskCompletionPolicy,
  ) ? wire.completion_policy as AgentTaskCompletionPolicy : null;
  const legacyUnverified = wire.legacy_gate_unverified === true || gateState === null;
  const receipt = wire.gate_receipt;
  const evidenceRefs = receipt && Array.isArray(receipt.evidence_refs)
    ? receipt.evidence_refs.filter((value): value is string => typeof value === "string" && Boolean(value))
    : [];
  const capsuleSatisfied = completionPolicy !== "evidence_and_capsule_required" || (
    receipt?.capsule_state === "READY"
    && typeof receipt.capsule_ref === "string"
    && Boolean(receipt.capsule_ref)
  );
  const verifiedComplete = Boolean(
    !legacyUnverified
    && gateState === "completed"
    && task.state === "succeeded"
    && task.result?.status === "succeeded"
    && receipt
    && receipt.task_id === task.task_id
    && task.linked_run_id
    && receipt.run_id === task.linked_run_id
    && receipt.evidence_state === "finalized"
    && receipt.integrity_state === "verified"
    && evidenceRefs.length > 0
    && capsuleSatisfied
  );

  return {
    label: gateLabel(task, gateState, verifiedComplete),
    tone: gateTone(task, gateState, verifiedComplete),
    gateState,
    completionPolicy,
    verifiedComplete,
    evidenceRefs: verifiedComplete ? evidenceRefs : [],
    capsuleRef: verifiedComplete && receipt?.capsule_state === "READY"
      ? receipt.capsule_ref
      : null,
    scheduleSummary: scheduleSummary(wire.schedule_receipt),
    slurmJobId: wire.schedule_receipt?.slurm_job_id ?? null,
    legacyUnverified,
  };
}

export function isAgentTaskGateVerified(task: AgentTask): boolean {
  return agentTaskGateView(task).verifiedComplete;
}

export function agentTaskCompletionPolicyLabel(task: AgentTask): string {
  const policy = agentTaskGateView(task).completionPolicy;
  if (policy === "evidence_and_capsule_required") return "Evidence + Capsule";
  if (policy === "evidence_required") return "Evidence";
  return "旧版/未知";
}

function scheduleSummary(receipt: AgentTaskScheduleReceiptWire | null | undefined): string | null {
  if (!receipt) return null;
  return ({
    admitted: "验证请求已受理",
    submitting: "正在提交到 Slurm",
    pending: "Slurm 已受理，等待资源",
    submitted: "Slurm 已受理",
    submission_uncertain: "提交结果待权威调度事实确认",
  } as const)[receipt.submit_state] ?? "验证请求已受理";
}

function gateLabel(
  task: AgentTask,
  gateState: AgentTaskGateState | null,
  verifiedComplete: boolean,
): string {
  if (task.cancel_requested && (task.state === "pending" || task.state === "running")) {
    return "取消中";
  }
  if (verifiedComplete) return "验证完成";
  if (gateState === null) {
    if (task.state === "succeeded") return "旧记录·未核验";
    return legacyStateLabel(task.state);
  }
  return ({
    created: "准备验证",
    admitted: "验证已受理",
    submitting: "正在提交",
    pending: "Slurm 排队中",
    running: "Slurm 运行中",
    awaiting_run_terminal: "等待运行结束",
    awaiting_evidence: "收集 Evidence",
    awaiting_integrity: "校验 Evidence",
    awaiting_capsule: "等待 Capsule",
    completed: "完成状态待核验",
    input_required: "需要输入",
    cancelling: "取消中",
    cancelled: "已取消",
    failed: "验证失败",
    blocked: "验证受阻",
    orphaned: "运行状态失联",
  } as const)[gateState];
}

function gateTone(
  task: AgentTask,
  gateState: AgentTaskGateState | null,
  verifiedComplete: boolean,
): AgentTaskGateView["tone"] {
  if (task.cancel_requested) return "warning";
  if (verifiedComplete) return "success";
  if (gateState === "failed" || gateState === "orphaned" || task.state === "auth_required") {
    return "danger";
  }
  if (
    gateState === "blocked"
    || gateState === "input_required"
    || gateState === "awaiting_capsule"
    || (gateState === null && task.state === "succeeded")
  ) return "warning";
  if (gateState === "cancelled" || task.state === "cancelled") return "neutral";
  return "info";
}

function legacyStateLabel(state: AgentTask["state"]): string {
  return ({
    pending: "等待调度",
    running: "运行中",
    succeeded: "旧记录·未核验",
    failed: "已失败",
    cancelled: "已取消",
    auth_required: "需要认证",
  } as const)[state];
}
