import { ApiRequestError, describeApiError } from "./api";

export interface WorkAreaSummary {
  workarea_id: string;
  owner: string;
  title: string;
  description: string;
  created_at: string;
  updated_at: string;
}

export interface WorkAreaBinding {
  kind: "asset" | "contract" | "run";
  target_ref: string;
  role?: string;
  source: "user" | "inherited";
  linked_at?: string;
}

export interface WorkAreaDetail extends WorkAreaSummary {
  bindings: {
    contracts: WorkAreaBinding[];
    runs: WorkAreaBinding[];
    assets: WorkAreaBinding[];
  };
}

export interface ContractSummary {
  contract_id: string;
  recipe_version_id: string;
  digest: string;
  created_at: string;
}

export interface LaunchFinding {
  severity: "INFO" | "WARN" | "BLOCK" | string;
  code: string;
  message: string;
  source_authority?: string | null;
}

export interface LaunchPreflight {
  preflight_id: string;
  candidate_id: string;
  candidate_digest: string;
  status: "OK" | "BLOCK";
  findings: LaunchFinding[];
  effective_request: Record<string, unknown>;
  assessment_digest: string;
  created_at: string;
}

export interface LaunchCandidate {
  candidate_id: string;
  workarea_id: string;
  owner: string;
  contract_id: string;
  title: string;
  note: string;
  candidate_digest: string;
  created_at: string;
  updated_at: string;
  preflight: LaunchPreflight | null;
}

export interface LaunchRecord {
  launch_id: string;
  candidate_id: string;
  preflight_id: string;
  workarea_id: string;
  owner: string;
  contract_id: string;
  candidate_digest: string;
  preflight_digest: string;
  committed_at: string;
  submitted_at: string | null;
  submit_error: Record<string, unknown> | null;
  run_ids: string[];
}

export interface LaunchCommitResponse {
  launch: LaunchRecord;
  run: {
    run_id: string;
    contract_id: string | null;
    state: string;
    job_id: string | null;
    workdir: string;
    created_at: string;
    updated_at: string;
  };
  submit_error: Record<string, unknown> | null;
}

interface Page<T> { items: T[] }

function withSignal(signal?: AbortSignal): RequestInit {
  return signal ? { signal } : {};
}

async function request<T>(
  path: string,
  user: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      "X-Pilot107-User": user,
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers ?? {}),
    },
  });
  const payload = await response.json().catch(() => ({})) as T & {
    error?: { code?: string; message?: string };
  };
  if (!response.ok) {
    const code = payload.error?.code ?? `HTTP.${response.status}`;
    throw new ApiRequestError(
      response.status,
      code,
      describeApiError(code, payload.error?.message ?? response.statusText),
    );
  }
  return payload;
}

const mutate = <T>(
  method: "POST" | "PATCH",
  path: string,
  user: string,
  body: object,
) => request<T>(path, user, {
  method,
  body: JSON.stringify(body),
});

const post = <T>(path: string, user: string, body: object) =>
  mutate<T>("POST", path, user, body);

export const workareaApi = {
  list: (user: string, signal?: AbortSignal) =>
    request<Page<WorkAreaSummary>>(`/api/v1/workareas?limit=100`, user, withSignal(signal)),
  get: (user: string, id: string, signal?: AbortSignal) =>
    request<WorkAreaDetail>(
      `/api/v1/workareas/${encodeURIComponent(id)}`,
      user,
      withSignal(signal),
    ),
  create: (user: string, input: { title: string; description: string; request_key: string }) =>
    post<WorkAreaDetail>("/api/v1/workareas", user, input),
  update: (
    user: string,
    id: string,
    input: { title?: string; description?: string },
  ) => mutate<WorkAreaDetail>(
    "PATCH",
    `/api/v1/workareas/${encodeURIComponent(id)}`,
    user,
    input,
  ),
  addBinding: (
    user: string,
    id: string,
    input: { kind: "asset" | "contract" | "run"; target_ref: string; role?: string },
  ) => post<WorkAreaDetail>(
    `/api/v1/workareas/${encodeURIComponent(id)}/bindings`,
    user,
    input,
  ),
  removeBinding: (
    user: string,
    id: string,
    input: { kind: "asset" | "contract" | "run"; target_ref: string },
  ) => request<Record<string, never>>(
    `/api/v1/workareas/${encodeURIComponent(id)}/bindings/${encodeURIComponent(input.kind)}/${encodeURIComponent(input.target_ref)}`,
    user,
    { method: "DELETE" },
  ),
  contracts: (user: string, signal?: AbortSignal) =>
    request<Page<ContractSummary>>(
      `/api/v1/contracts?owner=${encodeURIComponent(user)}&limit=100`,
      user,
      withSignal(signal),
    ),
  createCandidate: (
    user: string,
    workareaId: string,
    input: { contract_id: string; title: string; note: string; request_key: string },
  ) => post<LaunchCandidate>(
    `/api/v1/workareas/${encodeURIComponent(workareaId)}/launch-candidates`,
    user,
    input,
  ),
  candidate: (user: string, candidateId: string, signal?: AbortSignal) =>
    request<LaunchCandidate>(
      `/api/v1/launch-candidates/${encodeURIComponent(candidateId)}`,
      user,
      withSignal(signal),
    ),
  preflight: (user: string, candidateId: string) =>
    post<LaunchPreflight>(
      `/api/v1/launch-candidates/${encodeURIComponent(candidateId)}/preflight`,
      user,
      {},
    ),
  commit: (
    user: string,
    candidateId: string,
    input: { preflight_digest: string; request_key: string },
  ) => post<LaunchCommitResponse>(
    `/api/v1/launch-candidates/${encodeURIComponent(candidateId)}/commit`,
    user,
    input,
  ),
  launches: (user: string, workareaId: string, signal?: AbortSignal) =>
    request<Page<LaunchRecord>>(
      `/api/v1/workareas/${encodeURIComponent(workareaId)}/launches?limit=100`,
      user,
      withSignal(signal),
    ),
  launch: (user: string, launchId: string, signal?: AbortSignal) =>
    request<LaunchRecord>(
      `/api/v1/launches/${encodeURIComponent(launchId)}`,
      user,
      withSignal(signal),
    ),
};