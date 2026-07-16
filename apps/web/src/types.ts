export type RunState =
  | "DRAFT"
  | "VALIDATED"
  | "SUBMITTING"
  | "SUBMITTED"
  | "PENDING"
  | "RUNNING"
  | "COMPLETING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED"
  | "UNKNOWN"
  | "SUBMIT_FAILED"
  | "COLLECTION_FAILED"
  | "AUTH_REQUIRED"
  | "SUBMISSION_UNCERTAIN"
  | "EVIDENCE_PARTIAL";

export interface WebSession {
  identity_mode: "demo" | "fixed_user" | string;
  user: string;
  switchable: boolean;
}

export interface RunSummary {
  run_id: string;
  contract_id: string | null;
  owner: string;
  state: RunState;
  collection_state: string;
  diagnosis_state: string;
  capsule_state: string;
  result_status: string;
  job_id: string | null;
  exit_code: string | null;
  terminal_state?: string | null;
  parent_run_id?: string | null;
  lineage_reason?: string | null;
  attempt?: number;
  workdir?: string | null;
  recipe_version_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface EvidenceTask {
  task_id: number;
  task_type: string;
  state: string;
  attempts: number;
  updated_at: string;
}

export interface EvidenceObject {
  object_id: string;
  category: string;
  logical_path: string;
  source_uri: string | null;
  sha256: string | null;
  size_bytes: number | null;
  mime_type: string | null;
  collection_status: string;
  mutable_during_run: boolean;
  finalized_at: string | null;
}

export interface EvidenceTreeNode {
  name: string;
  kind: "directory" | "file" | string;
  logical_path: string;
  size_bytes?: number;
  sha256?: string;
  content_type?: string;
  children?: EvidenceTreeNode[];
}

export interface RunEvidence {
  run_id: string;
  owner: string;
  job_id: string | null;
  run_state: RunState;
  collection_state: string;
  tasks: EvidenceTask[];
  objects: EvidenceObject[];
  tree: EvidenceTreeNode;
}

export interface EvidenceObjectPreview extends EvidenceObject {
  preview: {
    available: boolean;
    reason?: string;
    content?: string;
    encoding?: string;
    bytes_read?: number;
    max_bytes: number;
    truncated?: boolean;
    integrity?: "verified" | "mismatch" | "not_checked" | string;
  };
}

export interface DiagnosisRecordPayload {
  diagnosis_id: string;
  run_id: string;
  rule_id: string;
  severity: string;
  summary: string;
  evidence_refs: string[];
  suggested_patch: JsonObject;
  retryable: boolean;
  confidence: string;
  category: string | null;
  stage: string | null;
  fix_guide: Record<string, string>;
  created_at: string;
}

export interface RunDiagnoses {
  run_id: string;
  diagnosis_state: string;
  items: DiagnosisRecordPayload[];
}

export interface RawCapsuleDetail {
  run_id: string;
  capsule_id: string;
  manifest_sha256: string;
  files_copied: number;
  valid?: boolean;
  checked_files?: number;
  manifest?: JsonObject;
  warnings: string[];
  errors?: string[];
}

export interface RunCapsule extends RunSummary {
  capsule: RawCapsuleDetail | null;
}

export interface PageInfo {
  limit: number;
  has_more: boolean;
  next_cursor: string | null;
}

export interface PagePayload<T> {
  items: T[];
  page: PageInfo;
}

export interface PartitionCapability {
  name: string;
  nodes?: string;
  total_nodes?: number;
  state?: string[];
  allow_qos?: string[];
  gpu_types?: string[];
}

export interface QosCapability {
  name: string;
  max_cpus?: number | null;
  max_gpus?: number | null;
  max_memory_gb?: number | null;
  max_wall_hours?: number | null;
  source_authority?: string;
}

export interface CapabilityProfile {
  profile_id: string;
  source_authority: string;
  captured_at: string;
  freshness_seconds: number;
  default_partition: string;
  default_qos: string;
  partitions: PartitionCapability[];
  qos: QosCapability[];
  dynamic_facts: string[];
  limitations: string[];
  snapshot_ref?: {
    snapshot_id?: string;
    freshness?: string;
    observed_at?: string;
  } | null;
}

export interface PlatformSnapshot {
  snapshot_id: string;
  scope: string;
  source_type: string;
  observed_at: string;
  freshness: string;
  data_quality: string;
  facts?: Record<string, unknown>;
  limitations?: string[];
}

export interface EntitlementSnapshot {
  snapshot_id: string;
  observed_at: string;
  freshness: string;
  data_quality: string;
  default_account?: string | null;
  associations?: Array<{
    account?: string;
    partition?: string | null;
    qos?: string[];
  }>;
}

export interface HealthReady {
  status: string;
  checks?: Record<string, { status?: string; detail?: string }>;
}

export type JsonObject = Record<string, unknown>;

export type RemediationState =
  | "waiting_evidence"
  | "diagnosing"
  | "planning"
  | "awaiting_input"
  | "awaiting_approval"
  | "ready"
  | "preparing"
  | "executing"
  | "evaluating"
  | "succeeded"
  | "exhausted"
  | "blocked"
  | "failed"
  | "cancelled";

export interface RemediationBudget {
  max_attempts: number;
  max_submissions: number;
  max_wall_time_seconds: number;
  max_llm_calls: number;
  max_llm_tokens: number;
}

export interface RemediationUsage {
  attempts: number;
  submissions: number;
  wall_time_seconds: number;
  llm_calls: number;
  llm_tokens: number;
}

export interface RemediationProposal {
  proposal_id: string;
  turn_id: string;
  action_id: string;
  action_type: string;
  source: string;
  risk: string;
  approval_required: boolean;
  policy_status: string;
  payload: JsonObject;
  created_at: string;
}

export interface RemediationDecision {
  decision_id: string;
  proposal_id: string;
  actor: string;
  decision: string;
  note: string | null;
  created_at: string;
}

export interface RemediationExecution {
  execution_id: string;
  proposal_id: string;
  state: string;
  derived_contract_id: string | null;
  derived_run_id: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface RemediationEvaluation {
  evaluation_id: string;
  execution_id: string;
  source_run_id: string;
  derived_run_id: string;
  outcome: string;
  checks: JsonObject[];
  comparison: JsonObject;
  evidence_refs: string[];
  created_at: string;
}

export interface RemediationSession {
  session_id: string;
  owner: string;
  state: RemediationState;
  version: number;
  source_run_id: string;
  source_contract_id: string | null;
  source_diagnosis_digest: string;
  source_evidence_digest: string;
  automation_policy: string;
  budget: RemediationBudget;
  usage: RemediationUsage;
  stop_reason: string | null;
  takeover_reason: string | null;
  turns: JsonObject[];
  proposals: RemediationProposal[];
  decisions: RemediationDecision[];
  executions: RemediationExecution[];
  evaluations: RemediationEvaluation[];
  created_at: string;
  updated_at: string;
}

export interface ContractRecordPayload {
  contract_id: string;
  owner: string;
  recipe_version_id: string;
  schema_version: string;
  digest: string;
  parent_contract_id?: string | null;
  derivation_reason?: string | null;
  source_advice_id?: string | null;
  source_action_id?: string | null;
  contract: JsonObject;
  field_sources: Array<Record<string, unknown>>;
  created_at: string;
  updated_at: string;
}

export interface TemplateVerification {
  verification_id: string;
  release_id: string;
  run_id: string | null;
  environment: string;
  status: string;
  evidence_ref: string | null;
  evidence_sha256: string | null;
  verified_by: string | null;
  verified_at: string;
}

export interface TemplateRelease {
  release_id: string;
  template_id: string;
  release_version: string;
  publisher: string;
  title: string;
  description: string;
  visibility: string;
  scope_key: string | null;
  payload: JsonObject;
  compatibility: JsonObject;
  publication: JsonObject;
  gate_report: JsonObject;
  content_sha256: string;
  published_at: string;
  withdrawn_at: string | null;
  withdrawal_reason: string | null;
}

export interface TemplateMarketItem extends TemplateRelease {
  metrics: {
    adoption_count: number;
    verification_passed: number;
    verification_failed: number;
    verification_expired: number;
    success_rate: number | null;
    latest_verification: TemplateVerification | null;
  };
}

export interface TemplateReleaseDiff {
  template_id: string;
  from: { release_id: string; release_version: string; content_sha256: string };
  to: { release_id: string; release_version: string; content_sha256: string };
  changes: Array<{ path: string; before?: unknown; after?: unknown }>;
}

export interface TemplateAdoption {
  adoption_id: string;
  release_id: string;
  adopter: string;
  request_key: string;
  target_template_id: string;
  target_draft_id: string;
  target_contract_id: string | null;
  created_at: string;
}

export interface PreparedRun extends RunSummary {
  preview?: { submitted_script?: string; execution_wrapper?: string };
  risk_lint?: Array<Record<string, unknown>>;
  preflight?: ContractFinding[];
}

export interface ContractFinding {
  code: string;
  severity: string;
  message: string;
  field?: string;
}

export interface ContractValidation {
  status: "OK" | "BLOCK" | "WARN" | string;
  findings: ContractFinding[];
  effective_request: {
    recipe_version_id: string;
    schema_version: string;
    contract_digest: string;
    contract: JsonObject;
    workdir: string;
    script: string | null;
    materializer: string;
    resource_plan: Record<string, unknown>;
  };
  risk_lint: Array<Record<string, unknown>>;
  configuration_snapshot_id: string;
  observed_at: string;
}

export interface RecipeSummaryPayload {
  recipe_id: string;
  latest_version: string;
  title: string;
  trust_level: string;
  executable: boolean;
}
