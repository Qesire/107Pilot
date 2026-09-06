import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { AgentTask, RuntimeWatchState } from "./types";
import { runtimePollingInterval, type RuntimeViewerVisibility } from "./runtime-polling";
import {
  cpuAllocation,
  jobsByState,
  nodesByState,
  type CpuAllocation,
  type StateCount,
} from "./resource-summary";

export function useWebSession(requestedUser: string) {
  return useQuery({
    queryKey: ["web-session", requestedUser],
    queryFn: ({ signal }) => api.webSession(requestedUser, signal),
    retry: false,
    staleTime: 30_000,
  });
}

export function useHealth(user: string, enabled = true) {
  return useQuery({
    queryKey: ["health", user],
    queryFn: ({ signal }) => api.health(user, signal),
    enabled,
    refetchInterval: enabled ? 30_000 : false,
  });
}

export function usePlatformConnections(user: string) {
  return useQuery({
    queryKey: ["platform-connections", user],
    queryFn: ({ signal }) => api.platformConnections(user, signal),
    retry: false,
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

export function useLatestPlatformObservation(user: string, connectionId: string) {
  return useQuery({
    queryKey: ["platform-observation", user, connectionId],
    queryFn: ({ signal }) => api.latestPlatformObservation(user, connectionId, signal),
    retry: false,
    refetchInterval: 20_000,
  });
}

export interface ResourceSummary {
  nodes: StateCount[];
  cpu: CpuAllocation;
  jobs: StateCount[];
  freshness: string;
  capturedAt?: string | undefined;
  collectionStatus?: string | undefined;
  hasDetail: boolean;
}

// Polls the latest platform snapshot and reduces it to chart-ready aggregates.
// Shares the ["platform-snapshot"] cache with useLatestPlatform; the 20s poll
// is the "real-time" mechanism (the snapshot itself carries freshness facts).
export function useResourceSummary(user: string) {
  return useQuery({
    queryKey: ["platform-snapshot", user],
    queryFn: ({ signal }) => api.latestPlatform(user, signal),
    retry: false,
    refetchInterval: 20_000,
    select: (snapshot): ResourceSummary => {
      const detail = snapshot.snapshot;
      return {
        nodes: nodesByState(detail?.nodes),
        cpu: cpuAllocation(detail?.nodes),
        jobs: jobsByState(detail?.squeue_jobs),
        freshness: snapshot.freshness,
        capturedAt: detail?.captured_at ?? snapshot.captured_at,
        collectionStatus: snapshot.collection_status,
        hasDetail:
          (detail?.nodes?.length ?? 0) > 0 || (detail?.squeue_jobs?.length ?? 0) > 0,
      };
    },
  });
}

export function useStorageUsage(user: string) {
  return useQuery({
    queryKey: ["storage-usage", user],
    queryFn: ({ signal }) => api.storageUsage(user, signal),
    retry: false,
    refetchInterval: 60_000,
  });
}

export function useRuns(user: string, state?: string, search?: string, limit = "20") {
  return useQuery({
    queryKey: ["runs", user, state ?? "", search ?? "", limit],
    queryFn: ({ signal }) => api.runs(user, { state, q: search, limit }, signal),
    refetchInterval: 15_000,
  });
}

export function useRunPages(user: string, state?: string, search?: string) {
  return useInfiniteQuery({
    queryKey: ["runs", user, "pages", state ?? "", search ?? ""],
    queryFn: ({ signal, pageParam }) => api.runs(user, {
      state,
      q: search,
      limit: "20",
      cursor: pageParam ?? undefined,
    }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.page.next_cursor ?? undefined,
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

export function useRunWorkspace(user: string, runId: string | null) {
  return useQuery({
    queryKey: ["run-workspace", user, runId],
    queryFn: ({ signal }) => api.runWorkspace(user, runId ?? "", signal),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const workspace = query.state.data;
      if (!workspace) return 10_000;
      if (
        ["SUBMITTING", "SUBMITTED", "PENDING", "RUNNING", "COMPLETING", "UNKNOWN"].includes(
          workspace.states.execution,
        )
      ) return 5_000;
      if (["pending", "running"].includes(workspace.states.collection)) return 5_000;
      return false;
    },
  });
}

export function useRunEvents(user: string, runId: string | null) {
  return useQuery({
    queryKey: ["run-events", user, runId],
    queryFn: ({ signal }) => api.runEvents(user, runId ?? "", signal),
    enabled: Boolean(runId),
    refetchInterval: 10_000,
  });
}

export function useRunLineage(user: string, runId: string | null) {
  return useQuery({
    queryKey: ["run-lineage", user, runId],
    queryFn: ({ signal }) => api.runLineage(user, runId ?? "", signal),
    enabled: Boolean(runId),
    refetchInterval: 15_000,
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

export function useRuntimeWatch(
  user: string,
  runId: string | null,
  visibility: RuntimeViewerVisibility = "visible",
) {
  return useQuery({
    queryKey: ["runtime-watch", user, runId],
    queryFn: ({ signal }) => api.runtimeWatch(user, runId ?? "", signal),
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: (query) =>
      runtimePollingInterval("summary", query.state.data?.state, visibility),
    refetchIntervalInBackground: false,
  });
}

export function useRuntimeWatchLogs(
  user: string,
  runId: string | null,
  stream: "stdout" | "stderr",
  watchState: RuntimeWatchState | null = null,
  visibility: RuntimeViewerVisibility = "visible",
) {
  return useQuery({
    queryKey: ["runtime-watch-logs", user, runId, stream],
    queryFn: ({ signal }) => api.runtimeWatchLogs(user, runId ?? "", stream, signal),
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: () => runtimePollingInterval("logs", watchState, visibility),
    refetchIntervalInBackground: false,
  });
}

export function useRuntimeWatchAlerts(
  user: string,
  runId: string | null,
  watchState: RuntimeWatchState | null = null,
  visibility: RuntimeViewerVisibility = "visible",
) {
  return useQuery({
    queryKey: ["runtime-watch-alerts", user, runId],
    queryFn: ({ signal }) => api.runtimeWatchAlerts(user, runId ?? "", signal),
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: () => runtimePollingInterval("alerts", watchState, visibility),
    refetchIntervalInBackground: false,
  });
}

export function useRunResources(user: string, runId: string | null) {
  return useQuery({
    queryKey: ["run-resources", user, runId],
    queryFn: ({ signal }) => api.runResources(user, runId ?? "", signal),
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.freshness === "terminal" ? false : 10_000,
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

export function useRecipeVersion(
  user: string,
  recipeId: string | null,
  version: string | null,
) {
  return useQuery({
    queryKey: ["recipe-version", user, recipeId, version],
    queryFn: ({ signal }) => api.recipeVersion(user, recipeId ?? "", version ?? "", signal),
    enabled: Boolean(recipeId && version),
    staleTime: 60_000,
  });
}

export function useTemplateDrafts(user: string) {
  return useQuery({
    queryKey: ["template-drafts", user],
    queryFn: ({ signal }) => api.templateDrafts(user, {}, signal),
    staleTime: 15_000,
  });
}

export function useTemplateDraft(user: string, draftId: string | null) {
  return useQuery({
    queryKey: ["template-draft", user, draftId],
    queryFn: ({ signal }) => api.templateDraft(user, draftId ?? "", signal),
    enabled: Boolean(draftId),
    staleTime: 15_000,
  });
}

export function useTemplateReviews(user: string) {
  return useQuery({
    queryKey: ["template-reviews", user],
    queryFn: ({ signal }) => api.templateReviews(user, {}, signal),
    staleTime: 15_000,
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

export function useSuccessfulRunMarket(
  user: string,
  filters: {
    q?: string | undefined;
    visibility?: string | undefined;
    tag?: string | undefined;
    limit?: string | undefined;
  },
) {
  return useQuery({
    queryKey: ["successful-run-market", user, filters],
    queryFn: ({ signal }) => api.successfulRunMarket(user, filters, signal),
    staleTime: 15_000,
  });
}

export function useMarketItems(
  user: string,
  filters: {
    q?: string | undefined;
    kind?: string | undefined;
    visibility?: string | undefined;
    tag?: string | undefined;
    limit?: string | undefined;
  },
) {
  return useQuery({
    queryKey: ["market-items", user, filters],
    queryFn: ({ signal }) => api.marketItems(user, filters, signal),
    staleTime: 15_000,
  });
}

export function useMarketItem(user: string, itemId: string | null) {
  return useQuery({
    queryKey: ["market-item", user, itemId],
    queryFn: ({ signal }) => api.marketItem(user, itemId ?? "", signal),
    enabled: Boolean(itemId),
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

export function useAgentSessions(user: string) {
  return useQuery({
    queryKey: ["agent-sessions", user],
    queryFn: ({ signal }) => api.agentSessions(user, signal),
    refetchInterval: 10_000,
  });
}

export function useAgentSessionTasks(user: string, sessionId: string | null) {
  return useQuery({
    queryKey: ["agent-tasks", user, sessionId],
    queryFn: ({ signal }) => api.agentSessionTasks(user, sessionId ?? "", signal),
    enabled: Boolean(sessionId),
    retry: false,
    refetchInterval: (query) => agentTaskPollInterval(query.state.data?.items ?? []),
  });
}

export function agentTaskPollInterval(tasks: readonly AgentTask[]): number | false {
  return tasks.some((task) => task.state === "pending" || task.state === "running")
    ? 2_000
    : false;
}

export function useAgentProjects(user: string) {
  return useQuery({
    queryKey: ["agent-projects", user],
    queryFn: ({ signal }) => api.agentProjects(user, signal),
    refetchInterval: 10_000,
  });
}

export function useAgentProject(user: string, projectId: string | null) {
  return useQuery({
    queryKey: ["agent-project", user, projectId],
    queryFn: ({ signal }) => api.agentProject(user, projectId ?? "", signal),
    enabled: Boolean(projectId),
    refetchInterval: 5_000,
  });
}

export function useAgentChangeSetDiff(
  user: string,
  projectId: string | null,
  workspaceId: string | null,
  changeSetId: string | null,
) {
  return useQuery({
    queryKey: ["agent-changeset-diff", user, changeSetId],
    queryFn: ({ signal }) => api.agentChangeSetDiff(
      user,
      projectId ?? "",
      workspaceId ?? "",
      changeSetId ?? "",
      signal,
    ),
    enabled: Boolean(projectId && workspaceId && changeSetId),
  });
}

export function useAgentSession(user: string, sessionId: string | null) {
  return useQuery({
    queryKey: ["agent-session", user, sessionId],
    queryFn: ({ signal }) => api.agentSession(user, sessionId ?? "", signal),
    enabled: Boolean(sessionId),
    refetchInterval: 3_000,
  });
}

export function useAgentSessionEvents(
  user: string,
  sessionId: string | null,
  afterEventId: number,
) {
  return useQuery({
    queryKey: ["agent-session-events", user, sessionId, afterEventId],
    queryFn: ({ signal }) => api.agentSessionEvents(
      user,
      sessionId ?? "",
      afterEventId,
      signal,
    ),
    enabled: Boolean(sessionId),
    refetchInterval: 2_000,
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

// ---------------------------------------------------------------------------
// M2: Repair Tickets
// ---------------------------------------------------------------------------

export function useRepairTickets(user: string, state?: string, sessionId?: string) {
  return useQuery({
    queryKey: ["repair-tickets", user, state ?? "", sessionId ?? ""],
    queryFn: ({ signal }) => api.repairTickets(user, state, sessionId, signal),
    refetchInterval: 15_000,
  });
}

export function useRepairTicket(user: string, ticketId: string | null) {
  return useQuery({
    queryKey: ["repair-ticket", user, ticketId],
    queryFn: ({ signal }) => api.repairTicket(user, ticketId ?? "", signal),
    enabled: Boolean(ticketId),
    refetchInterval: 15_000,
  });
}
