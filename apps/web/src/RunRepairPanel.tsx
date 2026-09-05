import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  Ban,
  Bot,
  CheckCircle2,
  Play,
  RefreshCw,
  ShieldAlert,
  Wrench,
  XCircle,
} from "lucide-react";
import { useState } from "react";
import { api } from "./api";
import {
  defaultProvider,
  llmConfiguredFromHealth,
  providerLabel,
  sessionProviderValue,
  type LlmProvider,
} from "./AgentPage";
import { QueryBoundary, StatusBadge } from "./components";
import { useHealth, useRemediationSession, useRemediationSessions } from "./query";
import {
  activeRepairStates,
  approvedProposalIds,
  canOpenRepair,
  derivedRunHref,
  repairProjectHref,
  sessionsForRun,
  terminalRepairStates,
} from "./run-repair";
import type { RemediationProposal, RemediationSession } from "./types";
import type { EvidenceTab, RunWorkspace } from "./run-workspace";

interface RunRepairPanelProps {
  workspace: RunWorkspace;
  onNavigate: (tab: EvidenceTab) => void;
}

const stateLabels: Record<string, string> = {
  waiting_evidence: "等待证据",
  diagnosing: "诊断中",
  planning: "形成方案",
  awaiting_input: "需要输入",
  awaiting_approval: "等待批准",
  ready: "已批准，待执行",
  preparing: "准备执行",
  executing: "执行中",
  evaluating: "验证中",
  succeeded: "修复已验证",
  exhausted: "预算耗尽",
  blocked: "需要人工接管",
  failed: "修复失败",
  cancelled: "已取消",
};

function stateTone(state: string): "neutral" | "info" | "success" | "warning" | "danger" {
  if (state === "succeeded") return "success";
  if (state === "failed" || state === "exhausted") return "danger";
  if (state === "blocked" || state === "awaiting_input" || state === "awaiting_approval") return "warning";
  if (activeRepairStates.has(state) || state === "ready") return "info";
  return "neutral";
}

export function RunRepairPanel({ workspace, onNavigate }: RunRepairPanelProps) {
  const user = workspace.run.owner;
  const runId = workspace.run.run_id;
  const queryClient = useQueryClient();
  const health = useHealth(user);
  const sessions = useRemediationSessions(user);
  const matching = sessionsForRun(sessions.data?.items ?? [], runId);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const selectedId = selectedSessionId && matching.some((item) => item.session_id === selectedSessionId)
    ? selectedSessionId
    : matching[0]?.session_id ?? null;
  const detail = useRemediationSession(user, selectedId);
  const [requestKey, setRequestKey] = useState<string | null>(null);
  const [providerOverride, setProviderOverride] = useState<LlmProvider | null>(null);
  const [createdProjectHref, setCreatedProjectHref] = useState<string | null>(null);
  const fallbackProvider = defaultProvider({ llmConfigured: llmConfiguredFromHealth(health.data) });
  const provider = providerOverride ?? sessionProviderValue(detail.data?.provider, fallbackProvider);

  const refresh = (updated?: RemediationSession) => {
    if (updated) {
      queryClient.setQueryData(["remediation-session", user, updated.session_id], updated);
    }
    void queryClient.invalidateQueries({ queryKey: ["remediation-sessions", user] });
    if (selectedId) {
      void queryClient.invalidateQueries({ queryKey: ["remediation-session", user, selectedId] });
    }
    void queryClient.invalidateQueries({ queryKey: ["run-workspace", user, runId] });
  };

  const create = useMutation({
    mutationFn: (key: string) => api.createRemediationSession(user, runId, key, provider),
    onSuccess: (session) => {
      setRequestKey(null);
      setSelectedSessionId(session.session_id);
      queryClient.setQueryData(["remediation-session", user, session.session_id], session);
      refresh(session);
    },
  });
  const advance = useMutation({
    mutationFn: (session: RemediationSession) => api.advanceRemediationSession(
      user,
      session.session_id,
      undefined,
      { provider },
    ),
    onSuccess: refresh,
  });
  const approve = useMutation({
    mutationFn: ({ session, proposal }: { session: RemediationSession; proposal: RemediationProposal }) =>
      api.approveRemediationAction(user, session.session_id, proposal.proposal_id, session.version),
    onSuccess: refresh,
  });
  const reject = useMutation({
    mutationFn: ({ session, proposal }: { session: RemediationSession; proposal: RemediationProposal }) =>
      api.rejectRemediationAction(user, session.session_id, proposal.proposal_id, session.version),
    onSuccess: refresh,
  });
  const execute = useMutation({
    mutationFn: ({ session, proposal }: { session: RemediationSession; proposal: RemediationProposal }) =>
      api.executeRemediationAction(user, session.session_id, proposal.proposal_id, session.version),
    onSuccess: refresh,
  });
  const cancel = useMutation({
    mutationFn: (session: RemediationSession) =>
      api.cancelRemediationSession(user, session.session_id, session.version),
    onSuccess: refresh,
  });
  const startRepairProject = useMutation({
    mutationFn: ({ session, proposal }: { session: RemediationSession; proposal: RemediationProposal }) =>
      api.startRemediationRepairProject(user, session.session_id, {
        proposal_id: proposal.proposal_id,
        expected_version: session.version,
        request_key: `run-repair:${session.session_id}:${proposal.proposal_id}`,
      }),
    onSuccess: (repair) => {
      setCreatedProjectHref(repairProjectHref(
        user,
        repair.project.project_id,
        repair.remediation_session_id,
        repair.source_run_id,
      ));
      void queryClient.invalidateQueries({ queryKey: ["agent-projects", user] });
      refresh();
    },
  });

  const mutationError = create.error ?? advance.error ?? approve.error ?? reject.error
    ?? execute.error ?? cancel.error ?? startRepairProject.error;
  const session = detail.data;
  const approved = session ? approvedProposalIds(session) : new Set<string>();
  const derivedRunId = session?.executions
    .map((item) => item.derived_run_id)
    .filter((value): value is string => Boolean(value))
    .at(-1) ?? null;

  if (!canOpenRepair(workspace)) {
    return (
      <section className="panel" aria-label="失败恢复">
        <div className="studio-notice warning">
          <ShieldAlert aria-hidden="true" />
          <div>
            <strong>尚不能进入修复</strong>
            <p>当前没有可作为解释依据的持久化诊断。先检查错误输出或生成诊断；系统不会猜测根因。</p>
            <div className="agent-action-row">
              <button className="button secondary" type="button" onClick={() => onNavigate("logs")}>查看错误输出</button>
              <button className="button primary" type="button" onClick={() => onNavigate("diagnosis")}>查看诊断</button>
            </div>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="panel agent-session-detail" aria-labelledby="run-repair-heading">
      <div className="panel-heading">
        <div>
          <p className="panel-kicker">Failure recovery / Run-local</p>
          <h3 id="run-repair-heading">准备修复这次运行</h3>
        </div>
        {session ? <StatusBadge label={stateLabels[session.state] ?? session.state} tone={stateTone(session.state)} /> : null}
      </div>

      <div className="studio-notice warning">
        <ShieldAlert aria-hidden="true" />
        <div>
          <strong>证据与解释保持分离</strong>
          <p>观察到：Run {workspace.states.execution}，退出码 {workspace.run.exit_code ?? "未登记"}，已有 {workspace.evidence_summary.object_count} 个证据对象。解释来自 {workspace.evidence_summary.diagnosis_count} 条持久化诊断，不由本面板重新推断。</p>
          <button className="text-link" type="button" onClick={() => onNavigate("diagnosis")}>核对诊断与 Evidence</button>
        </div>
      </div>

      <p className="side-detail">修复会话绑定当前 Run；审批、执行、验证与派生 Run 保持在同一条审计链中。Agent 只在需要实际修改代码时作为隔离工程能力进入，不替代 Run 工作区。</p>

      {matching.length > 1 ? (
        <div className="agent-action-row" aria-label="当前 Run 的修复会话">
          {matching.map((item) => (
            <button
              className={item.session_id === selectedId ? "button primary" : "button secondary"}
              key={item.session_id}
              type="button"
              onClick={() => setSelectedSessionId(item.session_id)}
            >
              {stateLabels[item.state] ?? item.state}
            </button>
          ))}
        </div>
      ) : null}

      <QueryBoundary pending={sessions.isPending || Boolean(selectedId && detail.isPending)} error={sessions.error ?? detail.error}>
        {!session ? (
          <div className="agent-create-controls">
            <label className="select-field">
              <Bot aria-hidden="true" size={16} />
              <span className="sr-only">修复解释方式</span>
              <select
                value={provider}
                onChange={(event) => setProviderOverride(event.target.value as LlmProvider)}
                aria-label="修复解释方式"
              >
                <option value="local">{providerLabel("local")}</option>
                <option value="none">{providerLabel("none")}</option>
              </select>
            </label>
            <button
              className="button primary"
              type="button"
              disabled={create.isPending}
              onClick={() => {
                const key = requestKey ?? `run-repair:${runId}:${crypto.randomUUID()}`;
                setRequestKey(key);
                create.mutate(key);
              }}
            >
              <Wrench aria-hidden="true" size={15} />
              {create.isPending ? "正在创建修复会话" : "创建修复会话"}
            </button>
          </div>
        ) : (
          <>
            <dl className="fact-list">
              <div><dt>修复会话</dt><dd className="mono wrap-anywhere">{session.session_id}</dd></div>
              <div><dt>Source Run</dt><dd className="mono wrap-anywhere">{session.source_run_id}</dd></div>
              <div><dt>Source Contract</dt><dd className="mono wrap-anywhere">{session.source_contract_id ?? "未绑定"}</dd></div>
              <div><dt>策略</dt><dd>{session.automation_policy}</dd></div>
              <div><dt>尝试预算</dt><dd>{session.usage.attempts}/{session.budget.max_attempts}</dd></div>
              <div><dt>提交预算</dt><dd>{session.usage.submissions}/{session.budget.max_submissions}</dd></div>
              <div><dt>会话版本</dt><dd>{session.version}</dd></div>
            </dl>

            {!terminalRepairStates.has(session.state) ? (
              <div className="agent-action-row">
                {activeRepairStates.has(session.state) ? (
                  <>
                    <label className="select-field">
                      <Bot aria-hidden="true" size={16} />
                      <span className="sr-only">修复解释方式</span>
                      <select
                        value={provider}
                        onChange={(event) => setProviderOverride(event.target.value as LlmProvider)}
                        aria-label="修复解释方式"
                      >
                        <option value="local">{providerLabel("local")}</option>
                        <option value="none">{providerLabel("none")}</option>
                      </select>
                    </label>
                    <button className="button secondary" type="button" disabled={advance.isPending} onClick={() => advance.mutate(session)}>
                      <RefreshCw aria-hidden="true" size={15} className={advance.isPending ? "spin" : undefined} />
                      推进诊断与计划
                    </button>
                  </>
                ) : null}
                <button className="button danger" type="button" disabled={cancel.isPending} onClick={() => cancel.mutate(session)}>
                  <Ban aria-hidden="true" size={15} />取消修复会话
                </button>
              </div>
            ) : null}

            {session.stop_reason ? (
              <div className="studio-notice warning"><ShieldAlert aria-hidden="true" /><div><strong>停止原因</strong><p>{session.stop_reason}</p></div></div>
            ) : null}

            <div className="agent-proposals">
              <h3>建议与操作</h3>
              {session.proposals.length ? session.proposals.map((proposal) => {
                const isApproved = approved.has(proposal.proposal_id);
                return (
                  <article key={proposal.proposal_id}>
                    <header>
                      <div><StatusBadge label={proposal.risk} tone={proposal.risk === "high" ? "danger" : "warning"} /><strong>{proposal.action_type}</strong></div>
                      <small>{proposal.policy_status}</small>
                    </header>
                    <p>建议来源：{proposal.source} · action {proposal.action_id}</p>
                    <details><summary>查看结构化建议</summary><pre><code>{JSON.stringify(proposal.payload, null, 2)}</code></pre></details>
                    <div className="agent-action-row">
                      {session.state === "awaiting_approval" && !isApproved ? (
                        <>
                          <button className="button secondary" type="button" disabled={reject.isPending} onClick={() => reject.mutate({ session, proposal })}><XCircle aria-hidden="true" size={15} />拒绝</button>
                          <button className="button primary" type="button" disabled={approve.isPending} onClick={() => approve.mutate({ session, proposal })}><CheckCircle2 aria-hidden="true" size={15} />批准此动作</button>
                        </>
                      ) : null}
                      {session.state === "ready" && isApproved ? (
                        proposal.action_type === "create_repair_ticket" ? (
                          <button className="button primary" type="button" disabled={startRepairProject.isPending} onClick={() => startRepairProject.mutate({ session, proposal })}><Wrench aria-hidden="true" size={15} />创建隔离修复工程</button>
                        ) : (
                          <button className="button primary" type="button" disabled={execute.isPending} onClick={() => execute.mutate({ session, proposal })}><Play aria-hidden="true" size={15} />执行并提交派生 Run</button>
                        )
                      ) : null}
                    </div>
                  </article>
                );
              }) : <p className="no-findings">当前还没有可批准的修复建议。</p>}
            </div>

            {session.evaluations.length ? (
              <div className="studio-notice"><CheckCircle2 aria-hidden="true" /><div><strong>验证记录已持久化</strong><p>已有 {session.evaluations.length} 条 evaluation；请按其 checks 与 Evidence 判断修复是否成立。</p></div></div>
            ) : null}
          </>
        )}
      </QueryBoundary>

      {mutationError ? <div className="studio-notice error" role="alert"><ShieldAlert aria-hidden="true" /><div><strong>修复动作失败</strong><p>{mutationError.message}</p></div></div> : null}

      {createdProjectHref ? (
        <a className="button secondary" href={createdProjectHref}>进入隔离 Agent Builder <ArrowRight aria-hidden="true" size={15} /></a>
      ) : null}
      {derivedRunId ? (
        <a className="button secondary" href={derivedRunHref(user, runId, derivedRunId)}>对比派生 Run <ArrowRight aria-hidden="true" size={15} /></a>
      ) : null}
    </section>
  );
}
