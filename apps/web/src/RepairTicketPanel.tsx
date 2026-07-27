import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ArrowRight, CheckCircle2, FileCode2, Wrench, XCircle } from "lucide-react";
import { api } from "./api";
import { StatusBadge, formatTimestamp } from "./components";
import { useRepairTickets } from "./query";
import type { RepairTicket, RepairTicketComparison, RepairTicketState } from "./types";

interface RepairTicketPanelProps {
  user: string;
  sessionId: string;
}

const stateLabels: Record<RepairTicketState, string> = {
  open: "待修复",
  resolved: "已修复",
  abandoned: "已放弃",
};

const stateTones: Record<RepairTicketState, "info" | "success" | "neutral"> = {
  open: "info",
  resolved: "success",
  abandoned: "neutral",
};

export function RepairTicketPanel({ user, sessionId }: RepairTicketPanelProps) {
  const tickets = useRepairTickets(user, undefined, sessionId);
  const items = tickets.data?.items ?? [];

  if (tickets.isPending) return <p className="no-findings">加载修复票据…</p>;
  if (!items.length) return null;

  return (
    <section className="agent-repair-tickets" aria-label="修复票据">
      <h3><Wrench aria-hidden="true" size={15} /> 修复票据</h3>
      {items.map((ticket) => (
        <RepairTicketCard key={ticket.ticket_id} user={user} ticket={ticket} />
      ))}
    </section>
  );
}

function RepairTicketCard({ user, ticket }: { user: string; ticket: RepairTicket }) {
  const queryClient = useQueryClient();
  const [showResolve, setShowResolve] = useState(false);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["repair-tickets", user] });
    void queryClient.invalidateQueries({ queryKey: ["repair-ticket", user, ticket.ticket_id] });
  };

  const abandon = useMutation({
    mutationFn: () => api.abandonRepairTicket(user, ticket.ticket_id, "用户手动放弃"),
    onSuccess: invalidate,
  });

  return (
    <article className="repair-ticket-card">
      <header>
        <div className="repair-ticket-header">
          <StatusBadge label={stateLabels[ticket.state]} tone={stateTones[ticket.state]} />
          <small className="mono">{ticket.ticket_id.slice(0, 12)}…</small>
        </div>
        <small>{formatTimestamp(ticket.updated_at)}</small>
      </header>

      {ticket.requested_change ? (
        <p className="repair-ticket-change"><FileCode2 aria-hidden="true" size={14} /> {ticket.requested_change}</p>
      ) : null}

      {ticket.code_context ? (
        <details className="repair-ticket-code">
          <summary>代码上下文</summary>
          <pre><code>{formatCodeContext(ticket.code_context)}</code></pre>
        </details>
      ) : null}

      {ticket.no_go_constraints.length ? (
        <div className="repair-ticket-constraints">
          <strong>约束</strong>
          <ul>{ticket.no_go_constraints.map((c) => <li key={c}>{c}</li>)}</ul>
        </div>
      ) : null}

      {ticket.state === "resolved" && ticket.resolution_comparison ? (
        <ComparisonView comparison={ticket.resolution_comparison} user={user} />
      ) : null}

      {ticket.state === "abandoned" && ticket.abandon_reason ? (
        <p className="repair-ticket-reason">放弃原因：{ticket.abandon_reason}</p>
      ) : null}

      {ticket.state === "open" ? (
        <div className="agent-action-row">
          <button
            className="button secondary"
            type="button"
            onClick={() => setShowResolve((v) => !v)}
          >
            <CheckCircle2 aria-hidden="true" size={15} />
            {showResolve ? "取消" : "标记已修复"}
          </button>
          <button
            className="button danger"
            type="button"
            disabled={abandon.isPending}
            onClick={() => abandon.mutate()}
          >
            <XCircle aria-hidden="true" size={15} />
            放弃
          </button>
        </div>
      ) : null}

      {showResolve && ticket.state === "open" ? (
        <ResolveForm user={user} ticket={ticket} onDone={() => { setShowResolve(false); invalidate(); }} />
      ) : null}

      {abandon.error ? <p className="repair-ticket-error" role="alert">{abandon.error.message}</p> : null}
    </article>
  );
}

function ResolveForm({ user, ticket, onDone }: { user: string; ticket: RepairTicket; onDone: () => void }) {
  const [revision, setRevision] = useState("");
  const [testSummary, setTestSummary] = useState("");
  const [derivedRunId, setDerivedRunId] = useState("");

  const resolve = useMutation({
    mutationFn: async () => {
      const manifest = await api.createArtifactManifest(user, {
        revision,
        ...(testSummary ? { local_test_summary: testSummary } : {}),
        disclosure: "metadata_only",
      });
      return api.resolveRepairTicket(user, ticket.ticket_id, {
        manifest_id: manifest.manifest_id,
        derived_run_id: derivedRunId,
      });
    },
    onSuccess: onDone,
  });

  const valid = revision.trim().length > 0 && derivedRunId.trim().length > 0;

  return (
    <form
      className="repair-resolve-form"
      onSubmit={(event) => { event.preventDefault(); if (valid) resolve.mutate(); }}
    >
      <label>
        <span>Revision (git SHA)</span>
        <input
          className="text-field"
          value={revision}
          onChange={(e) => setRevision(e.target.value)}
          placeholder="abc1234 或 fix-branch"
          required
        />
      </label>
      <label>
        <span>本地测试摘要</span>
        <input
          className="text-field"
          value={testSummary}
          onChange={(e) => setTestSummary(e.target.value)}
          placeholder="pytest 全部通过"
        />
      </label>
      <label>
        <span>派生 Run ID（修复后重新提交的 Run）</span>
        <input
          className="text-field mono"
          value={derivedRunId}
          onChange={(e) => setDerivedRunId(e.target.value)}
          placeholder="run-…"
          required
        />
      </label>
      <button className="button primary" type="submit" disabled={!valid || resolve.isPending}>
        <CheckCircle2 aria-hidden="true" size={15} />
        {resolve.isPending ? "提交中…" : "确认修复"}
      </button>
      {resolve.error ? <p className="repair-ticket-error" role="alert">{resolve.error.message}</p> : null}
    </form>
  );
}

function ComparisonView({ comparison, user }: { comparison: RepairTicketComparison; user: string }) {
  return (
    <div className="repair-comparison">
      <h4>Run 对比</h4>
      <table className="repair-comparison-table">
        <thead>
          <tr><th /><th>Source Run</th><th /><th>Derived Run</th></tr>
        </thead>
        <tbody>
          <tr>
            <td>状态</td>
            <td><StatusBadge label={comparison.source_state ?? "—"} tone="danger" /></td>
            <td><ArrowRight aria-hidden="true" size={14} /></td>
            <td><StatusBadge label={comparison.derived_state ?? "—"} tone="success" /></td>
          </tr>
          <tr>
            <td>Exit code</td>
            <td className="mono">{comparison.source_exit_code ?? "—"}</td>
            <td />
            <td className="mono">{comparison.derived_exit_code ?? "—"}</td>
          </tr>
          <tr>
            <td>诊断数</td>
            <td>{comparison.source_diagnosis_count ?? "—"}</td>
            <td />
            <td>{comparison.derived_diagnosis_count ?? "—"}</td>
          </tr>
        </tbody>
      </table>
      {comparison.improved ? (
        <p className="repair-improved"><CheckCircle2 aria-hidden="true" size={14} /> 修复有效：派生 Run 状态改善</p>
      ) : null}
      <a
        className="button secondary"
        href={`/runs/${comparison.derived_run_id}?user=${encodeURIComponent(user)}&tab=compare&compare=${encodeURIComponent(comparison.source_run_id)}`}
      >
        查看完整对比 <ArrowRight aria-hidden="true" size={14} />
      </a>
    </div>
  );
}

function formatCodeContext(ctx: Record<string, unknown>): string {
  if (typeof ctx.snippet === "string") return ctx.snippet;
  if (typeof ctx.traceback === "string") return ctx.traceback;
  return JSON.stringify(ctx, null, 2);
}
