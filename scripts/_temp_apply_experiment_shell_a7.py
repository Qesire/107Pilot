from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if old not in text:
        raise SystemExit(f"missing expected source block in {path}: {old[:120]!r}")
    if text.count(old) != 1:
        raise SystemExit(f"expected exactly one source block in {path}, found {text.count(old)}")
    path.write_text(text.replace(old, new, 1))


experiment_shell = r'''import type { ReactNode } from "react";
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
  onClick?: () => void;
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
              label={context.kind === "contract" && context.dirty ? "配置有未持久化修改" : context.contractId ? "配置已持久化" : "配置草稿"}
              tone={context.kind === "contract" && context.dirty ? "warning" : context.contractId ? "success" : "neutral"}
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
'''

experiment_test = r'''import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { ExperimentShell, experimentRunNextAction, runExperimentStage } from "./ExperimentShell";
import type { RunSummary } from "./types";

function run(state: RunSummary["state"]): RunSummary {
  return {
    run_id: "run_test",
    contract_id: "contract_test",
    owner: "alice",
    state,
    collection_state: "succeeded",
    diagnosis_state: "idle",
    capsule_state: "none",
    result_status: "unknown",
    job_id: "123",
    exit_code: state === "SUCCEEDED" ? "0:0" : "1:0",
    created_at: "2026-09-03T00:00:00Z",
    updated_at: "2026-09-03T00:00:00Z",
  };
}

const location = { pathname: "/runs/run_test", search: new URLSearchParams("user=alice") };

describe("ExperimentShell", () => {
  it("maps authoritative Run states onto lifecycle stages", () => {
    expect(runExperimentStage("VALIDATED")).toBe("preflight");
    expect(runExperimentStage("RUNNING")).toBe("run");
    expect(runExperimentStage("SUCCEEDED")).toBe("results");
    expect(runExperimentStage("FAILED")).toBe("repair");
  });

  it("derives one decision-oriented next action from the Run state", () => {
    expect(experimentRunNextAction("FAILED").tab).toBe("diagnosis");
    expect(experimentRunNextAction("SUCCEEDED").tab).toBe("results");
    expect(experimentRunNextAction("RUNNING").tab).toBe("logs");
  });

  it("renders a contract lifecycle without inventing a Run identity", () => {
    const markup = renderToStaticMarkup(
      <ExperimentShell
        user="alice"
        location={{ pathname: "/studio/new", search: new URLSearchParams("user=alice") }}
        navigate={vi.fn()}
        context={{ kind: "contract", contractId: null, title: null, dirty: true }}
      >
        <div>content</div>
      </ExperimentShell>,
    );
    expect(markup).toContain("实验工作区");
    expect(markup).toContain("尚未持久化");
    expect(markup).not.toContain("运行 ID");
  });

  it("renders Run and Contract identifiers from the existing read model", () => {
    const markup = renderToStaticMarkup(
      <ExperimentShell
        user="alice"
        location={location}
        navigate={vi.fn()}
        context={{ kind: "run", run: run("FAILED") }}
      >
        <div>content</div>
      </ExperimentShell>,
    );
    expect(markup).toContain("contract_test");
    expect(markup).toContain("run_test");
    expect(markup).toContain("需要处理");
  });
});
'''

experiment_css = r'''.experiment-shell {
  display: grid;
  gap: 18px;
  min-width: 0;
}

.experiment-shell-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding: 4px 2px 0;
}

.experiment-shell-heading { min-width: 0; }
.experiment-shell-kicker {
  margin: 0 0 5px;
  color: var(--accent-primary);
  font-size: 12px;
  font-weight: 720;
  letter-spacing: .09em;
}
.experiment-shell-heading h1 {
  margin: 0;
  color: var(--text-primary);
  font-size: clamp(26px, 2.2vw, 34px);
  line-height: 1.15;
  letter-spacing: -.035em;
}
.experiment-shell-title {
  display: block;
  max-width: 760px;
  margin-top: 9px;
  overflow: hidden;
  color: var(--text-secondary);
  font-size: 15px;
  font-weight: 680;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.experiment-shell-heading > p:last-child {
  max-width: 840px;
  margin: 7px 0 0;
  color: var(--text-tertiary);
  font-size: 13px;
  line-height: 1.6;
}
.experiment-shell-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex: none;
  flex-wrap: wrap;
  gap: 9px;
}

.experiment-identity {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  gap: 8px;
}
.experiment-identity > span {
  display: grid;
  min-width: 150px;
  gap: 2px;
  border: 1px solid var(--border-default);
  border-radius: 8px;
  background: var(--bg-subtle);
  padding: 7px 10px;
}
.experiment-identity > span.is-wide { min-width: min(420px, 100%); flex: 1; }
.experiment-identity small { color: var(--text-tertiary); font-size: 10px; font-weight: 650; }
.experiment-identity code {
  overflow: hidden;
  color: var(--text-secondary);
  font-family: var(--font-mono);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.experiment-trajectory {
  position: relative;
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  overflow: hidden;
  border: 1px solid var(--border-default);
  border-radius: 12px;
  background: var(--bg-surface);
}
.experiment-trajectory::before {
  position: absolute;
  top: 24px;
  right: 8.4%;
  left: 8.4%;
  height: 1px;
  background: var(--border-default);
  content: "";
  pointer-events: none;
}
.experiment-phase {
  position: relative;
  z-index: 1;
  display: grid;
  min-width: 0;
  grid-template-columns: 30px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  border: 0;
  border-right: 1px solid var(--border-default);
  background: transparent;
  padding: 11px 10px;
  color: var(--text-secondary);
  text-align: left;
  cursor: pointer;
}
.experiment-phase:last-child { border-right: 0; }
.experiment-phase:hover:not(:disabled) { background: var(--bg-subtle); }
.experiment-phase:disabled { opacity: .58; cursor: default; }
.experiment-phase.is-active { background: color-mix(in srgb, var(--accent-primary) 7%, var(--bg-surface)); color: var(--text-primary); }
.experiment-phase-icon {
  display: grid;
  width: 28px;
  height: 28px;
  place-items: center;
  border: 1px solid var(--border-default);
  border-radius: 50%;
  background: var(--bg-surface);
  color: var(--text-tertiary);
}
.experiment-phase-icon svg { width: 14px; height: 14px; }
.experiment-phase.is-active .experiment-phase-icon {
  border-color: var(--accent-primary);
  background: var(--accent-primary);
  color: #fff;
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent-primary) 11%, transparent);
}
.experiment-phase.is-done .experiment-phase-icon { border-color: var(--state-success); color: var(--state-success); }
.experiment-phase-copy { display: grid; min-width: 0; gap: 2px; }
.experiment-phase-copy strong { overflow: hidden; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.experiment-phase-copy small { overflow: hidden; color: var(--text-tertiary); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }

.experiment-next-action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  border: 1px solid color-mix(in srgb, var(--accent-primary) 25%, var(--border-default));
  border-left: 3px solid var(--accent-primary);
  border-radius: 10px;
  background: color-mix(in srgb, var(--accent-primary) 4%, var(--bg-surface));
  padding: 11px 12px 11px 14px;
}
.experiment-next-action > div { display: grid; gap: 2px; min-width: 0; }
.experiment-next-action small { color: var(--text-tertiary); font-size: 10px; font-weight: 700; }
.experiment-next-action strong { color: var(--text-primary); font-size: 12px; font-weight: 620; }
.experiment-shell-content { min-width: 0; }

.experiment-run-workspace {
  display: grid;
  gap: 16px;
  min-width: 0;
}
.experiment-run-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
}
.experiment-run-heading h2 { margin: 0; font-size: 17px; }
.experiment-run-heading p { margin: 3px 0 0; color: var(--text-tertiary); font-size: 12px; }
.experiment-run-summary {
  display: grid;
  gap: 12px;
  border: 1px solid var(--border-default);
  border-radius: 12px;
  background: var(--bg-surface);
  padding: 14px;
}
.experiment-run-summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}
.experiment-run-summary-grid > div {
  min-width: 0;
  border-left: 2px solid var(--border-default);
  padding-left: 9px;
}
.experiment-run-summary-grid dt { color: var(--text-tertiary); font-size: 10px; font-weight: 650; }
.experiment-run-summary-grid dd { margin: 3px 0 0; overflow-wrap: anywhere; color: var(--text-secondary); font-size: 12px; }
.experiment-run-summary-grid .is-wide { grid-column: span 2; }

@media (max-width: 1100px) {
  .experiment-trajectory { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .experiment-trajectory::before { display: none; }
  .experiment-phase:nth-child(3) { border-right: 0; }
  .experiment-phase:nth-child(-n + 3) { border-bottom: 1px solid var(--border-default); }
  .experiment-run-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 760px) {
  .experiment-shell-header,
  .experiment-next-action { align-items: stretch; flex-direction: column; }
  .experiment-shell-actions { justify-content: flex-start; }
  .experiment-trajectory { grid-template-columns: 1fr 1fr; }
  .experiment-phase { border-bottom: 1px solid var(--border-default); }
  .experiment-phase:nth-child(2n) { border-right: 0; }
  .experiment-phase:nth-last-child(-n + 2) { border-bottom: 0; }
  .experiment-run-summary-grid { grid-template-columns: 1fr; }
  .experiment-run-summary-grid .is-wide { grid-column: auto; }
}
'''

(ROOT / "apps/web/src/ExperimentShell.tsx").write_text(experiment_shell)
(ROOT / "apps/web/src/ExperimentShell.test.tsx").write_text(experiment_test)
(ROOT / "apps/web/src/styles/experiment-shell-v2.css").write_text(experiment_css)

# Load the shell stylesheet after the A6 asset and picker layers.
main = ROOT / "apps/web/src/main.tsx"
replace_once(
    main,
    'import "./styles/file-picker-v2.css";\n',
    'import "./styles/file-picker-v2.css";\nimport "./styles/experiment-shell-v2.css";\n',
)

# Studio: replace the page-level SectionHeading with the shared lifecycle shell.
studio = ROOT / "apps/web/src/StudioPage.tsx"
replace_once(
    studio,
    'import { QueryBoundary, SectionHeading, StatusBadge } from "./components";\nimport { ContractAssetSummary } from "./ContractAssetSummary";\n',
    'import { QueryBoundary, StatusBadge } from "./components";\nimport { ContractAssetSummary } from "./ContractAssetSummary";\nimport { ExperimentShell } from "./ExperimentShell";\n',
)
replace_once(
    studio,
    '  const studioLoading = schemaQuery.isPending || recipes.isPending;\n\n  return (\n    <>\n      <SectionHeading\n        eyebrow="Contract Studio / canonical state"\n        title={contractId ? "检查与派生 Contract" : "新建 Contract"}\n        detail="表单、源码与 Agent 共享同一 canonical object；左侧编辑表单，中间实时同步源码，右侧让 Agent 建议改动。服务端 validation 始终是最终权威。"\n      />\n\n      <QueryBoundary\n',
    '  const studioLoading = schemaQuery.isPending || recipes.isPending;\n  const projectName = readContractValue(canonical, ["project", "name"], "");\n\n  return (\n    <ExperimentShell\n      user={user}\n      location={location}\n      navigate={navigate}\n      context={{\n        kind: "contract",\n        contractId,\n        title: typeof projectName === "string" ? projectName : null,\n        dirty: canonicalDirty,\n      }}\n    >\n      <QueryBoundary\n',
)
replace_once(
    studio,
    '      </QueryBoundary>\n    </>\n  );\n}\n\nfunction RunLaunchPanel',
    '      </QueryBoundary>\n    </ExperimentShell>\n  );\n}\n\nfunction RunLaunchPanel',
)

# Runs: list route stays a decision surface; detail route becomes a full experiment workspace.
pages = ROOT / "apps/web/src/pages.tsx"
replace_once(
    pages,
    'import { ConnectionPanel } from "./ConnectionStatus";\n',
    'import { ConnectionPanel } from "./ConnectionStatus";\nimport { ExperimentShell } from "./ExperimentShell";\n',
)
text = pages.read_text()
start = text.index('export function RunsPage({ user, location, navigate }: PageProps) {')
end = text.index('export function TerminalCollaborationPage', start)
new_runs = r'''export function RunsPage({ user, location, navigate }: PageProps) {
  const selectedRunId = location.pathname.startsWith("/runs/")
    ? decodeURIComponent(location.pathname.slice("/runs/".length))
    : null;
  if (selectedRunId) {
    return <RunExperimentPage user={user} location={location} navigate={navigate} runId={selectedRunId} />;
  }
  return <RunsIndexPage user={user} location={location} navigate={navigate} />;
}

function RunsIndexPage({ user, location, navigate }: PageProps) {
  const state = location.search.get("state") ?? "";
  const search = location.search.get("q") ?? "";
  const runs = useRunPages(user, state || undefined, search || undefined);
  const items = runs.data?.pages.flatMap((page) => page.items) ?? [];
  const [savedFilters, setSavedFilters] = useState(() => loadRunFilters(user));
  useEffect(() => setSavedFilters(loadRunFilters(user)), [user]);
  const updateFilters = (updates: Record<string, string | null>) =>
    navigate(withSearch("/runs", location.search, updates));

  return (
    <>
      <SectionHeading
        eyebrow="实验 / 运行历史"
        title="实验运行"
        detail="这里是实验运行历史入口。打开某次运行后，会进入独立的实验工作区，而不是在列表旁展开抽屉。"
      />
      <section className="filter-bar" aria-label="运行筛选">
        <label className="search-field">
          <Search aria-hidden="true" size={17} />
          <span className="sr-only">搜索运行</span>
          <input
            value={search}
            placeholder="搜索运行 ID、Job ID 或工作目录"
            onChange={(event) => updateFilters({ q: event.target.value || null })}
          />
        </label>
        <label className="select-field">
          <SlidersHorizontal aria-hidden="true" size={16} />
          <span className="sr-only">状态</span>
          <select value={state} onChange={(event) => updateFilters({ state: event.target.value || null })}>
            <option value="">全部状态</option>
            <option value="RUNNING">运行中</option>
            <option value="PENDING">排队中</option>
            <option value="SUCCEEDED">已成功</option>
            <option value="FAILED">已失败</option>
            <option value="CANCELLED">已取消</option>
          </select>
        </label>
        <div className="filter-actions">
          <button type="button" onClick={() => {
            const next = { state, search };
            saveRunFilters(user, next);
            setSavedFilters(next);
          }}>保存筛选</button>
          <button type="button" disabled={!savedFilters} onClick={() => savedFilters && updateFilters({
            state: savedFilters.state || null,
            q: savedFilters.search || null,
          })}>应用已保存</button>
          <button type="button" disabled={!savedFilters} onClick={() => {
            clearRunFilters(user);
            setSavedFilters(null);
          }}>清除保存</button>
        </div>
      </section>

      <section className="panel runs-panel" aria-labelledby="runs-table-heading">
        <div className="panel-heading">
          <div><p className="panel-kicker">运行读模型</p><h2 id="runs-table-heading">已加载 {items.length} 个结果</h2></div>
          {runs.isFetching ? <StatusBadge label="同步中" tone="info" /> : <StatusBadge label="已同步" tone="success" />}
        </div>
        <QueryBoundary
          pending={runs.isPending}
          error={runs.error}
          empty={items.length === 0}
          emptyTitle="没有匹配的运行"
          emptyDetail="调整状态或搜索词；筛选不会改变服务器数据。"
        >
          <RunList
            runs={items}
            selectedRunId={null}
            onSelect={(runId) => navigate(withSearch(`/runs/${runId}`, location.search, { tab: "overview", object: null }))}
          />
          {runs.hasNextPage ? (
            <button className="button secondary pagination-more" type="button" disabled={runs.isFetchingNextPage} onClick={() => void runs.fetchNextPage()}>
              {runs.isFetchingNextPage ? "正在加载" : "加载更多"}
            </button>
          ) : null}
        </QueryBoundary>
      </section>
    </>
  );
}

function RunExperimentPage({
  user,
  location,
  navigate,
  runId,
}: PageProps & { runId: string }) {
  const selectedRun = useRun(user, runId);
  return (
    <QueryBoundary pending={selectedRun.isPending} error={selectedRun.error}>
      {selectedRun.data ? (
        <ExperimentShell
          user={user}
          location={location}
          navigate={navigate}
          context={{ kind: "run", run: selectedRun.data }}
        >
          <section className="experiment-run-workspace" aria-labelledby="run-detail-heading">
            <header className="experiment-run-heading">
              <div>
                <h2 id="run-detail-heading">运行详情</h2>
                <p>当前只读取这一运行对象及其证据；实验历史列表不会在详情路由中并行加载。</p>
              </div>
              {selectedRun.data.capsule_state === "ready" ? (
                <button
                  className="run-capsule-link"
                  type="button"
                  onClick={() => navigate(withSearch(location.pathname, location.search, { tab: "capsule", object: null }))}
                >
                  <Archive aria-hidden="true" size={15} /> 查看自动归档
                </button>
              ) : null}
            </header>

            <section className="experiment-run-summary" aria-label="运行事实">
              <div className="run-detail-status-line">
                <StatusBadge label={runStateLabel(selectedRun.data.state)} tone={runTone(selectedRun.data.state)} />
                <span className="run-detail-job-name" title={selectedRun.data.job_name ?? selectedRun.data.run_id}>
                  <strong>{selectedRun.data.job_name ?? "历史实验运行"}</strong>
                  <span> · sacct Job <b className="mono">{selectedRun.data.job_id ?? "尚未提交"}</b></span>
                </span>
              </div>
              <dl className="experiment-run-summary-grid">
                <div><dt>实验配置</dt><dd className="mono">{selectedRun.data.contract_id ?? "服务器 read model 未公开"}</dd></div>
                <div><dt>ExitCode</dt><dd className="mono">{selectedRun.data.exit_code ?? "—"}</dd></div>
                <div><dt>证据状态</dt><dd>{selectedRun.data.collection_state}</dd></div>
                <div><dt>Capsule</dt><dd>{selectedRun.data.capsule_state}</dd></div>
                <div className="is-wide"><dt>工作目录</dt><dd className="mono wrap-anywhere">{selectedRun.data.workdir ?? "服务器 read model 未公开"}</dd></div>
                <div><dt>诊断状态</dt><dd>{selectedRun.data.diagnosis_state}</dd></div>
                <div><dt>结果状态</dt><dd>{selectedRun.data.result_status}</dd></div>
              </dl>
            </section>

            <RunEvidencePanel user={user} run={selectedRun.data} location={location} navigate={navigate} />
          </section>
        </ExperimentShell>
      ) : null}
    </QueryBoundary>
  );
}

'''
pages.write_text(text[:start] + new_runs + text[end:])

# Append focused browser gates to the existing mock harness so they reuse its
# canonical Run/Contract fixtures without creating a second fake backend.
visual = ROOT / "tests/ui/visual.spec.js"
visual_text = visual.read_text()
marker = '\nasync function capture(page, filename) {'
if marker not in visual_text:
    raise SystemExit("visual.spec.js capture marker missing")
new_tests = r'''

test("experiment shell opens a Run as one workspace without loading the history list", async ({ page }) => {
  let listRequests = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/runs") listRequests += 1;
  });

  await page.goto("/runs/run_alice_failed?user=alice");
  await expect(page.getByRole("heading", { name: "实验工作区" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "运行详情" })).toBeVisible();
  await expect(page.getByText("contract_alice_002", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("/work/alice/projects/a-very-long-directory-name/failed-case", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: /已加载 .* 个结果/ })).toHaveCount(0);
  await expect.poll(() => listRequests).toBe(0);

  await page.getByRole("button", { name: "阶段：修复" }).click();
  await expect(page).toHaveURL(/tab=diagnosis/);
  await expect(page.getByText("RUNTIME.PYTHON_PACKAGE_MISSING", { exact: true })).toBeVisible();
});

test("experiment shell keeps Studio preparation and preflight in the same context", async ({ page }) => {
  await page.goto("/studio/new?user=alice");
  await expect(page.getByRole("heading", { name: "实验工作区" })).toBeVisible();
  await expect(page.getByRole("button", { name: "阶段：准备" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "阶段：配置" })).toHaveAttribute("aria-current", "step");
  await expect(page.getByRole("button", { name: "阶段：运行前检查" })).toBeDisabled();
  await page.getByRole("button", { name: "阶段：准备" }).click();
  await expect(page.getByRole("heading", { name: "实验资产" })).toBeVisible();
  await expect(page).toHaveURL(/\/studio\/new\?user=alice/);
});
'''
visual.write_text(visual_text.replace(marker, new_tests + marker, 1))

print("A7 experiment shell migration applied")
