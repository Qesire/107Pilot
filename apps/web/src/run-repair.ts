import type { RemediationSession } from "./types";
import type { RunWorkspace } from "./run-workspace";

export const activeRepairStates = new Set([
  "waiting_evidence",
  "diagnosing",
  "planning",
  "preparing",
  "executing",
  "evaluating",
]);

export const terminalRepairStates = new Set([
  "succeeded",
  "blocked",
  "exhausted",
  "failed",
  "cancelled",
]);

export function sessionsForRun(
  sessions: readonly RemediationSession[],
  runId: string,
): RemediationSession[] {
  return sessions
    .filter((session) => session.source_run_id === runId)
    .slice()
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
}

export function approvedProposalIds(session: RemediationSession): Set<string> {
  return new Set(
    session.decisions
      .filter((decision) => decision.decision === "approve")
      .map((decision) => decision.proposal_id),
  );
}

export function canOpenRepair(workspace: RunWorkspace): boolean {
  return workspace.next_action.kind === "prepare_repair"
    && workspace.evidence_summary.diagnosis_count > 0;
}

export function derivedRunHref(
  user: string,
  sourceRunId: string,
  derivedRunId: string,
): string {
  return `/runs/${encodeURIComponent(derivedRunId)}?user=${encodeURIComponent(user)}&tab=compare&compare=${encodeURIComponent(sourceRunId)}`;
}

export function repairProjectHref(
  user: string,
  projectId: string,
  remediationSessionId: string,
  sourceRunId: string,
): string {
  const search = new URLSearchParams({
    user,
    mode: "builder",
    project: projectId,
    repair_session: remediationSessionId,
    repair_run: sourceRunId,
  });
  return `/agent?${search.toString()}`;
}
