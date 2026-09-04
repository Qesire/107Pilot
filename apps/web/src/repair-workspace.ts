import { useQuery } from "@tanstack/react-query";
import { ApiRequestError, describeApiError } from "./api";

export type RepairWorkspaceNextActionKind =
  | "review_proposal"
  | "watch_derived_run"
  | "compare_outcome"
  | "continue_repair"
  | "start_repair"
  | "inspect_failure"
  | "no_repair_needed";

export interface RepairWorkspaceDiagnosis {
  diagnosis_id: string;
  rule_id: string;
  severity: string;
  summary: string;
  evidence_refs: string[];
  retryable: boolean;
  confidence: string;
  category: string | null;
  stage: string | null;
}

export interface RepairWorkspaceSession {
  session_id: string;
  state: string;
  version: number;
  automation_policy: string;
  provider: string;
  budget: Record<string, number>;
  usage: Record<string, number>;
  created_at: string;
  updated_at: string;
}

export interface RepairWorkspaceAdvice {
  advice_id: string;
  state: string;
  version: number;
  provider: string;
  model: string | null;
  evidence_bundle_sha256: string;
  source_run_updated_at: string;
  created_at: string;
  updated_at: string;
}

export interface RepairWorkspaceDecision {
  decision_id: number;
  advice_id: string;
  decision: string;
  actor: string;
  action_ids: string[];
  advice_version: number;
  created_at: string;
}

export interface RepairWorkspaceExecution {
  execution_id: string;
  advice_id: string;
  action_id: string;
  state: string;
  submit_requested: boolean;
  derived_contract_id: string | null;
  derived_run_id: string | null;
  error_code: string | null;
  execution_phase: string | null;
  created_at: string;
  updated_at: string;
}

export interface RepairWorkspaceTicket {
  ticket_id: string;
  state: string;
  session_id: string | null;
  source_contract_id: string | null;
  diagnosis_ids: string[];
  requested_change: string | null;
  resolution_manifest_id: string | null;
  resolution_run_id: string | null;
  abandon_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface RepairWorkspaceDerivedRun {
  run_id: string;
  state: string;
  collection_state: string;
  result_status: string;
  lineage_reason: string | null;
  remediation_plan_id: string | null;
  attempt: number;
  job_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface RepairWorkspace {
  schema_version: "pilot107.repair-workspace/v1";
  source_run: {
    run_id: string;
    owner: string;
    state: string;
    collection_state: string;
    diagnosis_state: string;
    result_status: string;
    contract_id: string | null;
    updated_at: string;
  };
  diagnoses: RepairWorkspaceDiagnosis[];
  remediation_sessions: RepairWorkspaceSession[];
  agent: {
    advice: RepairWorkspaceAdvice[];
    decisions: RepairWorkspaceDecision[];
    executions: RepairWorkspaceExecution[];
    truncated: boolean;
  };
  repair_tickets: RepairWorkspaceTicket[];
  derived_runs: RepairWorkspaceDerivedRun[];
  truncation: {
    remediation_sessions: boolean;
    agent_advice: boolean;
    repair_tickets: boolean;
  };
  status: {
    has_repair_activity: boolean;
    awaiting_approval: boolean;
    has_derived_run: boolean;
    has_successful_derived_run: boolean;
  };
  next_action: {
    kind: RepairWorkspaceNextActionKind;
    label: string;
    detail: string;
  };
}

const ACTIVE_DERIVED_RUN_STATES = new Set([
  "DRAFT",
  "VALIDATED",
  "SUBMITTING",
  "SUBMITTED",
  "PENDING",
  "RUNNING",
  "COMPLETING",
  "UNKNOWN",
]);

export function repairWorkspacePollInterval(
  workspace: RepairWorkspace | undefined,
): number | false {
  if (!workspace) return 5_000;
  if (workspace.status.awaiting_approval) return 5_000;
  if (workspace.derived_runs.some((run) => ACTIVE_DERIVED_RUN_STATES.has(run.state))) {
    return 5_000;
  }
  return false;
}

async function fetchRepairWorkspace(
  user: string,
  runId: string,
  signal?: AbortSignal,
): Promise<RepairWorkspace> {
  const request: RequestInit = {
    method: "GET",
    headers: {
      Accept: "application/json",
      "X-Pilot107-User": user,
    },
  };
  if (signal) request.signal = signal;
  const response = await fetch(
    `/api/v1/runs/${encodeURIComponent(runId)}/repair-workspace`,
    request,
  );
  const payload = (await response.json().catch(() => ({}))) as RepairWorkspace & {
    error?: { code?: string; message?: string };
  };
  if (!response.ok) {
    const code = payload.error?.code ?? `HTTP.${response.status}`;
    const message = describeApiError(code, payload.error?.message ?? response.statusText);
    throw new ApiRequestError(response.status, code, message);
  }
  return payload;
}

export function useRepairWorkspace(user: string, runId: string | null) {
  return useQuery({
    queryKey: ["repair-workspace", user, runId],
    queryFn: ({ signal }) => fetchRepairWorkspace(user, runId ?? "", signal),
    enabled: Boolean(runId),
    refetchInterval: (query) => repairWorkspacePollInterval(query.state.data),
  });
}
