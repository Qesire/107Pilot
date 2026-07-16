import type {
  CapabilityProfile,
  ContractRecordPayload,
  ContractValidation,
  EntitlementSnapshot,
  HealthReady,
  PagePayload,
  PlatformSnapshot,
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
    const message = payload.error?.message ?? response.statusText ?? "请求失败";
    throw new ApiRequestError(response.status, code, message);
  }
  return payload;
}

async function sendJson<T>(
  path: string,
  user: string,
  body: JsonObject,
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
    const message = payload.error?.message ?? response.statusText ?? "请求失败";
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
    },
    signal?: AbortSignal,
  ) =>
    getJson<PagePayload<RunSummary>>(
      queryPath("/api/v1/runs", {
        owner: user,
        state: filters.state,
        q: filters.q,
        limit: filters.limit ?? "20",
      }),
      user,
      signal,
    ),
  run: (user: string, runId: string, signal?: AbortSignal) =>
    getJson<RunSummary>(`/api/v1/runs/${encodeURIComponent(runId)}`, user, signal),
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
  remediationSessions: (user: string, state?: string, signal?: AbortSignal) =>
    getJson<{ items: RemediationSession[] }>(
      queryPath("/api/v1/remediation-sessions", { owner: user, state }),
      user,
      signal,
    ),
  remediationSession: (user: string, sessionId: string, signal?: AbortSignal) =>
    getJson<RemediationSession>(
      `/api/v1/remediation-sessions/${encodeURIComponent(sessionId)}`,
      user,
      signal,
    ),
  createRemediationSession: (user: string, runId: string, signal?: AbortSignal) =>
    sendJson<RemediationSession>(
      `/api/v1/runs/${encodeURIComponent(runId)}/remediation-sessions`,
      user,
      { request_key: `ui:${runId}`, automation_policy: "manual_approval" },
      signal,
    ),
  advanceRemediationSession: (user: string, sessionId: string, signal?: AbortSignal) =>
    sendJson<RemediationSession>(
      `/api/v1/remediation-sessions/${encodeURIComponent(sessionId)}/advance`,
      user,
      {},
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
};
