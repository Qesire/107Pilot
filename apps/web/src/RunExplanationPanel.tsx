import { useMutation } from "@tanstack/react-query";
import { Bot } from "lucide-react";
import { useState } from "react";
import { api } from "./api";
import { defaultProvider, llmConfiguredFromHealth, providerLabel, type LlmProvider } from "./AgentPage";
import { StatusBadge } from "./components";
import { useHealth } from "./query";
import type { AgentExplanation } from "./types";

/**
 * "Why did this Run fail?" panel. Calls POST /runs/{id}/agent/explain and
 * renders the evidence-bound explanation. provider="none" uses the
 * deterministic rule path and always works; "local" needs the campus LLM
 * gateway and is disabled when health reports it unconfigured.
 */
export function RunExplanationPanel({ user, runId, onOpenObject }: {
  user: string;
  runId: string;
  onOpenObject: (objectId: string) => void;
}) {
  const health = useHealth(user);
  const llmConfigured = llmConfiguredFromHealth(health.data);
  const [selected, setSelected] = useState<LlmProvider | null>(null);
  const provider = selected ?? defaultProvider({ llmConfigured });
  const explain = useMutation({
    mutationFn: (chosen: LlmProvider) => api.explainRun(user, runId, chosen),
  });
  const explanation = explain.data;

  return (
    <section className="evidence-section run-explanation" aria-label="Agent 解释">
      <header className="section-action-heading">
        <div>
          <h3>Agent 解释：为什么失败</h3>
          <p>基于已登记 Evidence 生成解释；事实必须引用证据对象，不会自动执行任何修复。</p>
        </div>
        <div className="agent-action-row">
          <label className="select-field">
            <span className="sr-only">解释 provider</span>
            <select value={provider} onChange={(event) => setSelected(event.target.value as LlmProvider)}>
              <option value="none">确定性规则（无 LLM）</option>
              <option value="local" disabled={!llmConfigured}>USTC LLM（本地网关）</option>
            </select>
          </label>
          <button
            className="button secondary"
            type="button"
            disabled={explain.isPending || (provider === "local" && !llmConfigured)}
            onClick={() => explain.mutate(provider)}
          >
            <Bot aria-hidden="true" size={15} />
            {explain.isPending ? "生成解释中" : "获取 Agent 解释"}
          </button>
        </div>
      </header>
      {explain.isError ? <p className="limitation" role="alert">{explain.error.message}</p> : null}
      {explanation ? <ExplanationResult explanation={explanation} onOpenObject={onOpenObject} /> : <p className="side-detail">尚未生成本次解释。{providerLabel(provider)}。</p>}
    </section>
  );
}

function ExplanationResult({ explanation, onOpenObject }: {
  explanation: AgentExplanation;
  onOpenObject: (objectId: string) => void;
}) {
  const degraded = explanation.status !== "explained";
  return (
    <div className="explanation-result">
      <div className="diagnosis-state">
        <StatusBadge
          label={degraded ? `degraded · ${explanation.status}` : "解释已生成"}
          tone={degraded ? "warning" : "success"}
        />
        <span>{providerLabel(explanation.provider === "local" ? "local" : "none")}</span>
        {explanation.model ? <span className="mono">{explanation.model}</span> : null}
        {explanation.evidence_bundle_sha256 ? <span className="mono">evidence sha256 {explanation.evidence_bundle_sha256.slice(0, 16)}</span> : null}
      </div>
      <p><strong>{explanation.summary}</strong></p>
      {explanation.narrative ? <p className="template-description">{explanation.narrative}</p> : null}
      {explanation.warnings.length ? (
        <ul className="capsule-messages warning">{explanation.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
      ) : null}
      {explanation.facts.length ? (
        <div className="diagnosis-list">
          {explanation.facts.map((fact) => (
            <article key={fact.fact_id}>
              <header>
                <StatusBadge label={fact.confidence} tone={fact.confidence === "high" ? "success" : "neutral"} />
                <span className="mono">{fact.fact_id}</span>
              </header>
              <p>{fact.statement}</p>
              {fact.evidence_object_ids.length ? (
                <div className="diagnosis-evidence">
                  {fact.evidence_object_ids.map((objectId) => (
                    <button key={objectId} type="button" onClick={() => onOpenObject(objectId)}>object {objectId}</button>
                  ))}
                </div>
              ) : null}
            </article>
          ))}
        </div>
      ) : null}
      {explanation.recommendations.length ? (
        <section>
          <h4>建议</h4>
          <ol>{explanation.recommendations.map((item) => <li key={item}>{item}</li>)}</ol>
        </section>
      ) : null}
    </div>
  );
}
