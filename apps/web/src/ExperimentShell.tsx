import type { ReactNode } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  CircleDot,
  FileCheck2,
  FolderOpen,
  PlayCircle,
  Wrench,
} from "lucide-react";
import { StatusBadge } from "./components";
import { runStateLabel, runTone } from "./run-status";
import type { RunState, RunSummary } from "./types";
import type { LocationState } from "./url";
import { withSearch } from "./url";

export type ExperimentStage = "prepare" | "config" | "preflight" | "run" | "results" | "repair";

export type ExperimentContext =
  | {
      kind: "contract";
      contractId: string | null;
      title: string | null;
      dirty: boolean;
    }
  | {
      kind: "run";
      run: RunSummary;
    };

interface ExperimentShellProps {
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
  context: ExperimentContext;
  children: ReactNode;
}

const activeRunStates = new Set<RunState>([
  "SUBMITTING",
  "SUBMITTED",
  "PENDING",
  "RUNNING",
  "COMPLETING",
]);
const repairRunStates = new Set<RunState>([
  "FAILED",
  "SUBMIT_FAILED",
  "COLLECTION_FAILED",
  "ORPHANED",
]);

export function runExperimentStage(state: RunState): ExperimentStage {
  if (state === "DRAFT" || state === "VALIDATED") return "preflight";
  if (activeRunStates.has(state)) return "run";
  if (state === "SUCCEEDED") return "results";
  if (repairRunStates.has(state)) return "repair";
  return "run";
}

export function experimentRunNextAction(state: RunState): { label: string; tab: string; detail: string } {
  if (repairRunStates.has(state)) {
    return { label: "查看诊断", tab: "diagnosis", detail: "运行需要处理。先依据诊断与运行证据决定是否修复。" };
  }
  if (state === "SUCCEEDED") {
    return { label: "查看结果", tab: "results", detail: "运行已经完成，检查结果与可核验运行证据。" };
  }
  if (activeRunStates.has(state)) {
    return { label: "查看运行", tab: "logs", detail: "作业正在调度或执行，查看当前状态与增量日志。" };
  }
  if (state === "VALIDATED") {
    return { label: "检查提交状态", tab: "overview", detail: "运行已通过配置阶段检查，但尚未进入集群执行。" };
  }
  return { label: "查看摘要", tab: "overview", detail: "查看当前运行对象的状态、配置绑定与证据。" };
}

function scrollToSection(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function PhaseButton({
  label,
  detail,
  active,
  done,
  disabled,
  onClick,
  icon: Icon,
}: {
  label: string;
  detail: string;
  active?: boolean;
  done?: boolean;
  disabled?: boolean;
  onClick?: (() => void) | undefined;
  icon: typeof FolderOpen;
}) {
  return (
    <button
      type="button"
      className={`experiment-phase${active ? " is-active" : ""}${done ? " is-done" : ""}`}
      aria-label={`阶段：${label}`}
      aria-current={active ? "step" : undefined}
      disabled={disabled}
      onClick={onClick}
    >
      <span className="experiment-phase-icon">
        {done && !active ? <CheckCircle2 aria-hidden="true" /> : <Icon aria-hidden="true" />}
      </span>
      <span className="experiment-phase-copy"><strong>{label}</strong><small>{detail}</small></span>
    </button>
  );
}

export function ExperimentShell({ user, location, navigate, context, children }: ExperimentShellProps) {
  const isRun = context.kind === "run";
  const run = isRun ? context.run : null;
  const stage = run ? runExperimentStage(run.state) : "config";
  const next = run ? experimentRunNextAction(run.state) : null;
  const title = context.kind === "contract"
    ? context.title?.trim() || (context.contractId ? "实验配置" : "新建实验")
    : context.run.job_name?.trim() || "未命名实验运行";
  const contractId = context.kind === "contract" ? context.contractId : context.run.contract_id;

  const openRunTab = (tab: string) => {
    if (!run) return;
    navigate(withSearch(location.pathname, location.search, { tab, object: null }));
  };

  return (
    <div className="experiment-shell">
      <header className="experiment-shell-header">
        <div className="experiment-shell-heading">
          <p className="experiment-shell-kicker">实验工作区</p>
          <h1>实验工作区</h1>
          <strong className="experiment-shell-title" title={title}>{title}</strong>
          <p>{context.kind === "contract"
            ? "在同一上下文中准备资产、编辑配置并完成运行前检查；提交后继续进入同一实验的运行与结果阶段。"
            : "配置、运行状态、结果与修复入口保持在同一实验上下文中；所有状态均来自现有 Contract / Run 读模型。"}</p>
        </div>
        <div className="experiment-shell-actions">
          {run ? <StatusBadge label={runStateLabel(run.state)} tone={runTone(run.state)} /> : (
            <StatusBadge
              label={context.kind === "contract" && context.dirty ? "配置有未持久化修改" : contractId ? "配置已持久化" : "配置草稿"}
              tone={context.kind === "contract" && context.dirty ? "warning" : contractId ? "success" : "neutral"}
            />
          )}
          {run ? (
            <button className="button secondary" type="button" onClick={() => navigate(`/runs?user=${encodeURIComponent(user)}`)}>
              <ArrowLeft aria-hidden="true" size={15} /> 返回实验列表
            </button>
          ) : null}
        </div>
      </header>

      <div className="experiment-identity" aria-label="实验上下文标识">
        <span><small>实验配置</small><code>{contractId ?? "尚未持久化"}</code></span>
        {run ? <span><small>运行 ID</small><code>{run.run_id}</code></span> : null}
        {run ? <span><small>Slurm Job</small><code>{run.job_id ?? "尚未获得"}</code></span> : null}
        {run?.workdir ? <span className="is-wide"><small>工作目录</small><code>{run.workdir}</code></span> : null}
      </div>

      <nav className="experiment-trajectory" aria-label="实验生命周期">
        <PhaseButton
          label="准备"
          detail={isRun ? "已进入运行阶段" : "资产与路径"}
          done={isRun}
          active={!isRun && false}
          icon={FolderOpen}
          onClick={!isRun ? () => scrollToSection("contract-assets-heading") : undefined}
          disabled={isRun}
        />
        <PhaseButton
          label="配置"
          detail={contractId ? "canonical 已绑定" : "编辑中"}
          active={!isRun}
          done={isRun && Boolean(contractId)}
          icon={FileCheck2}
          disabled={isRun && !contractId}
          onClick={isRun && contractId ? () => navigate(`/studio/${encodeURIComponent(contractId)}?user=${encodeURIComponent(user)}`) : undefined}
        />
        <PhaseButton
          label="运行前检查"
          detail={isRun ? "已形成运行对象" : contractId ? "在本页完成" : "保存配置后可用"}
          active={isRun ? stage === "preflight" : false}
          done={isRun && stage !== "preflight"}
          icon={CircleDot}
          disabled={!isRun && !contractId}
          onClick={!isRun && contractId ? () => scrollToSection("run-launch-heading") : isRun ? () => openRunTab("overview") : undefined}
        />
        <PhaseButton
          label="运行"
          detail={run ? runStateLabel(run.state) : "提交后进入"}
          active={stage === "run"}
          done={Boolean(run && (stage === "results" || stage === "repair"))}
          icon={PlayCircle}
          disabled={!run}
          onClick={run ? () => openRunTab(activeRunStates.has(run.state) ? "logs" : "overview") : undefined}
        />
        <PhaseButton
          label="结果"
          detail={run?.state === "SUCCEEDED" ? "可查看" : run ? "查看已采集结果" : "等待运行"}
          active={stage === "results"}
          done={false}
          icon={CheckCircle2}
          disabled={!run}
          onClick={run ? () => openRunTab("results") : undefined}
        />
        <PhaseButton
          label="修复"
          detail={run && repairRunStates.has(run.state) ? "需要处理" : run ? "诊断入口" : "按需出现"}
          active={stage === "repair"}
          done={false}
          icon={Wrench}
          disabled={!run}
          onClick={run ? () => openRunTab("diagnosis") : undefined}
        />
      </nav>

      {run && next ? (
        <section className="experiment-next-action" aria-label="实验下一步">
          <div><small>下一步</small><strong>{next.detail}</strong></div>
          <button className="button primary" type="button" onClick={() => openRunTab(next.tab)}>{next.label}</button>
        </section>
      ) : null}

      <div className="experiment-shell-content">{children}</div>
    </div>
  );
}
