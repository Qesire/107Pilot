import { useState } from "react";
import { ArrowRight, CircleAlert, FileCheck2, Fingerprint, TerminalSquare } from "lucide-react";
import { StatusBadge } from "./components";
import { RunRepairPanel } from "./RunRepairPanel";
import { canOpenRepair } from "./run-repair";
import {
  runWorkspaceNextTab,
  type EvidenceTab,
  type RunWorkspace,
  type RunWorkspaceAttentionSeverity,
  type RunWorkspaceOutcomeKind,
} from "./run-workspace";

interface RunWorkspaceOverviewProps {
  workspace: RunWorkspace;
  onNavigate: (tab: EvidenceTab) => void;
}

const outcomeLabel: Record<RunWorkspaceOutcomeKind, string> = {
  failed: "计算失败",
  collection_failed: "证据不完整",
  collecting: "正在整理证据",
  succeeded: "计算已完成",
  running: "正在运行",
  queued: "等待执行",
};

function outcomeTone(kind: RunWorkspaceOutcomeKind): "neutral" | "info" | "success" | "warning" | "danger" {
  if (kind === "failed") return "danger";
  if (kind === "collection_failed") return "warning";
  if (kind === "succeeded") return "success";
  if (kind === "running" || kind === "collecting") return "info";
  return "neutral";
}

function attentionTone(
  severity: RunWorkspaceAttentionSeverity,
): "neutral" | "info" | "warning" | "danger" {
  if (severity === "critical") return "danger";
  if (severity === "warning") return "warning";
  if (severity === "info") return "info";
  return "neutral";
}

export function RunWorkspaceOverview({ workspace, onNavigate }: RunWorkspaceOverviewProps) {
  const [repairOpen, setRepairOpen] = useState(false);
  const targetTab = runWorkspaceNextTab(workspace.next_action.kind);
  const repairAvailable = canOpenRepair(workspace);
  const canNavigate = targetTab !== "overview";
  const canAct = repairAvailable || canNavigate;
  const evidence = workspace.evidence_summary;

  return (
    <div className="evidence-section run-workspace-overview" data-testid="run-workspace-overview">
      <section className="panel" aria-labelledby="run-outcome-heading">
        <div className="panel-heading">
          <div>
            <p className="panel-kicker">当前判断</p>
            <h3 id="run-outcome-heading">{outcomeLabel[workspace.outcome.kind]}</h3>
          </div>
          <StatusBadge
            label={workspace.states.execution}
            tone={outcomeTone(workspace.outcome.kind)}
          />
        </div>
        <p>{workspace.outcome.summary}</p>
        {workspace.attention.title ? (
          <div className="query-state" role="status">
            <CircleAlert aria-hidden="true" />
            <div>
              <strong>{workspace.attention.title}</strong>
              {workspace.attention.detail ? <p>{workspace.attention.detail}</p> : null}
            </div>
            <StatusBadge
              label={workspace.attention.severity}
              tone={attentionTone(workspace.attention.severity)}
            />
          </div>
        ) : null}
      </section>

      <section className="panel" aria-labelledby="run-next-action-heading">
        <div className="panel-heading">
          <div>
            <p className="panel-kicker">下一步</p>
            <h3 id="run-next-action-heading">{workspace.next_action.label}</h3>
          </div>
          {canAct ? (
            <button
              className="button primary"
              type="button"
              aria-expanded={repairAvailable ? repairOpen : undefined}
              onClick={() => {
                if (repairAvailable) {
                  setRepairOpen((open) => !open);
                  return;
                }
                onNavigate(targetTab);
              }}
            >
              {repairAvailable ? (repairOpen ? "收起修复" : "准备修复") : "继续"}
              <ArrowRight aria-hidden="true" size={15} />
            </button>
          ) : null}
        </div>
        <p>{workspace.next_action.detail}</p>
      </section>

      {repairOpen ? <RunRepairPanel workspace={workspace} onNavigate={onNavigate} /> : null}

      <section className="panel" aria-labelledby="run-evidence-summary-heading">
        <div className="panel-heading">
          <div>
            <p className="panel-kicker">运行证据</p>
            <h3 id="run-evidence-summary-heading">已登记的证据摘要</h3>
          </div>
          <FileCheck2 aria-hidden="true" size={18} />
        </div>
        <dl className="experiment-run-summary-grid">
          <div><dt>证据对象</dt><dd>{evidence.object_count}</dd></div>
          <div><dt>结果对象</dt><dd>{evidence.result_count}</dd></div>
          <div><dt>持久化诊断</dt><dd>{evidence.diagnosis_count}</dd></div>
          <div><dt>标准输出</dt><dd>{evidence.stdout_available ? "已登记" : "未登记"}</dd></div>
          <div><dt>错误输出</dt><dd>{evidence.stderr_available ? "已登记" : "未登记"}</dd></div>
          <div><dt>自动归档</dt><dd>{evidence.capsule_available ? "可用" : "未就绪"}</dd></div>
        </dl>
      </section>

      <section className="panel" aria-labelledby="run-provenance-heading">
        <div className="panel-heading">
          <div>
            <p className="panel-kicker">来源与绑定</p>
            <h3 id="run-provenance-heading">可追溯事实</h3>
          </div>
          <Fingerprint aria-hidden="true" size={18} />
        </div>
        <dl className="fact-list">
          <div><dt>实验配置</dt><dd className="mono">{workspace.provenance.contract_id ?? "未绑定"}</dd></div>
          <div><dt>配置摘要</dt><dd className="mono wrap-anywhere">{workspace.provenance.contract_digest ?? "未登记"}</dd></div>
          <div><dt>Slurm Job</dt><dd className="mono">{workspace.provenance.job_id ?? "尚未提交"}</dd></div>
          <div><dt>父运行</dt><dd className="mono">{workspace.provenance.parent_run_id ?? "无"}</dd></div>
          <div><dt>来源修订</dt><dd className="mono wrap-anywhere">{workspace.provenance.source_revision ?? "未登记"}</dd></div>
          <div><dt>平台快照</dt><dd className="mono wrap-anywhere">{workspace.provenance.platform_snapshot_ref ?? "未登记"}</dd></div>
          <div className="is-wide"><dt>工作目录</dt><dd className="mono wrap-anywhere">{workspace.provenance.workdir ?? "服务器 read model 未公开"}</dd></div>
        </dl>
        <p className="panel-footnote">
          <TerminalSquare aria-hidden="true" size={14} />
          这里展示的是持久化运行事实；计算成功不会被解释为科学结论已经成立。
        </p>
      </section>
    </div>
  );
}
