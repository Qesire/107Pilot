import { useQuery } from "@tanstack/react-query";
import { api } from "./api";

export function useWebSession(requestedUser: string) {
  return useQuery({
    queryKey: ["web-session", requestedUser],
    queryFn: ({ signal }) => api.webSession(requestedUser, signal),
    retry: false,
    staleTime: 30_000,
  });
}

export function useHealth(user: string) {
  return useQuery({
    queryKey: ["health", user],
    queryFn: ({ signal }) => api.health(user, signal),
    refetchInterval: 30_000,
  });
}

export function useCapabilities(user: string) {
  return useQuery({
    queryKey: ["capabilities", user],
    queryFn: ({ signal }) => api.capabilities(user, signal),
    staleTime: 30_000,
  });
}

export function useLatestPlatform(user: string) {
  return useQuery({
    queryKey: ["platform-snapshot", user],
    queryFn: ({ signal }) => api.latestPlatform(user, signal),
    retry: false,
  });
}

export function useLatestEntitlement(user: string) {
  return useQuery({
    queryKey: ["entitlement", user],
    queryFn: ({ signal }) => api.latestEntitlement(user, signal),
    retry: false,
  });
}

export function useRuns(user: string, state?: string, search?: string, limit = "20") {
  return useQuery({
    queryKey: ["runs", user, state ?? "", search ?? "", limit],
    queryFn: ({ signal }) => api.runs(user, { state, q: search, limit }, signal),
    refetchInterval: 15_000,
  });
}

export function useRun(user: string, runId: string | null) {
  return useQuery({
    queryKey: ["run", user, runId],
    queryFn: ({ signal }) => api.run(user, runId ?? "", signal),
    enabled: Boolean(runId),
    refetchInterval: 10_000,
  });
}

export function useRunEvidence(user: string, runId: string | null) {
  return useQuery({
    queryKey: ["run-evidence", user, runId],
    queryFn: ({ signal }) => api.runEvidence(user, runId ?? "", signal),
    enabled: Boolean(runId),
    refetchInterval: (query) =>
      ["succeeded", "degraded", "failed"].includes(query.state.data?.collection_state ?? "")
        ? false
        : 5_000,
  });
}

export function useEvidenceObject(
  user: string,
  runId: string | null,
  objectId: string | null,
) {
  return useQuery({
    queryKey: ["evidence-object", user, runId, objectId],
    queryFn: ({ signal }) => api.evidenceObject(user, runId ?? "", objectId ?? "", signal),
    enabled: Boolean(runId && objectId),
    staleTime: Number.POSITIVE_INFINITY,
  });
}

export function useRunDiagnoses(user: string, runId: string | null) {
  return useQuery({
    queryKey: ["run-diagnoses", user, runId],
    queryFn: ({ signal }) => api.runDiagnoses(user, runId ?? "", signal),
    enabled: Boolean(runId),
    refetchInterval: (query) => query.state.data?.diagnosis_state === "running" ? 3_000 : false,
  });
}

export function useRunCapsule(user: string, runId: string | null) {
  return useQuery({
    queryKey: ["run-capsule", user, runId],
    queryFn: ({ signal }) => api.runCapsule(user, runId ?? "", signal),
    enabled: Boolean(runId),
    refetchInterval: (query) => query.state.data?.capsule_state === "running" ? 3_000 : false,
  });
}

export function useContractSchema(user: string) {
  return useQuery({
    queryKey: ["contract-schema"],
    queryFn: ({ signal }) => api.contractSchema(user, signal),
    staleTime: Number.POSITIVE_INFINITY,
  });
}

export function useContract(user: string, contractId: string | null) {
  return useQuery({
    queryKey: ["contract", user, contractId],
    queryFn: ({ signal }) => api.contract(user, contractId ?? "", signal),
    enabled: Boolean(contractId),
    staleTime: 30_000,
  });
}

export function useRecipes(user: string) {
  return useQuery({
    queryKey: ["recipes", user],
    queryFn: ({ signal }) => api.recipes(user, signal),
    staleTime: 60_000,
  });
}

export function useTemplates(
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
) {
  return useQuery({
    queryKey: ["templates", user, filters],
    queryFn: ({ signal }) => api.templates(user, filters, signal),
    staleTime: 15_000,
  });
}

export function useTemplateRelease(
  user: string,
  templateId: string | null,
  version: string | null,
) {
  return useQuery({
    queryKey: ["template-release", user, templateId, version],
    queryFn: ({ signal }) => api.templateRelease(user, templateId ?? "", version ?? "", signal),
    enabled: Boolean(templateId && version),
    staleTime: 30_000,
  });
}

export function useTemplateDiff(
  user: string,
  templateId: string | null,
  from: string | null,
  to: string | null,
) {
  return useQuery({
    queryKey: ["template-diff", user, templateId, from, to],
    queryFn: ({ signal }) => api.templateDiff(user, templateId ?? "", from ?? "", to ?? "", signal),
    enabled: Boolean(templateId && from && to && from !== to),
    staleTime: 30_000,
  });
}

export function useRemediationSessions(user: string, state?: string) {
  return useQuery({
    queryKey: ["remediation-sessions", user, state ?? ""],
    queryFn: ({ signal }) => api.remediationSessions(user, state, signal),
    refetchInterval: 10_000,
  });
}

export function useRemediationSession(user: string, sessionId: string | null) {
  return useQuery({
    queryKey: ["remediation-session", user, sessionId],
    queryFn: ({ signal }) => api.remediationSession(user, sessionId ?? "", signal),
    enabled: Boolean(sessionId),
    refetchInterval: (query) => [
      "waiting_evidence",
      "diagnosing",
      "planning",
      "preparing",
      "executing",
      "evaluating",
    ].includes(query.state.data?.state ?? "") ? 3_000 : false,
  });
}
