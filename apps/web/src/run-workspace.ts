export type RunWorkspaceOutcomeKind =
  | "failed"
  | "collection_failed"
  | "collecting"
  | "succeeded"
  | "running"
  | "queued";

export type RunWorkspaceAttentionSeverity = "critical" | "warning" | "info" | "none";

export type RunWorkspaceNextActionKind =
  | "prepare_repair"
  | "inspect_failure"
  | "inspect_collection"
  | "wait_collection"
  | "view_results"
  | "watch_run"
  | "watch_queue";

export type EvidenceTab =
  | "overview"
  | "timeline"
  | "compare"
  | "logs"
  | "results"
  | "diagnosis"
  | "capsule"
  | "objects";

export interface RunWorkspace {
  run: {
    run_id: string;
    owner: string;
    job_name: string | null;
    created_at: string;
    updated_at: string;
    exit_code: string | null;
    terminal_state: string | null;
    attempt: number;
  };
  states: {
    execution: string;
    collection: string;
    diagnosis: string;
    capsule: string;
    result: string;
  };
  outcome: {
    kind: RunWorkspaceOutcomeKind;
    summary: string;
  };
  attention: {
    severity: RunWorkspaceAttentionSeverity;
    title: string | null;
    detail: string | null;
  };
  next_action: {
    kind: RunWorkspaceNextActionKind;
    label: string;
    detail: string;
  };
  evidence_summary: {
    object_count: number;
    result_count: number;
    diagnosis_count: number;
    stdout_available: boolean;
    stderr_available: boolean;
    capsule_available: boolean;
  };
  provenance: {
    contract_id: string | null;
    contract_digest: string | null;
    workdir: string | null;
    job_id: string | null;
    parent_run_id: string | null;
    lineage_reason: string | null;
    remediation_plan_id: string | null;
    workspace_revision: number | null;
    workspace_digest: string | null;
    source_revision: string | null;
    platform_snapshot_ref: string | null;
  };
}

export interface RunWorkspaceTabRequirements {
  evidence: boolean;
  diagnoses: boolean;
  capsule: boolean;
  health: boolean;
}

export function runWorkspaceTabRequirements(tab: EvidenceTab): RunWorkspaceTabRequirements {
  if (tab === "overview" || tab === "timeline") {
    return { evidence: false, diagnoses: false, capsule: false, health: false };
  }
  if (tab === "diagnosis") {
    return { evidence: true, diagnoses: true, capsule: false, health: true };
  }
  if (tab === "capsule") {
    return { evidence: true, diagnoses: false, capsule: true, health: false };
  }
  return { evidence: true, diagnoses: false, capsule: false, health: false };
}

export function runWorkspaceNextTab(kind: RunWorkspaceNextActionKind): EvidenceTab {
  switch (kind) {
    case "prepare_repair":
      return "diagnosis";
    case "inspect_failure":
      return "logs";
    case "inspect_collection":
      return "objects";
    case "wait_collection":
    case "view_results":
      return "results";
    case "watch_run":
      return "logs";
    case "watch_queue":
      return "overview";
  }
}
