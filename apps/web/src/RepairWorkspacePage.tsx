import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  GitCompare,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { api } from "./api";
import { QueryBoundary, StatusBadge, formatTimestamp } from "./components";
import { ExperimentShell } from "./ExperimentShell";
import { useRun } from "./query";
import {
  type RepairWorkspace,
  type RepairWorkspaceDerivedRun,
  useRepairWorkspace,
} from "./repair-workspace";
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
  if (["FAILED", "SUBMIT_FAILED", "failed", "abandoned", "rejected"].includes(state)) return "danger";
  if (["awaiting_approval", "PENDING", "SUBMITTED", "planning", "ready"].includes(state)) return "warning";
  if (["RUNNING", "COMPLETING", "executing", "evaluating"].includes(state)) return "info";
  return "neutral";
}

function latestDerived(workspace: RepairWorkspace): RepairWorkspaceDerivedRun | null {
  if (!workspace.derived_runs.length) return null;
  return workspace.derived_runs[workspace.derived_runs.length - 1] ?? null;
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
  const startRepair = useMutation({
    mutationFn: () =>
      api.createRemediationSession(
        user,
        runId,
        `repair-workspace:${runId}:${crypto.randomUUID()}`,
        "none",
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["repair-workspace", user, runId] });
    },
  });

  const openRunTab = (tab: string, extra: Record<string, string | null> = {}) => {
    navigate(withSearch(`/runs/${encodeURIComponent(runId)}`, location.search, {
      tab,
      object: null,
      ...extra,
    }));
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
        if (latestSession) {
          navigate(
            `/agent?user=${encodeURIComponent(user)}&session=${encodeURIComponent(latestSession.session_id)}`,
          );
        }
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
                原始模型上下文、代码正文和 shell 输出不会进入这个聚合接口。
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
                  <h3 id="repair-session-heading">修复会话与 Agent</h3>
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
                        onClick={() => navigate(
                          `/agent?user=${encodeURIComponent(user)}&session=${encodeURIComponent(session.session_id)}`,
                        )}
                      >
                        打开受控 Agent 会话
                      </button>
                    </article>
                  ))}
                </div>
              ) : (
                <p className="limitation">尚未创建修复会话。创建动作只建立 owner-scoped 会话，不会直接修改源工作区。</p>
              )}
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
