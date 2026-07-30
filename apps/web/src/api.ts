import type {
  ArchiveResponse,
  ArtifactManifest,
  CapabilityProfile,
  ContractRecordPayload,
  ContractSuggestion,
  ContractValidation,
  EntitlementSnapshot,
  FileContentResponse,
  FileListResponse,
  HealthReady,
  PagePayload,
  PlatformConnection,
  PlatformConnections,
  PlatformSnapshot,
  RepairTicket,
  RunEvent,
  RunLineage,
  RunSummary,
  JsonObject,
  RecipeSummaryPayload,
  PreparedRun,
  RemediationSession,
  EvidenceObjectPreview,
  RunCapsule,
  RunDiagnoses,
  RunEvidence,
  TemplateAdoption,
  TemplateMarketItem,
  TemplateRelease,
  TemplateReleaseDiff,
  MarketItem,
  MarketItemAdoption,
  SuccessfulRunAdoption,
  SuccessfulRunMarketItem,
  SuccessfulRunPublicationInput,
  TerminalCommandResult,
  UploadSession,
  WebSession,
} from "./types";

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
  }
}

interface ErrorPayload {
  error?: { code?: string; message?: string };
}

const genericHttpMessages = new Set(["", "OK", "Forbidden", "Unauthorized", "Bad Request", "请求失败"]);

export function describeApiError(code: string, message?: string): string {
  const supplied = message?.trim() ?? "";
  if (supplied && !genericHttpMessages.has(supplied)) return supplied;
  const guidance: Record<string, string> = {
    "AUTH.FORBIDDEN": "当前登录身份无权访问该资源。请确认正在使用正确的账号，且该作业或 Contract 属于此账号。",
    "AUTH.MISSING": "未识别到登录身份。请重新登录后再试。",
    "AUTH.PROXY_SIGNATURE_INVALID": "身份代理校验未通过。请联系部署管理员检查 Web 与 API 的受信代理配置。",
    "TEMPLATE.FORBIDDEN": "当前账号没有采用此模板 release 的权限。请返回模板市场选择可见的 release，或申请课程/发布权限。",
    "CSRF.ORIGIN_DENIED": "当前访问来源未获写入授权。请从配置的 107Pilot 地址重新打开页面后再试。",
    "CSRF.COOKIE_AUTH_UNSUPPORTED": "此部署不接受浏览器 Cookie 代签写操作。请联系部署管理员完成身份代理配置。",
    "CSRF.CROSS_SITE_DENIED": "跨站写操作已被安全策略拒绝。请从 107Pilot 页面内重新发起操作。",
  };
  return guidance[code] ?? (supplied || "请求失败");
}

async function getJson<T>(
  path: string,
  user: string,
  signal?: AbortSignal,
): Promise<T> {
  const request: RequestInit = {
    method: "GET",
    headers: {
      Accept: "application/json",
      "X-Pilot107-User": user,
    },
  };
  if (signal) request.signal = signal;
  const response = await fetch(path, request);
  const payload = (await response.json().catch(() => ({}))) as T & ErrorPayload;
  if (!response.ok) {
    const code = payload.error?.code ?? `HTTP.${response.status}`;
    const message = describeApiError(code, payload.error?.message ?? response.statusText);
    throw new ApiRequestError(response.status, code, message);
  }
  return payload;
}

async function sendJson<T>(
  path: string,
  user: string,
  body: object,
  signal?: AbortSignal,
): Promise<T> {
  const request: RequestInit = {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-Pilot107-User": user,
    },
    body: JSON.stringify(body),
  };
  if (signal) request.signal = signal;
  const response = await fetch(path, request);
  const payload = (await response.json().catch(() => ({}))) as T & ErrorPayload;
  if (!response.ok) {
    const code = payload.error?.code ?? `HTTP.${response.status}`;
    const message = describeApiError(code, payload.error?.message ?? response.statusText);
    throw new ApiRequestError(response.status, code, message);
  }
  return payload;
}

function queryPath(path: string, params: Record<string, string | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value) search.set(key, value);
  });
  const encoded = search.toString();
  return encoded ? `${path}?${encoded}` : path;
}

export const api = {
  webSession: (requestedUser: string, signal?: AbortSignal) =>
    getJson<WebSession>("/api/v1/web/session", requestedUser, signal),
  health: (user: string, signal?: AbortSignal) =>
    getJson<HealthReady>("/api/v1/health/ready", user, signal),
  platformConnections: (user: string, signal?: AbortSignal) =>
    getJson<PlatformConnections>("/api/v1/platform/connections", user, signal),
  checkPlatformConnection: (
    user: string,
    connectionId: string,
    signal?: AbortSignal,
  ) =>
    sendJson<PlatformConnection>(
      `/api/v1/platform/connections/${encodeURIComponent(connectionId)}/check`,
      user,
      {},
      signal,
    ),
  capabilities: (user: string, signal?: AbortSignal) =>
    getJson<CapabilityProfile>(
      queryPath("/api/v1/platform/capabilities", { owner: user }),
      user,
      signal,
    ),
  latestPlatform: (user: string, signal?: AbortSignal) =>
    getJson<PlatformSnapshot>(
      queryPath("/api/v1/platform/snapshots/latest", {
        owner: user,
        scope: "login_node",
      }),
      user,
      signal,
    ),
  latestEntitlement: (user: string, signal?: AbortSignal) =>
    getJson<EntitlementSnapshot>(
      queryPath("/api/v1/platform/entitlements/latest", { owner: user }),
      user,
      signal,
    ),
  runs: (
    user: string,
    filters: {
      state?: string | undefined;
      q?: string | undefined;
      limit?: string | undefined;
      cursor?: string | undefined;
    },
    signal?: AbortSignal,
  ) =>
    getJson<PagePayload<RunSummary>>(
      queryPath("/api/v1/runs", {
        owner: user,
        state: filters.state,
        q: filters.q,
        limit: filters.limit ?? "20",
        cursor: filters.cursor,
      }),
      user,
      signal,
    ),
  run: (user: string, runId: string, signal?: AbortSignal) =>
    getJson<RunSummary>(`/api/v1/runs/${encodeURIComponent(runId)}`, user, signal),
  runEvents: (user: string, runId: string, signal?: AbortSignal) =>
    getJson<PagePayload<RunEvent>>(
      `/api/v1/runs/${encodeURIComponent(runId)}/events?limit=100`,
      user,
      signal,
    ),
  runLineage: (user: string, runId: string, signal?: AbortSignal) =>
    getJson<RunLineage>(
      `/api/v1/runs/${encodeURIComponent(runId)}/lineage`,
      user,
      signal,
    ),
  runEvidence: (user: string, runId: string, signal?: AbortSignal) =>
    getJson<RunEvidence>(
      `/api/v1/runs/${encodeURIComponent(runId)}/evidence`,
      user,
      signal,
    ),
  evidenceObject: (
    user: string,
    runId: string,
    objectId: string,
    signal?: AbortSignal,
  ) => getJson<EvidenceObjectPreview>(
    `/api/v1/runs/${encodeURIComponent(runId)}/evidence/objects/${encodeURIComponent(objectId)}`,
    user,
    signal,
  ),
  runDiagnoses: (user: string, runId: string, signal?: AbortSignal) =>
    getJson<RunDiagnoses>(
      `/api/v1/runs/${encodeURIComponent(runId)}/diagnoses`,
      user,
      signal,
    ),
  diagnoseRun: (user: string, runId: string, signal?: AbortSignal) =>
    sendJson<RunDiagnoses>(
      `/api/v1/runs/${encodeURIComponent(runId)}/diagnose`,
      user,
      {},
      signal,
    ),
  runCapsule: (user: string, runId: string, signal?: AbortSignal) =>
    getJson<RunCapsule>(
      `/api/v1/runs/${encodeURIComponent(runId)}/capsule`,
      user,
      signal,
    ),
  buildRunCapsule: (user: string, runId: string, signal?: AbortSignal) =>
    sendJson<RunCapsule>(
      `/api/v1/runs/${encodeURIComponent(runId)}/capsule`,
      user,
      {},
      signal,
    ),
  contractSchema: (user: string, signal?: AbortSignal) =>
    getJson<JsonObject>("/api/v1/contracts/schema", user, signal),
  contract: (user: string, contractId: string, signal?: AbortSignal) =>
    getJson<ContractRecordPayload>(
      `/api/v1/contracts/${encodeURIComponent(contractId)}`,
      user,
      signal,
    ),
  recipes: (user: string, signal?: AbortSignal) =>
    getJson<{ items: RecipeSummaryPayload[] }>("/api/v1/recipes", user, signal),
  validateContract: (user: string, contract: JsonObject, signal?: AbortSignal) =>
    sendJson<ContractValidation>("/api/v1/contracts/validate", user, contract, signal),
  createContract: (user: string, contract: JsonObject, signal?: AbortSignal) =>
    sendJson<ContractRecordPayload>("/api/v1/contracts", user, contract, signal),
  suggestContractPatch: (
    user: string,
    contract: JsonObject,
    recipeVersionId: string,
    userIntent: string,
    provider: "local" | "none" = "local",
    signal?: AbortSignal,
  ) => sendJson<ContractSuggestion>(
    "/api/v1/contracts/agent/suggest",
    user,
    {
      current_contract: contract,
      recipe_version_id: recipeVersionId,
      user_intent: userIntent,
      provider,
    },
    signal,
  ),
  templates: (
    user: string,
    filters: {
      q?: string | undefined;
      visibility?: string | undefined;
      partition?: string | undefined;
      gpu?: string | undefined;
      verified?: string | undefined;
      verification_environment?: string | undefined;
      limit?: string | undefined;
    },
    signal?: AbortSignal,
  ) => getJson<PagePayload<TemplateMarketItem>>(
    queryPath("/api/v1/templates", { ...filters, limit: filters.limit ?? "20" }),
    user,
    signal,
  ),
  templateRelease: (
    user: string,
    templateId: string,
    version: string,
    signal?: AbortSignal,
  ) => getJson<TemplateRelease>(
    `/api/v1/templates/${encodeURIComponent(templateId)}/releases/${encodeURIComponent(version)}`,
    user,
    signal,
  ),
  templateDiff: (
    user: string,
    templateId: string,
    from: string,
    to: string,
    signal?: AbortSignal,
  ) => getJson<TemplateReleaseDiff>(
    queryPath(`/api/v1/templates/${encodeURIComponent(templateId)}/diff`, { from, to }),
    user,
    signal,
  ),
  adoptTemplate: (
    user: string,
    templateId: string,
    version: string,
    requestKey: string,
    signal?: AbortSignal,
  ) => sendJson<TemplateAdoption>(
    `/api/v1/templates/${encodeURIComponent(templateId)}/releases/${encodeURIComponent(version)}/adopt`,
    user,
    { request_key: requestKey },
    signal,
  ),
  marketItems: (
    user: string,
    filters: {
      q?: string | undefined;
      kind?: string | undefined;
      visibility?: string | undefined;
      tag?: string | undefined;
      limit?: string | undefined;
      cursor?: string | undefined;
    },
    signal?: AbortSignal,
  ) => getJson<PagePayload<MarketItem>>(
    queryPath("/api/v1/market/items", { ...filters, limit: filters.limit ?? "20" }),
    user,
    signal,
  ),
  marketItem: (
    user: string,
    itemId: string,
    signal?: AbortSignal,
  ) => getJson<MarketItem>(
    `/api/v1/market/items/${encodeURIComponent(itemId)}`,
    user,
    signal,
  ),
  adoptMarketItem: (
    user: string,
    itemId: string,
    requestKey: string,
    signal?: AbortSignal,
  ) => sendJson<MarketItemAdoption>(
    `/api/v1/market/items/${encodeURIComponent(itemId)}/adopt`,
    user,
    { request_key: requestKey },
    signal,
  ),
  withdrawMarketItem: (
    user: string,
    itemId: string,
    reason: string,
    signal?: AbortSignal,
  ) => sendJson<{ publication_id?: string; release_id?: string; withdrawn: boolean }>(
    `/api/v1/market/items/${encodeURIComponent(itemId)}/withdraw`,
    user,
    { reason },
    signal,
  ),
  successfulRunMarket: (
    user: string,
    filters: {
      q?: string | undefined;
      visibility?: string | undefined;
      tag?: string | undefined;
      limit?: string | undefined;
      cursor?: string | undefined;
    },
    signal?: AbortSignal,
  ) => getJson<PagePayload<SuccessfulRunMarketItem>>(
    queryPath("/api/v1/market", { ...filters, limit: filters.limit ?? "20" }),
    user,
    signal,
  ),
  publishSuccessfulRun: (
    user: string,
    runId: string,
    payload: SuccessfulRunPublicationInput,
    signal?: AbortSignal,
  ) => sendJson<SuccessfulRunMarketItem>(
    `/api/v1/runs/${encodeURIComponent(runId)}/publish`,
    user,
    payload,
    signal,
  ),
  adoptSuccessfulRun: (
    user: string,
    publicationId: string,
    requestKey: string,
    signal?: AbortSignal,
  ) => sendJson<SuccessfulRunAdoption>(
    `/api/v1/market/${encodeURIComponent(publicationId)}/adopt`,
    user,
    { request_key: requestKey },
    signal,
  ),
  preflightContract: (user: string, contractId: string, signal?: AbortSignal) =>
    sendJson<ContractValidation>(
      `/api/v1/contracts/${encodeURIComponent(contractId)}/preflight`,
      user,
      {},
      signal,
    ),
  prepareRun: (user: string, contractId: string, signal?: AbortSignal) =>
    sendJson<PreparedRun>("/api/v1/runs/prepare", user, { contract_id: contractId }, signal),
  submitRun: (user: string, runId: string, signal?: AbortSignal) =>
    sendJson<RunSummary>(
      `/api/v1/runs/${encodeURIComponent(runId)}/submit`,
      user,
      {},
      signal,
    ),
  cancelRun: (user: string, runId: string, signal?: AbortSignal) =>
    sendJson<RunSummary>(
      `/api/v1/runs/${encodeURIComponent(runId)}/cancel`,
      user,
      {},
      signal,
    ),
  prepareRetry: (
    user: string,
    run: Pick<RunSummary, "run_id" | "contract_id" | "state">,
    signal?: AbortSignal,
  ) => sendJson<PreparedRun>(
    "/api/v1/runs/prepare",
    user,
    {
      contract_id: run.contract_id,
      parent_run_id: run.run_id,
      lineage_reason: ["FAILED", "SUBMIT_FAILED", "COLLECTION_FAILED", "CANCELLED"]
        .includes(run.state) ? "manual_retry" : "manual_clone",
    },
    signal,
  ),
  remediationSessions: (user: string, state?: string, signal?: AbortSignal) =>
    getJson<{ items: RemediationSession[] }>(
      queryPath("/api/v1/remediation-sessions", { state }),
      user,
      signal,
    ),
  remediationSession: (user: string, sessionId: string, signal?: AbortSignal) =>
    getJson<RemediationSession>(
      `/api/v1/remediation-sessions/${encodeURIComponent(sessionId)}`,
      user,
      signal,
    ),
  createRemediationSession: (
    user: string,
    runId: string,
    requestKey: string,
    provider: "local" | "none" = "local",
    signal?: AbortSignal,
  ) =>
    sendJson<RemediationSession>(
      `/api/v1/runs/${encodeURIComponent(runId)}/remediation-sessions`,
      user,
      { request_key: requestKey, automation_policy: "manual_approval", provider },
      signal,
    ),
  advanceRemediationSession: (
    user: string,
    sessionId: string,
    signal?: AbortSignal,
    options?: { provider?: "local" | "none" },
  ) =>
    sendJson<RemediationSession>(
      `/api/v1/remediation-sessions/${encodeURIComponent(sessionId)}/advance`,
      user,
      { provider: options?.provider ?? "local" },
      signal,
    ),
  approveRemediationAction: (
    user: string,
    sessionId: string,
    proposalId: string,
    expectedVersion: number,
    signal?: AbortSignal,
  ) => sendJson<RemediationSession>(
    `/api/v1/remediation-sessions/${encodeURIComponent(sessionId)}/approve`,
    user,
    { proposal_id: proposalId, expected_version: expectedVersion },
    signal,
  ),
  rejectRemediationAction: (
    user: string,
    sessionId: string,
    proposalId: string,
    expectedVersion: number,
    signal?: AbortSignal,
  ) => sendJson<RemediationSession>(
    `/api/v1/remediation-sessions/${encodeURIComponent(sessionId)}/reject`,
    user,
    { proposal_id: proposalId, expected_version: expectedVersion },
    signal,
  ),
  cancelRemediationSession: (
    user: string,
    sessionId: string,
    expectedVersion: number,
    signal?: AbortSignal,
  ) => sendJson<RemediationSession>(
    `/api/v1/remediation-sessions/${encodeURIComponent(sessionId)}/cancel`,
    user,
    { expected_version: expectedVersion },
    signal,
  ),
  takeoverRemediationSession: (
    user: string,
    sessionId: string,
    expectedVersion: number,
    note: string,
    signal?: AbortSignal,
  ) => sendJson<RemediationSession>(
    `/api/v1/remediation-sessions/${encodeURIComponent(sessionId)}/takeover`,
    user,
    { expected_version: expectedVersion, note },
    signal,
  ),
  executeRemediationAction: (
    user: string,
    sessionId: string,
    proposalId: string,
    expectedVersion: number,
    signal?: AbortSignal,
  ) => sendJson<RemediationSession>(
    `/api/v1/remediation-sessions/${encodeURIComponent(sessionId)}/execute`,
    user,
    { proposal_id: proposalId, expected_version: expectedVersion, submit: true },
    signal,
  ),
  terminalCommand: (
    user: string,
    command: "identity" | "cluster" | "my_jobs" | "run_status",
    runId?: string | null,
    signal?: AbortSignal,
  ) => sendJson<TerminalCommandResult>(
    "/api/v1/terminal/commands",
    user,
    { command, ...(runId ? { run_id: runId } : {}) },
    signal,
  ),
  // -------------------------------------------------------------------------
  // M2: Repair Tickets & Artifact Manifests
  // -------------------------------------------------------------------------
  repairTickets: (user: string, state?: string, sessionId?: string, signal?: AbortSignal) =>
    getJson<{ items: RepairTicket[]; page: { limit: number; has_more: boolean; next_cursor: string | null } }>(
      queryPath("/api/v1/repair-tickets", { state, session_id: sessionId }),
      user,
      signal,
    ),
  repairTicket: (user: string, ticketId: string, signal?: AbortSignal) =>
    getJson<RepairTicket>(
      `/api/v1/repair-tickets/${encodeURIComponent(ticketId)}`,
      user,
      signal,
    ),
  createRepairTicket: (
    user: string,
    input: { session_id?: string; source_run_id?: string; request_key: string; requested_change?: string },
    signal?: AbortSignal,
  ) => sendJson<RepairTicket>("/api/v1/repair-tickets", user, input, signal),
  resolveRepairTicket: (
    user: string,
    ticketId: string,
    input: { manifest_id: string; derived_run_id: string },
    signal?: AbortSignal,
  ) => sendJson<RepairTicket>(
    `/api/v1/repair-tickets/${encodeURIComponent(ticketId)}/resolve`,
    user,
    input,
    signal,
  ),
  abandonRepairTicket: (
    user: string,
    ticketId: string,
    reason?: string,
    signal?: AbortSignal,
  ) => sendJson<RepairTicket>(
    `/api/v1/repair-tickets/${encodeURIComponent(ticketId)}/abandon`,
    user,
    { reason: reason ?? null },
    signal,
  ),
  createArtifactManifest: (
    user: string,
    input: {
      revision: string;
      run_id?: string;
      dirty_diff_digest?: string;
      bundle_digest?: string;
      remote_workdir?: string;
      local_test_summary?: string;
      disclosure?: string;
    },
    signal?: AbortSignal,
  ) => sendJson<ArtifactManifest>("/api/v1/artifact-manifests", user, input, signal),
  runArtifactManifests: (user: string, runId: string, signal?: AbortSignal) =>
    getJson<{ items: ArtifactManifest[] }>(
      `/api/v1/runs/${encodeURIComponent(runId)}/artifact-manifests`,
      user,
      signal,
    ),
  // -------------------------------------------------------------------------
  // Visual Filesystem
  // -------------------------------------------------------------------------
  fileList: (user: string, path: string, signal?: AbortSignal) =>
    getJson<FileListResponse>(
      queryPath("/api/v1/files", { path }),
      user,
      signal,
    ),
  fileContent: (
    user: string,
    path: string,
    offset: number,
    length: number,
    signal?: AbortSignal,
  ) =>
    getJson<FileContentResponse>(
      queryPath("/api/v1/files/content", {
        path,
        offset: String(offset),
        length: String(length),
      }),
      user,
      signal,
    ),
  fileMkdir: (user: string, path: string, signal?: AbortSignal) =>
    sendJson<{ status: string; path: string }>("/api/v1/files/mkdir", user, { path }, signal),
  fileDelete: (user: string, path: string, signal?: AbortSignal) =>
    sendJson<{ status: string; path: string }>("/api/v1/files/delete", user, { path }, signal),
  fileArchive: (
    user: string,
    paths: string[],
    destDir: string,
    archiveName?: string,
    signal?: AbortSignal,
  ) =>
    sendJson<ArchiveResponse>(
      "/api/v1/files/archive",
      user,
      { paths, dest_dir: destDir, ...(archiveName ? { archive_name: archiveName } : {}) },
      signal,
    ),
  uploadInit: (
    user: string,
    input: {
      target_path: string;
      filename: string;
      total_size: number;
      sha256?: string;
      chunk_size?: number;
      auto_extract?: boolean;
    },
    signal?: AbortSignal,
  ) => sendJson<UploadSession>("/api/v1/files/uploads", user, input, signal),
  uploadChunk: (
    user: string,
    sessionId: string,
    index: number,
    dataB64: string,
    signal?: AbortSignal,
  ) =>
    sendJson<UploadSession>(
      `/api/v1/files/uploads/${encodeURIComponent(sessionId)}/chunks`,
      user,
      { index, data_b64: dataB64 },
      signal,
    ),
  uploadComplete: (user: string, sessionId: string, signal?: AbortSignal) =>
    sendJson<UploadSession>(
      `/api/v1/files/uploads/${encodeURIComponent(sessionId)}/complete`,
      user,
      {},
      signal,
    ),
  uploadAbort: (user: string, sessionId: string, signal?: AbortSignal) =>
    sendJson<UploadSession>(
      `/api/v1/files/uploads/${encodeURIComponent(sessionId)}/abort`,
      user,
      {},
      signal,
    ),
  uploadSessions: (user: string, signal?: AbortSignal) =>
    getJson<{ items: UploadSession[] }>("/api/v1/files/uploads", user, signal),
};
