import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle2,
  GitCompare,
  ShieldCheck,
  Wrench,
  XCircle,
} from "lucide-react";
import { api } from "./api";
import { QueryBoundary, StatusBadge, formatTimestamp } from "./components";
import { ExperimentShell } from "./ExperimentShell";
import { useRemediationSession, useRun } from "./query";
import {
  type RepairWorkspace,
  type RepairWorkspaceDerivedRun,
  useRepairWorkspace,
} from "./repair-workspace";
import type { RemediationProposal, RemediationSession } from "./types";
import type { LocationState } from "./url";
import { withSearch } from "./url";

interface RepairWorkspacePageProps {
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
  runId: string;
}

function toneForState(state: string): "neutral" | "info" | "success" | "warning" | "danger" {
  if (["SUCCEEDED", "succeeded", "resolved", "approved"].includes(state)) return "success";
  if ([
    "FAILED",
    "SUBMIT_FAILED",
    "failed",
    "abandoned",
    "rejected",
    "error",
    "critical",
  ].includes(state)) return "danger";
  if (["awaiting_approval", "PENDING", "SUBMITTED", "planning", "ready"].includes(state)) return "warning";
  if (["RUNNING", "COMPLETING", "executing", "evaluating"].includes(state)) return "info";
  return "neutral";
}

function latestDerived(workspace: RepairWorkspace): RepairWorkspaceDerivedRun | null {
  if (!workspace.derived_runs.length) return null;
  return workspace.derived_runs[workspace.derived_runs.length - 1] ?? null;
}

function proposalPatchRows(payload: RemediationProposal["payload"]) {
  const direct = payload.proposed_patch;
  const parameters = payload.parameters;
  const nested = parameters && typeof parameters === "object" && !Array.isArray(parameters)
    ? (parameters as Record<string, unknown>).patch
    : null;
  const patch = direct && typeof direct === "object" && !Array.isArray(direct)
    ? direct as Record<string, unknown>
    : nested && typeof nested === "object" && !Array.isArray(nested)
      ? nested as Record<string, unknown>
      : null;
  if (!patch) return [];
  return Object.entries(patch)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([field, value]) => ({
      field,
      value: value === null
        ? "需要输入"
        : typeof value === "string"
          ? value
          : JSON.stringify(value),
    }));
}

export function RepairWorkspacePage({
  user,
  location,
  navigate,
  runId,
}: RepairWorkspacePageProps) {
  const queryClient = useQueryClient();
  const run = useRun(user, runId);
  const repair = useRepairWorkspace(user, runId);
  const latestSessionId = repair.data?.remediation_sessions[0]?.session_id ?? null;
  const sessionDetail = useRemediationSession(user, latestSessionId);

  const refreshSession = (updated?: RemediationSession) => {
    if (updated) {
      queryClient.setQueryData(
        ["remediation-session", user, updated.session_id],
        updated,
      );
    }
    void queryClient.invalidateQueries({ queryKey: ["repair-workspace", user, runId] });
    void queryClient.invalidateQueries({ queryKey: ["remediation-sessions", user] });
    if (latestSessionId) {
      void queryClient.invalidateQueries({
        queryKey: ["remediation-session", user, latestSessionId],
      });
    }
  };

  const startRepair = useMutation({
    mutationFn: () =>
      api.createRemediationSession(
        user,
        runId,
        `repair-workspace:${runId}:${crypto.randomUUID()}`,
        "none",
      ),
    onSuccess: (session) => refreshSession(session),
  });

  const approveProposal = useMutation({
    mutationFn: (proposal: RemediationProposal) => {
      const detail = sessionDetail.data;
      if (!detail) throw new Error("修复会话详情尚未加载");
      return api.approveRemediationAction(
        user,
        detail.session_id,
        proposal.proposal_id,
        detail.version,
      );
    },
    onSuccess: refreshSession,
  });

  const rejectProposal = useMutation({
    mutationFn: (proposal: RemediationProposal) => {
      const detail = sessionDetail.data;
      if (!detail) throw new Error("修复会话详情尚未加载");
      return api.rejectRemediationAction(
        user,
        detail.session_id,
        proposal.proposal_id,
        detail.version,
      );
    },
    onSuccess: refreshSession,
  });

  const openRunTab = (tab: string, extra: Record<string, string | null> = {}) => {
    navigate(withSearch(`/runs/${encodeURIComponent(runId)}`, location.search, {
      tab,
      object: null,
      ...extra,
    }));
  };

  const openFullRepairSession = (sessionId: string) => {
    navigate(withSearch("/agent", location.search, {
      mode: "repair",
      session: sessionId,
      state: null,
      project: null,
    }));
  };

  const focusRepairSession = () => {
    document.getElementById("repair-session-heading")?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  };

  const performNextAction = (workspace: RepairWorkspace) => {
    const latestSession = workspace.remediation_sessions[0] ?? null;
    const derived = latestDerived(workspace);
    switch (workspace.next_action.kind) {
      case "start_repair":
        startRepair.mutate();
        return;
      case "review_proposal":
      case "continue_repair":
        if (latestSession) focusRepairSession();
        return;
      case "watch_derived_run":
        if (derived) {
          navigate(
            `/runs/${encodeURIComponent(derived.run_id)}?user=${encodeURIComponent(user)}&tab=overview`,
          );
        }
        return;
      case "compare_outcome":
        if (derived) openRunTab("compare", { compare: derived.run_id });
        return;
      case "inspect_failure":
        openRunTab("logs");
        return;
      case "no_repair_needed":
        openRunTab("overview");
    }
  };

  const approvalError = approveProposal.error ?? rejectProposal.error;
  const approvedProposalIds = new Set(
    (sessionDetail.data?.decisions ?? [])
      .filter((decision) => decision.decision === "approve")
      .map((decision) => decision.proposal_id),
  );
  const rejectedProposalIds = new Set(
    (sessionDetail.data?.decisions ?? [])
      .filter((decision) => decision.decision === "reject")
      .map((decision) => decision.proposal_id),
  );

  return (
    <QueryBoundary
      pending={run.isPending || repair.isPending}
      error={run.error ?? repair.error}
    >
      {run.data && repair.data ? (
        <ExperimentShell
          user={user}
          location={location}
          navigate={navigate}
          context={{ kind: "run", run: run.data }}
        >
          <div className="evidence-section" data-testid="repair-workspace">
            <section className="panel" aria-labelledby="repair-workspace-heading">
              <div className="panel-heading">
                <div>
                  <p className="panel-kicker">Failure recovery</p>
                  <h2 id="repair-workspace-heading">修复工作区</h2>
                </div>
                <StatusBadge
                  label={repair.data.source_run.state}
                  tone={toneForState(repair.data.source_run.state)}
                />
              </div>
              <p>
                Diagnosis、Agent 建议、审批、Repair Ticket 与派生 Run 在这里按同一失败链聚合；
                原始模型上下文、代码正文和 shell 输出不会进入聚合接口。需要审批时，本页再按需读取权威修复会话详情。
              </p>
              <div className="agent-action-row">
                <button
                  className="button secondary"
                  type="button"
                  onClick={() => openRunTab("diagnosis")}
                >
                  查看诊断
                </button>
                <button
                  className="button primary"
                  type="button"
                  disabled={startRepair.isPending}
                  onClick={() => performNextAction(repair.data)}
                >
                  <Wrench aria-hidden="true" size={15} />
                  {startRepair.isPending ? "正在创建修复会话" : repair.data.next_action.label}
                  <ArrowRight aria-hidden="true" size={15} />
                </button>
              </div>
              {startRepair.isError ? (
                <p className="limitation" role="alert">{startRepair.error.message}</p>
              ) : null}
              <p className="panel-footnote">{repair.data.next_action.detail}</p>
            </section>

            <section className="panel" aria-labelledby="repair-status-heading">
              <div className="panel-heading">
                <div>
                  <p className="panel-kicker">Recovery state</p>
                  <h3 id="repair-status-heading">闭环状态</h3>
                </div>
                <ShieldCheck aria-hidden="true" size={18} />
              </div>
              <dl className="experiment-run-summary-grid">
                <div><dt>持久化诊断</dt><dd>{repair.data.diagnoses.length}</dd></div>
                <div><dt>修复会话</dt><dd>{repair.data.remediation_sessions.length}</dd></div>
                <div><dt>Agent 建议</dt><dd>{repair.data.agent.advice.length}</dd></div>
                <div><dt>Repair Ticket</dt><dd>{repair.data.repair_tickets.length}</dd></div>
                <div><dt>派生 Run</dt><dd>{repair.data.derived_runs.length}</dd></div>
                <div><dt>等待审批</dt><dd>{repair.data.status.awaiting_approval ? "是" : "否"}</dd></div>
              </dl>
              {Object.values(repair.data.truncation).some(Boolean) ? (
                <p className="limitation" role="status">
                  当前聚合达到读取上限；这里只展示最近对象，权威记录仍保存在各自的持久化模型中。
                </p>
              ) : null}
            </section>

            <section className="panel" aria-labelledby="repair-diagnosis-heading">
              <div className="panel-heading">
                <div>
                  <p className="panel-kicker">Evidence → Diagnosis</p>
                  <h3 id="repair-diagnosis-heading">失败依据</h3>
                </div>
                <AlertTriangle aria-hidden="true" size={18} />
              </div>
              {repair.data.diagnoses.length ? (
                <div className="diagnosis-list">
                  {repair.data.diagnoses.map((item) => (
                    <article key={item.diagnosis_id}>
                      <header>
                        <StatusBadge label={item.severity} tone={toneForState(item.severity)} />
                        <span>{item.rule_id}</span>
                        <small>{item.confidence}</small>
                      </header>
                      <h4>{item.summary}</h4>
                      <div className="diagnosis-evidence">
                        {item.evidence_refs.map((ref) => <span key={ref} className="mono">{ref}</span>)}
                      </div>
                    </article>
                  ))}
                </div>
              ) : (
                <p className="limitation">尚无持久化诊断。先回到日志与 Evidence，避免无依据地启动自动修复。</p>
              )}
            </section>

            <section className="panel" aria-labelledby="repair-session-heading">
              <div className="panel-heading">
                <div>
                  <p className="panel-kicker">Controlled remediation</p>
                  <h3 id="repair-session-heading">修复会话、Agent 与审批</h3>
                </div>
                <Bot aria-hidden="true" size={18} />
              </div>
              {repair.data.remediation_sessions.length ? (
                <div className="lineage-nodes">
                  {repair.data.remediation_sessions.map((session) => (
                    <article key={session.session_id}>
                      <span>
                        <StatusBadge label={session.state} tone={toneForState(session.state)} />
                        <small>{session.provider}</small>
                      </span>
                      <strong className="mono wrap-anywhere">{session.session_id}</strong>
                      <small>{session.automation_policy} · v{session.version}</small>
                      <small>{formatTimestamp(session.updated_at)}</small>
                      <button
                        type="button"
                        onClick={() => openFullRepairSession(session.session_id)}
                      >
                        打开完整修复会话
                      </button>
                    </article>
                  ))}
                </div>
              ) : (
                <p className="limitation">尚未创建修复会话。创建动作只建立 owner-scoped 会话，不会直接修改源工作区。</p>
              )}

              {latestSessionId && sessionDetail.isPending ? (
                <p className="limitation" role="status">正在读取最新修复会话的权威详情…</p>
              ) : null}
              {sessionDetail.error ? (
                <p className="limitation" role="alert">无法读取修复会话详情：{sessionDetail.error.message}</p>
              ) : null}
              {approvalError ? (
                <p className="limitation" role="alert">审批操作失败：{approvalError.message}</p>
              ) : null}

              {sessionDetail.data ? (
                <section className="agent-proposals" id="repair-proposals" aria-labelledby="repair-proposals-heading">
                  <div className="panel-heading">
                    <div>
                      <p className="panel-kicker">Review gate</p>
                      <h4 id="repair-proposals-heading">当前动作建议</h4>
                    </div>
                    <StatusBadge
                      label={sessionDetail.data.state}
                      tone={toneForState(sessionDetail.data.state)}
                    />
                  </div>
                  {sessionDetail.data.proposals.length ? sessionDetail.data.proposals.map((proposal) => {
                    const approved = approvedProposalIds.has(proposal.proposal_id);
                    const rejected = rejectedProposalIds.has(proposal.proposal_id);
                    const rows = proposalPatchRows(proposal.payload);
                    const mutationPending = approveProposal.isPending || rejectProposal.isPending;
                    return (
                      <article key={proposal.proposal_id}>
                        <header>
                          <div>
                            <StatusBadge label={proposal.risk} tone={toneForState(proposal.risk)} />
                            <strong>{proposal.action_type}</strong>
                          </div>
                          <small>{proposal.policy_status}</small>
                        </header>
                        <p>来源：{proposal.source} · action {proposal.action_id}</p>
                        {rows.length ? (
                          <dl className="agent-proposal-diff">
                            {rows.map((row) => (
                              <div key={row.field}>
                                <dt>{row.field}</dt>
                                <dd className="mono wrap-anywhere">{row.value}</dd>
                              </div>
                            ))}
                          </dl>
                        ) : null}
                        <details>
                          <summary>完整动作参数</summary>
                          <pre><code>{JSON.stringify(proposal.payload, null, 2)}</code></pre>
                        </details>
                        <div className="agent-action-row">
                          {approved ? <StatusBadge label="已批准" tone="success" /> : null}
                          {rejected ? <StatusBadge label="已拒绝" tone="danger" /> : null}
                          {sessionDetail.data?.state === "awaiting_approval" && !approved && !rejected ? (
                            <>
                              <button
                                className="button secondary"
                                type="button"
                                disabled={mutationPending}
                                onClick={() => rejectProposal.mutate(proposal)}
                              >
                                <XCircle aria-hidden="true" size={15} />拒绝
                              </button>
                              <button
                                className="button primary"
                                type="button"
                                disabled={mutationPending}
                                onClick={() => approveProposal.mutate(proposal)}
                              >
                                <CheckCircle2 aria-hidden="true" size={15} />批准此动作
                              </button>
                            </>
                          ) : null}
                        </div>
                      </article>
                    );
                  }) : (
                    <p className="limitation">当前修复会话尚未形成可审阅动作。可以留在本页等待状态推进，或打开完整修复会话查看详细审计轨迹。</p>
                  )}
                </section>
              ) : null}

              {repair.data.agent.advice.length ? (
                <dl className="fact-list">
                  {repair.data.agent.advice.map((advice) => (
                    <div key={advice.advice_id}>
                      <dt>{advice.provider}{advice.model ? ` · ${advice.model}` : ""}</dt>
                      <dd>
                        <StatusBadge label={advice.state} tone={toneForState(advice.state)} />
                        <span className="mono wrap-anywhere">{advice.advice_id}</span>
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : null}
            </section>

            <section className="panel" aria-labelledby="repair-ticket-heading">
              <div className="panel-heading">
                <div>
                  <p className="panel-kicker">Reviewable change</p>
                  <h3 id="repair-ticket-heading">Repair Ticket</h3>
                </div>
                <Wrench aria-hidden="true" size={18} />
              </div>
              {repair.data.repair_tickets.length ? (
                <div className="diagnosis-list">
                  {repair.data.repair_tickets.map((ticket) => (
                    <article key={ticket.ticket_id}>
                      <header>
                        <StatusBadge label={ticket.state} tone={toneForState(ticket.state)} />
                        <span className="mono">{ticket.ticket_id}</span>
                      </header>
                      <h4>{ticket.requested_change ?? "未提供变更摘要"}</h4>
                      <p>绑定诊断：{ticket.diagnosis_ids.join(", ") || "未绑定"}</p>
                      {ticket.resolution_run_id ? (
                        <button
                          type="button"
                          onClick={() => navigate(
                            `/runs/${encodeURIComponent(ticket.resolution_run_id ?? "")}?user=${encodeURIComponent(user)}&tab=overview`,
                          )}
                        >
                          查看 resolution Run
                        </button>
                      ) : null}
                    </article>
                  ))}
                </div>
              ) : (
                <p className="limitation">尚无 Repair Ticket；Agent 建议不等同于已经批准的代码变更。</p>
              )}
            </section>

            <section className="panel" aria-labelledby="repair-derived-heading">
              <div className="panel-heading">
                <div>
                  <p className="panel-kicker">Validation</p>
                  <h3 id="repair-derived-heading">派生 Run 与前后对比</h3>
                </div>
                <GitCompare aria-hidden="true" size={18} />
              </div>
              {repair.data.derived_runs.length ? (
                <div className="lineage-nodes">
                  {repair.data.derived_runs.map((derived) => (
                    <article key={derived.run_id}>
                      <span>
                        <StatusBadge label={derived.state} tone={toneForState(derived.state)} />
                        <small>attempt {derived.attempt}</small>
                      </span>
                      <strong className="mono wrap-anywhere">{derived.run_id}</strong>
                      <small>{derived.lineage_reason ?? "derived"}</small>
                      <div className="agent-action-row">
                        <button
                          type="button"
                          onClick={() => navigate(
                            `/runs/${encodeURIComponent(derived.run_id)}?user=${encodeURIComponent(user)}&tab=overview`,
                          )}
                        >
                          查看 Run
                        </button>
                        <button
                          type="button"
                          onClick={() => openRunTab("compare", { compare: derived.run_id })}
                        >
                          与源 Run 对比
                        </button>
                      </div>
                    </article>
                  ))}
                </div>
              ) : (
                <p className="limitation">尚无派生 Run。只有经过审批并形成新的运行 lineage 后，才进入验证阶段。</p>
              )}
              {repair.data.status.has_successful_derived_run ? (
                <p className="panel-footnote">
                  派生 Run 的计算成功只说明执行终态；仍需检查 Evidence 与科学结果，不能自动判定修复有效。
                </p>
              ) : null}
            </section>
          </div>
        </ExperimentShell>
      ) : null}
    </QueryBoundary>
  );
}
