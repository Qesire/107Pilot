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
  | "EVIDENCE_PARTIAL"
  | "ORPHANED";

export interface WebSession {
  identity_mode: "demo" | "fixed_user" | string;
  user: string;
  switchable: boolean;
  terminal_deep_link: string | null;
}

export type PlatformConnectionState =
  | "active"
  | "auth_required"
  | "unavailable"
  | "expired"
  | "revoked";

export interface PlatformConnection {
  connection_id: string;
  target_id: string;
  state: PlatformConnectionState;
  owner: "current-user-only";
  checked_at: string | null;
  expires_at: string | null;
  message: string;
  status_code: string;
  revision: number;
}

export interface PlatformConnections {
  items: PlatformConnection[];
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
  job_name?: string | null;
  exit_code: string | null;
  terminal_state?: string | null;
  parent_run_id?: string | null;
  lineage_reason?: string | null;
  attempt?: number;
  workdir?: string | null;
  submit_strategy?: string | null;
  backend?: {
    kind: string | null;
    target_id: string | null;
  };
  recipe_version_id?: string | null;
  created_at: string;
  updated_at: string;
  publication?: {
    status: "eligible" | "published" | "ineligible";
    reason: "run_not_succeeded" | "exit_nonzero" | "not_owner" | "already_published" | null;
    publication_id: string | null;
  };
}

export interface RunEvent {
  event_id: number;
  run_id: string;
  event_type: string;
  payload: JsonObject;
  created_at: string;
}

export interface TerminalCommandResult {
  command: string;
  argv: string[];
  returncode: number;
  stdout: string;
  stderr: string;
}

export interface RunLineageEdge {
  source_run_id: string;
  target_run_id: string;
  type: "lineage" | "workflow_dependency" | string;
  reason: string | null;
}

export interface RunLineage {
  run_id: string;
  root_run_id: string;
  lineage: RunSummary[];
  children: RunSummary[];
  nodes: RunSummary[];
  edges: RunLineageEdge[];
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

export interface PlatformNodeSnapshot {
  node_name: string;
  partitions?: string[];
  state_raw?: string | null;
  state_normalized: string;
  cpus_total?: number | null;
  cpus_allocated?: number | null;
  memory_mb?: number | null;
}

export interface PlatformJobSnapshot {
  job_id: string;
  state_raw: string;
  pending_reason?: string | null;
  partition?: string | null;
  name?: string | null;
}

export interface PlatformSnapshotDetail {
  snapshot_id?: string;
  scope?: string;
  captured_at?: string;
  partitions?: Array<Record<string, unknown>>;
  nodes?: PlatformNodeSnapshot[];
  squeue_jobs?: PlatformJobSnapshot[];
}

export interface PlatformSnapshot {
  snapshot_id: string;
  scope: string;
  source_type: string;
  source_name?: string;
  captured_at?: string;
  expires_at?: string;
  freshness: string;
  collection_status?: string;
  counts?: {
    commands: number;
    partitions: number;
    nodes: number;
    jobs: number;
    limitations: number;
  };
  snapshot?: PlatformSnapshotDetail;
  // Legacy/compat fields referenced by older UI panels.
  observed_at?: string;
  data_quality?: string;
  facts?: Record<string, unknown>;
  limitations?: string[];
}

export interface StorageUsage {
  home: string;
  used_bytes: number;
  total_bytes: number | null;
  observed_at: string;
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
  // The current readiness endpoint emits an ordered array. Keep the map
  // shape as a compatibility union for older deployments.
  checks?: HealthCheck[] | Record<string, HealthCheck>;
}

export interface HealthCheck {
  name?: string;
  status?: string;
  detail?: string;
  reason?: string;
  required?: boolean;
  latency_ms?: number;
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
  // Backend emits `provider` on every GET (remediation_service.py:707) and
  // persists it via the `provider` column (migration 003e.003). Kept as a
  // string so unknown values degrade gracefully instead of breaking the UI.
  provider: string;
  // Backend emits `lease` on every GET (remediation_service.py:712-715). Both
  // fields are nullable: an idle session has no lease owner or expiry.
  lease: { owner: string | null; expires_at: string | null };
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

export type MarketVisibility = "private" | "course" | "campus" | "public";

/**
 * An owner-confirmed successful Run, not a curated/reproduced template.
 * The API intentionally does not include the submitted script, workdir, or
 * source Contract payload in this market read model.
 */
export interface SuccessfulRunMarketItem {
  kind: "successful_run";
  publication_id: string;
  source_run_id: string;
  owner: string;
  title: string;
  description: string;
  visibility: MarketVisibility;
  scope_key: string | null;
  tags: string[];
  reproduction_note: string;
  adoptable: boolean;
  published_at: string;
  updated_at: string;
}

export interface SuccessfulRunPublicationInput {
  request_key: string;
  title: string;
  description: string;
  visibility: MarketVisibility;
  scope_key?: string | null;
  tags: string[];
  reproduction_note: string;
  confirm_share: boolean;
}

export interface SuccessfulRunAdoption {
  adoption_id: string;
  publication_id: string;
  target_contract_id: string | null;
  created_at: string;
}

export type MarketItemKind = "run_publication" | "curated_template";

interface MarketItemBase {
  kind: MarketItemKind;
  item_id: string;
  title: string;
  description: string;
  visibility: MarketVisibility;
  scope_key: string | null;
  publisher: string;
  published_at: string;
  updated_at: string;
  tags: string[];
  adoption: {
    available: boolean;
    reason: string | null;
  };
  withdrawn_at: string | null;
}

export interface RunPublicationMarketItem extends MarketItemBase {
  kind: "run_publication";
  source: {
    type: "successful_run";
    run_id: string;
  };
  reproduction_note: string;
}

export interface CuratedTemplateMarketItem extends MarketItemBase {
  kind: "curated_template";
  template: {
    template_id: string;
    release_version: string;
    content_sha256: string;
  };
  contract_payload: JsonObject;
  compatibility: JsonObject;
  publication: JsonObject;
  metrics: TemplateMarketItem["metrics"];
}

export type MarketItem = RunPublicationMarketItem | CuratedTemplateMarketItem;

export interface MarketItemAdoption {
  adoption_id: string;
  target_contract_id: string | null;
  created_at: string;
  publication_id?: string;
  release_id?: string;
  target_template_id?: string;
  target_draft_id?: string;
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

export interface ContractSuggestion {
  suggested_patch: Record<string, unknown>;
  explanation_zh: string;
  needs_user_confirmation: boolean;
}

export interface RecipeSummaryPayload {
  recipe_id: string;
  latest_version: string;
  title: string;
  trust_level: string;
  executable: boolean;
}

/** Full recipe version read model (GET /api/v1/recipes/{id}/versions/{v}). */
export interface RecipeVersionPayload {
  recipe_id: string;
  version: string;
  recipe_version_id: string;
  title: string;
  description: string;
  trust_level: string;
  /** Dotted-path parameter schema: `required` array plus per-field metadata. */
  parameter_schema: JsonObject;
  compatibility: JsonObject;
  risk_declaration: JsonObject | null;
  preflight_checks: unknown[];
  recovery: JsonObject | null;
  success_protocol: JsonObject | null;
  source: JsonObject;
  content_sha256: string;
  materializer: string;
  executable: boolean;
}

// ---------------------------------------------------------------------------
// Agent explanation (POST /api/v1/runs/{id}/agent/explain)
// ---------------------------------------------------------------------------

export interface AgentFactPayload {
  fact_id: string;
  statement: string;
  evidence_refs: string[];
  evidence_object_ids: string[];
  confidence: string;
}

export interface AgentCitationPayload {
  fact_id: string;
  evidence_object_ids: string[];
}

export interface AgentExplanation {
  run_id: string;
  provider: string;
  status: string;
  summary: string;
  facts: AgentFactPayload[];
  diagnoses: DiagnosisRecordPayload[];
  model: string | null;
  narrative: string | null;
  recommendations: string[];
  citations: AgentCitationPayload[];
  warnings: string[];
  evidence_bundle_sha256: string | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Template authoring: drafts, reviews, publication gate
// ---------------------------------------------------------------------------

export interface TemplateDraft {
  draft_id: string;
  template_id: string;
  owner: string;
  title: string;
  description: string;
  visibility: MarketVisibility;
  scope_key: string | null;
  state: string;
  version: number;
  payload: JsonObject;
  compatibility: JsonObject;
  publication: JsonObject;
  content_sha256: string;
  created_at: string;
  updated_at: string;
}

export interface TemplateReview {
  review_id: string;
  draft_id: string;
  requester: string;
  reviewer: string | null;
  reviewer_role: string | null;
  reviewer_scope_key: string | null;
  state: string;
  version: number;
  draft_version: number;
  content_sha256: string;
  note: string | null;
  gate_report: JsonObject | null;
  validated_at: string | null;
  created_at: string;
  updated_at: string;
  decided_at: string | null;
}

/** Review queue rows additionally carry draft title and scope facts. */
export interface TemplateReviewQueueItem extends TemplateReview {
  draft_title: string;
  visibility: MarketVisibility;
  scope_key: string | null;
}

export interface TemplateGateFinding {
  code: string;
  severity: string;
  message: string;
  [key: string]: unknown;
}

export interface TemplateGateValidation {
  policy_version: string;
  status: string;
  findings: TemplateGateFinding[];
}

// ---------------------------------------------------------------------------
// M2: Repair Ticket & Artifact Manifest
// ---------------------------------------------------------------------------

export type RepairTicketState = "open" | "resolved" | "abandoned";

export interface ArtifactManifest {
  manifest_id: string;
  owner: string;
  run_id: string | null;
  revision: string;
  dirty_diff_digest: string | null;
  bundle_digest: string | null;
  remote_workdir: string | null;
  local_test_summary: string | null;
  disclosure: string;
  created_at: string;
}

export interface RepairTicketComparison {
  source_run_id: string;
  derived_run_id: string;
  source_state?: string;
  source_exit_code?: string | null;
  derived_state?: string;
  derived_exit_code?: string | null;
  source_diagnosis_count?: number;
  derived_diagnosis_count?: number;
  source_diagnosis_rules?: string[];
  derived_diagnosis_rules?: string[];
  improved?: boolean;
}

export interface RepairTicket {
  ticket_id: string;
  owner: string;
  state: RepairTicketState;
  source_run_id: string;
  source_contract_id: string | null;
  session_id: string | null;
  diagnosis_ids: string[];
  cited_facts: Array<Record<string, unknown>>;
  code_context: JsonObject | null;
  requested_change: string | null;
  no_go_constraints: string[];
  resolution_manifest_id: string | null;
  resolution_run_id: string | null;
  resolution_comparison: RepairTicketComparison | null;
  abandon_reason: string | null;
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Visual Filesystem
// ---------------------------------------------------------------------------

export interface FileEntry {
  name: string;
  path: string;
  kind: "file" | "directory" | "symlink";
  size: number;
  modified: string;
}

export interface FileListResponse {
  path: string;
  entries: FileEntry[];
}

export interface FileContentResponse {
  path: string;
  offset: number;
  size: number;
  data_b64: string;
}

export type UploadSessionState =
  | "initialized"
  | "uploading"
  | "completing"
  | "completed"
  | "written"
  | "aborted"
  | "failed";

export interface UploadSession {
  upload_id: string;
  owner: string;
  target_path: string;
  filename: string;
  total_size: number;
  is_partial: boolean;
  received_bytes: number;
  sha256_expected: string | null;
  sha256_actual: string | null;
  state: UploadSessionState;
  auto_extract: boolean;
  created_at: string;
  written_path: string | null;
  extracted_members: number | null;
  error: string | null;
}

export interface ArchiveResponse {
  status: string;
  path: string;
  size: number;
}
