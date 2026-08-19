import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ArrowRight, Ban, Bot, CheckCircle2, FolderGit2, Play, Plus, RefreshCw, ShieldAlert, Wrench, XCircle } from "lucide-react";
import { api, ApiRequestError } from "./api";
import { AgentSessionPanel } from "./AgentSessionPanel";
import { AgentProjectPanel } from "./AgentProjectPanel";
import { QueryBoundary, SectionHeading, StatusBadge, formatTimestamp } from "./components";
import { RepairTicketPanel } from "./RepairTicketPanel";
import { RunPicker, type RunPickerRun } from "./RunPicker";
import { useHealth, useRemediationSession, useRemediationSessions, useRuns } from "./query";
import type { HealthReady, RemediationProposal, RemediationSession, RemediationState } from "./types";
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

const remediationCandidateStates = ["FAILED", "SUBMIT_FAILED", "COLLECTION_FAILED", "ORPHANED"] as const;

export function AgentPage({ user, location, navigate }: AgentPageProps) {
  const mode = agentPageMode(location.search);
  const switchMode = (nextMode: AgentPageMode) => navigate(withSearch("/agent", location.search, {
    mode: nextMode,
    session: null,
    project: null,
    state: null,
  }));

  return (
    <>
      <SectionHeading
        eyebrow="Agent"
        title={mode === "conversation" ? "持久化只读对话" : mode === "builder" ? "隔离实验工程" : "可审计的修复会话"}
        detail={mode === "conversation"
          ? "围绕平台、Workspace、Run、日志和 Evidence 提问；事件按持久化游标重放。"
          : mode === "builder"
            ? "从目标和只读快照形成 Blueprint，在应用侧 Workspace 编辑、审阅 diff 并运行无网络 Sandbox。"
            : "规则先提出动作；风险、预算、审批、派生 Run 和评价结果保持在同一条 lineage 中。"}
      />
      <nav className="agent-mode-switch segmented" aria-label="Agent 工作模式">
        <button
          type="button"
          className={mode === "builder" ? "active" : undefined}
          aria-current={mode === "builder" ? "page" : undefined}
          onClick={() => switchMode("builder")}
        >
          <FolderGit2 aria-hidden="true" size={16} />
          工程
        </button>
        <button
          type="button"
          className={mode === "conversation" ? "active" : undefined}
          aria-current={mode === "conversation" ? "page" : undefined}
          onClick={() => switchMode("conversation")}
        >
          <Bot aria-hidden="true" size={16} />
          对话
        </button>
        <button
          type="button"
          className={mode === "repair" ? "active" : undefined}
          aria-current={mode === "repair" ? "page" : undefined}
          onClick={() => switchMode("repair")}
        >
          <Wrench aria-hidden="true" size={16} />
          修复
        </button>
      </nav>
      {mode === "conversation" ? (
        <AgentSessionPanel user={user} location={location} navigate={navigate} />
      ) : mode === "builder" ? (
        <AgentProjectPanel user={user} location={location} navigate={navigate} />
      ) : (
        <RemediationPanel user={user} location={location} navigate={navigate} />
      )}
    </>
  );
}

type AgentPageMode = "conversation" | "builder" | "repair";

export function agentPageMode(search: URLSearchParams): AgentPageMode {
  const mode = search.get("mode");
  return mode === "repair" || mode === "builder" ? mode : "conversation";
}

function RemediationPanel({ user, location, navigate }: AgentPageProps) {
  const queryClient = useQueryClient();
  const state = location.search.get("state") ?? "";
  const requestedSession = location.search.get("session");
  const sessions = useRemediationSessions(user, state || undefined);
  const runs = useRuns(user, undefined, undefined, "100");
  const health = useHealth(user);
  const llmConfigured = llmConfiguredFromHealth(health.data);
  const selectedId = requestedSession ?? sessions.data?.items[0]?.session_id ?? null;
  const detail = useRemediationSession(user, selectedId);
  const selectSession = (sessionId: string) =>
    navigate(withSearch("/agent", location.search, { session: sessionId }));
  // Provider chosen on the creation form. The Worker auto-advances through
  // `planning` within ~1s of creation, so the user's LLM choice must ride on
  // the create request itself — the detail-page selector only affects later
  // replanning cycles. We track an override rather than a fixed initial value
  // so the default can react to `useHealth` resolving (LLM-configured sites
  // offer "local"; unconfigured sites fall back to "none") without freezing
  // the first-render value before health has loaded.
  const [createProviderOverride, setCreateProviderOverride] = useState<LlmProvider | null>(null);
  const createProvider = createProviderOverride ?? defaultProvider({ llmConfigured });
  // Toggles the inline "new session" controls above the session list. Only
  // relevant when at least one session already exists — the empty state has
  // its own create controls and never sets this.
  const [showCreate, setShowCreate] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  // A key survives a retry, but is never permanently tied to a Run. Otherwise
  // a cancelled/blocked historical session prevents any new session forever.
  const [createRequestKey, setCreateRequestKey] = useState<string | null>(null);
  const candidateRuns = filterRemediationRuns(runs.data?.items ?? []);
  const selectedRun = candidateRuns.find((run) => run.run_id === selectedRunId) ?? null;
  const createRemediationSession = useMutation({
    mutationFn: ({ runId, requestKey }: { runId: string; requestKey: string }) =>
      api.createRemediationSession(user, runId, requestKey, createProvider),
    onSuccess: (session) => {
      setShowCreate(false);
      setSelectedRunId(null);
      setCreateRequestKey(null);
      void queryClient.invalidateQueries({ queryKey: ["remediation-sessions", user] });
      selectSession(session.session_id);
    },
  });
  const selectRun = (runId: string) => {
    if (runId !== selectedRunId) setCreateRequestKey(null);
    setSelectedRunId(runId);
    createRemediationSession.reset();
  };
  const createSelectedSession = () => {
    if (!selectedRunId) return;
    const requestKey = createRequestKey ?? newRemediationRequestKey(selectedRunId);
    setCreateRequestKey(requestKey);
    createRemediationSession.mutate({ runId: selectedRunId, requestKey });
  };

  return (
    <>
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
            <div className="agent-queue-actions">
              {(sessions.data?.items.length ?? 0) > 0 ? (
                <button
                  type="button"
                  className="button secondary"
                  aria-expanded={showCreate}
                  aria-controls="agent-new-session"
                  onClick={() => setShowCreate((value) => !value)}
                >
                  <Plus aria-hidden="true" size={15} />
                  {showCreate ? "取消新建" : "新建修复会话"}
                </button>
              ) : null}
              {sessions.isFetching ? <StatusBadge label="同步中" tone="info" /> : null}
            </div>
          </div>
          <QueryBoundary
            pending={sessions.isPending}
            error={sessions.error}
            empty={(sessions.data?.items.length ?? 0) === 0}
            emptyTitle="选择一个失败的 Run 开始修复"
            emptyDetail={
              <>
                <div className="agent-create-controls">
                  <label className="select-field">
                    <Bot aria-hidden="true" size={16} />
                    <span className="sr-only">LLM provider</span>
                    <select
                      value={createProvider}
                      onChange={(event) => setCreateProviderOverride(event.target.value as LlmProvider)}
                      aria-label="LLM provider"
                      className="llm-provider-select"
                    >
                      <option value="local">{providerLabel("local")}</option>
                      <option value="none">{providerLabel("none")}</option>
                    </select>
                  </label>
                  <p className="agent-safety-note">
                    选择 Run 后会以当前 LLM 设置创建修复会话；Worker 将用该 provider 解释诊断。
                  </p>
                </div>
                <RemediationRunSelection
                  runs={candidateRuns}
                  selectedRun={selectedRun}
                  pending={createRemediationSession.isPending}
                  error={createRemediationSession.error}
                  onSelect={selectRun}
                  onConfirm={createSelectedSession}
                />
                <p className="agent-safety-note">
                  选择 Run 后，Agent 只处理该 Run 的 Evidence，不会扫描或修改其他作业。
                </p>
              </>
            }
          >
            {showCreate && (sessions.data?.items.length ?? 0) > 0 ? (
              <section id="agent-new-session" className="agent-new-session" aria-label="新建修复会话">
                <div className="agent-create-controls">
                  <label className="select-field">
                    <Bot aria-hidden="true" size={16} />
                    <span className="sr-only">LLM provider</span>
                    <select
                      value={createProvider}
                      onChange={(event) => setCreateProviderOverride(event.target.value as LlmProvider)}
                      aria-label="LLM provider"
                      className="llm-provider-select"
                    >
                      <option value="local">{providerLabel("local")}</option>
                      <option value="none">{providerLabel("none")}</option>
                    </select>
                  </label>
                  <p className="agent-safety-note">
                    选择 Run 后会以当前 LLM 设置创建修复会话；Worker 将用该 provider 解释诊断。
                  </p>
                </div>
                <RemediationRunSelection
                  runs={candidateRuns}
                  selectedRun={selectedRun}
                  pending={createRemediationSession.isPending}
                  error={createRemediationSession.error}
                  onSelect={selectRun}
                  onConfirm={createSelectedSession}
                />
              </section>
            ) : null}
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
            {detail.data ? <SessionDetail key={detail.data.session_id} user={user} session={detail.data} /> : null}
          </QueryBoundary>
        </section>
      </div>
    </>
  );
}

function RemediationRunSelection({
  runs,
  selectedRun,
  pending,
  error,
  onSelect,
  onConfirm,
}: {
  runs: RunPickerRun[];
  selectedRun: RunPickerRun | null;
  pending: boolean;
  error: Error | null;
  onSelect: (runId: string) => void;
  onConfirm: () => void;
}) {
  return (
    <div className="agent-run-selection">
      <RunPicker
        runs={runs}
        filter={{ states: remediationCandidateStates }}
        selectedRunId={selectedRun?.run_id ?? null}
        onSelect={onSelect}
      />
      <div className="agent-run-selection-footer" aria-live="polite">
        {selectedRun ? (
          <div className="agent-selected-run">
            <span>已选择</span>
            <strong title={selectedRun.job_name ?? selectedRun.run_id}>{selectedRun.job_name ?? "历史作业"}</strong>
            <small className="mono">sacct Job {selectedRun.job_id ?? "未提交"} · {selectedRun.run_id}</small>
          </div>
        ) : <p>先选择一个失败、提交失败或采集失败的 Run。</p>}
        <button
          className="button primary"
          type="button"
          disabled={!selectedRun || pending}
          onClick={onConfirm}
        >
          <Play aria-hidden="true" size={15} />
          {pending ? "正在创建会话" : "创建修复会话"}
        </button>
      </div>
      {error ? <MutationError error={error} /> : null}
    </div>
  );
}

function filterRemediationRuns<T extends { state: string }>(runs: T[]) {
  return runs.filter((run) => remediationCandidateStates.includes(run.state as (typeof remediationCandidateStates)[number]));
}

function MutationError({ error }: { error: Error }) {
  const apiError = error instanceof ApiRequestError ? error : null;
  return (
    <div className="agent-mutation-error" role="alert">
      <strong>无法完成此操作</strong>
      <p>{error.message}</p>
      {apiError ? <small className="mono">{apiError.code}</small> : null}
    </div>
  );
}

function SessionDetail({ user, session }: { user: string; session: RemediationSession }) {
  const queryClient = useQueryClient();
  const health = useHealth(user);
  const llmConfigured = llmConfiguredFromHealth(health.data);
  // The provider dropdown mirrors the session's persisted provider. We track
  // an override (not a fixed initial value) so that:
  //   - On first render, before the user touches anything, the dropdown shows
  //     `session.provider` — the value the Worker has been using. Advancing
  //     re-sends that same value instead of silently overwriting it with
  //     "local" (the old hard-coded default).
  //   - If `session.provider` is missing/unknown, we fall back to
  //     `defaultProvider({ llmConfigured })` so unconfigured-LLM sites get
  //     "none" and configured sites get "local".
  //   - If the session is refreshed and `session.provider` changes, the
  //     dropdown follows it — unless the user has already picked, in which
  //     case the override sticks.
  const [providerOverride, setProviderOverride] = useState<LlmProvider | null>(null);
  const provider: LlmProvider = providerOverride ?? sessionProviderValue(session.provider, defaultProvider({ llmConfigured }));
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
  // We always send the dropdown's current value. Because the dropdown is
  // initialized from `session.provider`, this is a no-op unless the user
  // intentionally picks a different provider — it does not silently overwrite
  // the persisted value the way the old hard-coded "local" did.
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
  // create_repair_ticket proposals hand off to the M2 repair-ticket workflow
  // instead of deriving a Run: executing them via the remediation execute API
  // would fail (no contract candidate). We create the ticket directly, which
  // binds the session's diagnoses + code context; the embedded
  // RepairTicketPanel then drives fix → derived Run → resolve.
  const createTicket = useMutation({
    mutationFn: (proposal: RemediationProposal) => api.createRepairTicket(user, {
      session_id: session.session_id,
      request_key: `ui:${session.session_id}:${proposal.proposal_id}`,
    }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["repair-tickets", user] });
      refresh();
    },
  });
  const error = advance.error ?? approve.error ?? reject.error ?? cancel.error
    ?? takeover.error ?? execute.error ?? createTicket.error;
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
                    onChange={(event) => setProviderOverride(event.target.value as LlmProvider)}
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
                  proposal.action_type === "create_repair_ticket" ? (
                    <button className="button primary" type="button" disabled={createTicket.isPending} onClick={() => createTicket.mutate(proposal)}><Wrench aria-hidden="true" size={15} />{createTicket.isPending ? "正在创建修复票据" : "创建修复票据"}</button>
                  ) : (
                    <button className="button primary" type="button" disabled={execute.isPending} onClick={() => execute.mutate(proposal)}><Play aria-hidden="true" size={15} />执行并提交派生 Run</button>
                  )
                ) : null}
              </div>
            </article>
          );
        }) : <p className="no-findings">当前轮次没有可执行建议。</p>}
      </section>
      {session.executions.length ? <AuditSection title="执行记录" values={session.executions} /> : null}
      {session.evaluations.length ? <AuditSection title="评价结果" values={session.evaluations} /> : null}
      <RepairTicketPanel user={user} sessionId={session.session_id} />
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

// Derives whether the local LLM gateway is configured from the readiness
// payload. The backend emits a `local_llm` check whose status is "configured"
// when the gateway is enabled and "disabled" otherwise (health.py:102,
// `_configured_check`). Treat anything other than "configured" as
// unconfigured so a missing/unknown check degrades to the safe "none" default.
export function llmConfiguredFromHealth(health: HealthReady | undefined): boolean {
  const checks = health?.checks;
  if (!checks) return false;
  if (Array.isArray(checks)) {
    return checks.some((check) => check.name === "local_llm" && check.status === "configured");
  }
  return checks.local_llm?.status === "configured";
}

export function newRemediationRequestKey(runId: string): string {
  return `ui:${runId}:${crypto.randomUUID()}`;
}

// Coerces the session's persisted provider string into the dropdown's union
// type. Unknown/empty values fall back to `fallback` so the UI never shows a
// blank option or sends a value the backend would reject.
export function sessionProviderValue(value: string | undefined, fallback: LlmProvider): LlmProvider {
  if (value === "local" || value === "none") return value;
  return fallback;
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
