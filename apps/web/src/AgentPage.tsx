import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ArrowRight, Ban, Bot, CheckCircle2, Play, RefreshCw, ShieldAlert, XCircle } from "lucide-react";
import { api } from "./api";
import { QueryBoundary, SectionHeading, StatusBadge, formatTimestamp } from "./components";
import { useRemediationSession, useRemediationSessions } from "./query";
import type { RemediationProposal, RemediationSession, RemediationState } from "./types";
import type { LocationState } from "./url";
import { withSearch } from "./url";

interface AgentPageProps {
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
}

const activeStates = new Set<RemediationState>([
  "waiting_evidence",
  "diagnosing",
  "planning",
  "preparing",
  "executing",
  "evaluating",
]);

const terminalStates = new Set<RemediationState>([
  "succeeded",
  "blocked",
  "exhausted",
  "failed",
  "cancelled",
]);

export function AgentPage({ user, location, navigate }: AgentPageProps) {
  const state = location.search.get("state") ?? "";
  const requestedSession = location.search.get("session");
  const sessions = useRemediationSessions(user, state || undefined);
  const selectedId = requestedSession ?? sessions.data?.items[0]?.session_id ?? null;
  const detail = useRemediationSession(user, selectedId);
  const selectSession = (sessionId: string) =>
    navigate(withSearch("/agent", location.search, { session: sessionId }));

  return (
    <>
      <SectionHeading
        eyebrow="Agent / Remediation"
        title="可审计的修复会话"
        detail="规则先提出动作；风险、预算、审批、派生 Run 和评价结果保持在同一条 lineage 中。"
      />
      <section className="filter-bar" aria-label="Agent 会话筛选">
        <label className="select-field">
          <Bot aria-hidden="true" size={16} />
          <span className="sr-only">会话状态</span>
          <select
            value={state}
            onChange={(event) => navigate(withSearch("/agent", location.search, {
              state: event.target.value || null,
              session: null,
            }))}
          >
            <option value="">全部状态</option>
            <option value="awaiting_approval">等待审批</option>
            <option value="executing">执行中</option>
            <option value="blocked">需要接管</option>
            <option value="succeeded">已验证成功</option>
            <option value="exhausted">预算耗尽</option>
          </select>
        </label>
      </section>
      <div className="agent-layout">
        <section className="panel agent-queue" aria-labelledby="agent-queue-heading">
          <div className="panel-heading">
            <div><p className="panel-kicker">Session queue</p><h2 id="agent-queue-heading">{sessions.data?.items.length ?? 0} 个会话</h2></div>
            {sessions.isFetching ? <StatusBadge label="同步中" tone="info" /> : null}
          </div>
          <QueryBoundary
            pending={sessions.isPending}
            error={sessions.error}
            empty={(sessions.data?.items.length ?? 0) === 0}
            emptyTitle="还没有修复会话"
            emptyDetail="从失败 Run 的诊断页启动；Agent 不会扫描或修改其他用户的作业。"
          >
            <div className="agent-session-list">
              {(sessions.data?.items ?? []).map((session) => (
                <button
                  key={session.session_id}
                  type="button"
                  className={session.session_id === selectedId ? "active" : undefined}
                  onClick={() => selectSession(session.session_id)}
                >
                  <span><StatusBadge label={remediationStateLabel(session.state)} tone={remediationStateTone(session.state)} /><small>{formatTimestamp(session.updated_at)}</small></span>
                  <strong className="mono wrap-anywhere">{session.source_run_id}</strong>
                  <small>{session.usage.attempts}/{session.budget.max_attempts} attempts · {session.usage.submissions}/{session.budget.max_submissions} submissions</small>
                </button>
              ))}
            </div>
          </QueryBoundary>
        </section>
        <section className="panel agent-detail" aria-labelledby="agent-detail-heading">
          <div className="panel-heading"><div><p className="panel-kicker">Selected session</p><h2 id="agent-detail-heading">会话详情</h2></div></div>
          <QueryBoundary
            pending={Boolean(selectedId) && detail.isPending}
            error={detail.error}
            empty={!selectedId}
            emptyTitle="选择一个会话"
            emptyDetail="队列会保留审批、执行和评价的完整审计记录。"
          >
            {detail.data ? <SessionDetail user={user} session={detail.data} /> : null}
          </QueryBoundary>
        </section>
      </div>
    </>
  );
}

function SessionDetail({ user, session }: { user: string; session: RemediationSession }) {
  const queryClient = useQueryClient();
  const [provider, setProvider] = useState<LlmProvider>("local");
  const refresh = (updated?: RemediationSession) => {
    if (updated) queryClient.setQueryData(
      ["remediation-session", user, session.session_id],
      updated,
    );
    void queryClient.invalidateQueries({ queryKey: ["remediation-sessions", user] });
    void queryClient.invalidateQueries({
      queryKey: ["remediation-session", user, session.session_id],
    });
  };
  const advance = useMutation({
    mutationFn: () => api.advanceRemediationSession(
      user,
      session.session_id,
      undefined,
      { provider },
    ),
    onSuccess: refresh,
  });
  const approve = useMutation({
    mutationFn: (proposal: RemediationProposal) => api.approveRemediationAction(
      user,
      session.session_id,
      proposal.proposal_id,
      session.version,
    ),
    onSuccess: refresh,
  });
  const reject = useMutation({
    mutationFn: (proposal: RemediationProposal) => api.rejectRemediationAction(
      user,
      session.session_id,
      proposal.proposal_id,
      session.version,
    ),
    onSuccess: refresh,
  });
  const cancel = useMutation({
    mutationFn: () => api.cancelRemediationSession(
      user,
      session.session_id,
      session.version,
    ),
    onSuccess: refresh,
  });
  const takeover = useMutation({
    mutationFn: () => api.takeoverRemediationSession(
      user,
      session.session_id,
      session.version,
      "在 Contract Studio 中人工派生后继续",
    ),
    onSuccess: refresh,
  });
  const execute = useMutation({
    mutationFn: (proposal: RemediationProposal) => api.executeRemediationAction(
      user,
      session.session_id,
      proposal.proposal_id,
      session.version,
    ),
    onSuccess: refresh,
  });
  const error = advance.error ?? approve.error ?? reject.error ?? cancel.error
    ?? takeover.error ?? execute.error;
  const decided = new Set(session.decisions.filter((item) => item.decision === "approve").map((item) => item.proposal_id));

  return (
    <div className="agent-session-detail">
      <header className="agent-session-heading">
        <div><StatusBadge label={remediationStateLabel(session.state)} tone={remediationStateTone(session.state)} /><p className="mono wrap-anywhere">{session.session_id}</p></div>
        {!terminalStates.has(session.state) ? (
          <div className="agent-session-controls">
            {activeStates.has(session.state) ? (
              <>
                <label className="select-field">
                  <Bot aria-hidden="true" size={16} />
                  <span className="sr-only">LLM provider</span>
                  <select
                    value={provider}
                    onChange={(event) => setProvider(event.target.value as LlmProvider)}
                    aria-label="LLM provider"
                    className="llm-provider-select"
                  >
                    <option value="local">{providerLabel("local")}</option>
                    <option value="none">{providerLabel("none")}</option>
                  </select>
                </label>
                <button className="button secondary" type="button" disabled={advance.isPending} onClick={() => advance.mutate()}>
                  <RefreshCw aria-hidden="true" size={15} className={advance.isPending ? "spin" : undefined} />推进状态
                </button>
              </>
            ) : null}
            <button className="button danger" type="button" disabled={cancel.isPending} onClick={() => cancel.mutate()}><Ban aria-hidden="true" size={15} />取消会话</button>
          </div>
        ) : null}
      </header>
      {error ? <div className="studio-notice error" role="alert"><ShieldAlert aria-hidden="true" /><div><strong>会话动作失败</strong><p>{error.message}</p></div></div> : null}
      {session.stop_reason ? <div className="studio-notice warning"><ShieldAlert aria-hidden="true" /><div><strong>停止原因</strong><p>{session.stop_reason}</p></div></div> : null}
      {session.state === "awaiting_input" && session.source_contract_id ? (
        <div className="studio-notice warning">
          <ShieldAlert aria-hidden="true" />
          <div>
            <strong>规则缺少安全的具体值</strong>
            <p>请从 source Contract 派生新版本；系统不会猜测命令、路径或依赖。</p>
            <div className="agent-action-row">
              <a href={`/studio/${encodeURIComponent(session.source_contract_id)}?user=${encodeURIComponent(user)}`}>打开 Contract Studio</a>
              <button className="button secondary" type="button" disabled={takeover.isPending} onClick={() => takeover.mutate()}>标记人工接管</button>
            </div>
          </div>
        </div>
      ) : null}
      <dl className="fact-list">
        <div><dt>Source Run</dt><dd className="mono wrap-anywhere">{session.source_run_id}</dd></div>
        <div><dt>Source Contract</dt><dd className="mono wrap-anywhere">{session.source_contract_id ?? "—"}</dd></div>
        <div><dt>Policy</dt><dd>{session.automation_policy}</dd></div>
        <div><dt>Version</dt><dd>{session.version}</dd></div>
      </dl>
      <section className="agent-budget" aria-label="自动化预算">
        <Budget label="尝试" used={session.usage.attempts} maximum={session.budget.max_attempts} />
        <Budget label="提交" used={session.usage.submissions} maximum={session.budget.max_submissions} />
        <Budget label="LLM calls" used={session.usage.llm_calls} maximum={session.budget.max_llm_calls} />
        <Budget label="Wall time" used={session.usage.wall_time_seconds} maximum={session.budget.max_wall_time_seconds} />
      </section>
      <section className="agent-proposals">
        <h3>动作建议</h3>
        {session.proposals.length ? session.proposals.map((proposal) => {
          const approved = decided.has(proposal.proposal_id);
          return (
            <article key={proposal.proposal_id}>
              <header><div><StatusBadge label={proposal.risk} tone={proposal.risk === "high" ? "danger" : "warning"} /><strong>{proposal.action_type}</strong></div><small>{proposal.policy_status}</small></header>
              <p>来源：{proposal.source} · action {proposal.action_id}</p>
              <ProposalDiff payload={proposal.payload} />
              <pre><code>{JSON.stringify(proposal.payload, null, 2)}</code></pre>
              <div className="agent-action-row">
                {session.state === "awaiting_approval" && !approved ? (
                  <>
                    <button className="button secondary" type="button" disabled={reject.isPending} onClick={() => reject.mutate(proposal)}><XCircle aria-hidden="true" size={15} />拒绝</button>
                    <button className="button secondary" type="button" disabled={approve.isPending} onClick={() => approve.mutate(proposal)}><CheckCircle2 aria-hidden="true" size={15} />批准此动作</button>
                  </>
                ) : null}
                {session.state === "ready" && approved ? (
                  <button className="button primary" type="button" disabled={execute.isPending} onClick={() => execute.mutate(proposal)}><Play aria-hidden="true" size={15} />执行并提交派生 Run</button>
                ) : null}
              </div>
            </article>
          );
        }) : <p className="no-findings">当前轮次没有可执行建议。</p>}
      </section>
      {session.executions.length ? <AuditSection title="执行记录" values={session.executions} /> : null}
      {session.evaluations.length ? <AuditSection title="评价结果" values={session.evaluations} /> : null}
      {session.executions.some((item) => item.derived_run_id) ? (
        <a className="button secondary" href={`/runs/${session.executions.at(-1)?.derived_run_id}?user=${encodeURIComponent(user)}&tab=compare&compare=${encodeURIComponent(session.source_run_id)}`}>对比派生 Run <ArrowRight aria-hidden="true" size={15} /></a>
      ) : null}
    </div>
  );
}

function ProposalDiff({ payload }: { payload: Record<string, unknown> }) {
  const rows = proposalPatchRows(payload);
  if (!rows.length) return null;
  return (
    <dl className="agent-proposal-diff">
      {rows.map((row) => <div key={row.field}><dt>{row.field}</dt><dd className="mono wrap-anywhere">{row.value}</dd></div>)}
    </dl>
  );
}

export type LlmProvider = "local" | "none";

export function defaultProvider(opts: { llmConfigured: boolean }): LlmProvider {
  return opts.llmConfigured ? "local" : "none";
}

export function providerLabel(provider: LlmProvider): string {
  return provider === "local" ? "USTC LLM (glm-5.2-107)" : "确定性规则（无 LLM）";
}

export function proposalPatchRows(payload: Record<string, unknown>) {
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
  return Object.entries(patch).sort(([left], [right]) => left.localeCompare(right)).map(
    ([field, value]) => ({
      field,
      value: value === null ? "需要输入" : typeof value === "string"
        ? value
        : JSON.stringify(value),
    }),
  );
}

function Budget({ label, used, maximum }: { label: string; used: number; maximum: number }) {
  const ratio = maximum === 0 ? 100 : Math.min(100, Math.round((used / maximum) * 100));
  return <div><span><strong>{label}</strong><small>{used} / {maximum}</small></span><div><i style={{ width: `${ratio}%` }} /></div></div>;
}

function AuditSection({ title, values }: { title: string; values: unknown[] }) {
  return <section className="agent-audit"><h3>{title}</h3><pre><code>{JSON.stringify(values, null, 2)}</code></pre></section>;
}

export function remediationStateLabel(state: RemediationState): string {
  const labels: Record<RemediationState, string> = {
    waiting_evidence: "等待证据",
    diagnosing: "诊断中",
    planning: "规划中",
    awaiting_input: "等待输入",
    awaiting_approval: "等待审批",
    ready: "已批准",
    preparing: "准备执行",
    executing: "执行中",
    evaluating: "评价中",
    succeeded: "已验证成功",
    exhausted: "预算耗尽",
    blocked: "需要接管",
    failed: "会话失败",
    cancelled: "已取消",
  };
  return labels[state];
}

export function remediationStateTone(state: RemediationState): "neutral" | "info" | "success" | "warning" | "danger" {
  if (state === "succeeded") return "success";
  if (["failed", "exhausted"].includes(state)) return "danger";
  if (["blocked", "awaiting_input", "awaiting_approval"].includes(state)) return "warning";
  if (activeStates.has(state) || state === "ready") return "info";
  return "neutral";
}
