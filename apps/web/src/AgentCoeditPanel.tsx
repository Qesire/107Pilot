import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import {
  AlertTriangle,
  Bot,
  Check,
  Send,
  Sparkles,
  X,
} from "lucide-react";
import { api } from "./api";
import { providerLabel, type LlmProvider } from "./AgentPage";
import type { ContractSuggestion, JsonObject } from "./types";

export interface AgentCoeditPanelProps {
  user: string;
  contract: JsonObject;
  recipeVersionId: string;
  onApplyPatch: (patch: Record<string, unknown>) => void;
}

/**
 * Right-rail co-edit panel. The agent reads the current canonical Contract and
 * the user's intent, returns a dotted-path patch + Chinese explanation, and
 * waits for the user to apply or reject. Nothing is written to canonical until
 * the user clicks 应用建议.
 */
export function AgentCoeditPanel({
  user,
  contract,
  recipeVersionId,
  onApplyPatch,
}: AgentCoeditPanelProps) {
  const [intent, setIntent] = useState("");
  const [provider, setProvider] = useState<LlmProvider>("local");
  const [suggestion, setSuggestion] = useState<ContractSuggestion | null>(null);

  const suggest = useMutation({
    mutationFn: () =>
      api.suggestContractPatch(user, contract, recipeVersionId, intent, provider),
    onSuccess: setSuggestion,
  });

  const send = () => {
    if (!intent.trim() || suggest.isPending) return;
    setSuggestion(null);
    suggest.mutate();
  };

  const apply = () => {
    if (!suggestion) return;
    onApplyPatch(suggestion.suggested_patch);
    setSuggestion(null);
    setIntent("");
  };

  const reject = () => {
    setSuggestion(null);
  };

  const patchRows = suggestion
    ? Object.entries(suggestion.suggested_patch)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([path, value]) => ({
          path,
          value:
            value === null
              ? "需要输入"
              : typeof value === "string"
                ? value
                : JSON.stringify(value),
        }))
    : [];

  return (
    <div className="agent-coedit">
      <header className="agent-coedit-heading">
        <p className="panel-kicker">Agent co-edit</p>
        <h2>描述需求，Agent 建议改动</h2>
        <p>
          Agent 只读取当前 Contract；任何建议都需要你确认后才会写入 canonical。
        </p>
      </header>

      <div className="agent-coedit-input">
        <textarea
          aria-label="需求描述"
          placeholder="例如：我要跑一个 python 训练脚本，需要 4 个 CPU、16G 内存"
          rows={4}
          value={intent}
          onChange={(event) => setIntent(event.target.value)}
          disabled={suggest.isPending}
        />
        <div className="agent-coedit-controls">
          <label className="select-field">
            <Bot aria-hidden="true" size={16} />
            <span className="sr-only">LLM provider</span>
            <select
              value={provider}
              onChange={(event) => setProvider(event.target.value as LlmProvider)}
              disabled={suggest.isPending}
            >
              <option value="local">{providerLabel("local")}</option>
              <option value="none">{providerLabel("none")}</option>
            </select>
          </label>
          <button
            className="button primary"
            type="button"
            disabled={suggest.isPending || !intent.trim()}
            onClick={send}
          >
            <Send aria-hidden="true" size={15} />
            {suggest.isPending ? "思考中" : "发送给 Agent"}
          </button>
        </div>
      </div>

      {suggest.isError ? (
        <div className="studio-notice error" role="alert">
          <AlertTriangle aria-hidden="true" />
          <div>
            <strong>Agent 调用失败</strong>
            <p>
              {suggest.error instanceof Error
                ? suggest.error.message
                : "未知错误"}
            </p>
          </div>
        </div>
      ) : null}

      {suggest.isPending && !suggestion ? (
        <div className="agent-coedit-loading" role="status">
          <Sparkles aria-hidden="true" />
          <span>Agent 正在分析当前 Contract…</span>
        </div>
      ) : null}

      {suggestion ? (
        <div className="agent-coedit-result">
          <div className="agent-coedit-explanation">
            <strong>Agent 建议</strong>
            <p>{suggestion.explanation_zh}</p>
          </div>
          {patchRows.length > 0 ? (
            <dl className="agent-coedit-patch">
              {patchRows.map((row) => (
                <div key={row.path}>
                  <dt>{row.path}</dt>
                  <dd className="mono wrap-anywhere">{row.value}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="no-findings">Agent 未建议具体字段改动。</p>
          )}
          {suggestion.needs_user_confirmation ? (
            <p className="agent-coedit-note">
              需要你确认后才会写入 canonical Contract。
            </p>
          ) : null}
          <div className="agent-action-row">
            <button
              className="button secondary"
              type="button"
              onClick={reject}
              disabled={suggest.isPending}
            >
              <X aria-hidden="true" size={15} /> 拒绝
            </button>
            <button
              className="button primary"
              type="button"
              onClick={apply}
              disabled={patchRows.length === 0 || suggest.isPending}
            >
              <Check aria-hidden="true" size={15} /> 应用建议
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
