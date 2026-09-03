from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one source block in {path}, found {count}: {old[:100]!r}")
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
export type ContractPreflightStatus = "idle" | "pending" | "ok" | "blocked" | "error";
export type LaunchOperationStatus = "idle" | "pending" | "error";
export type PrepareOperationStatus = LaunchOperationStatus | "prepared";

export interface ContractLaunchProgress {
  preflightStatus: ContractPreflightStatus;
  prepareStatus: PrepareOperationStatus;
  preparedRunId: string | null;
  submitStatus: LaunchOperationStatus;
}

export const EMPTY_CONTRACT_LAUNCH_PROGRESS: ContractLaunchProgress = {
  preflightStatus: "idle",
  prepareStatus: "idle",
  preparedRunId: null,
  submitStatus: "idle",
};

export interface ContractExperimentContext {
  kind: "contract";
  contractId: string | null;
  title: string | null;
  dirty: boolean;
  launch: ContractLaunchProgress;
}

export type ExperimentContext =
  | ContractExperimentContext
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

export function contractExperimentStage(context: ContractExperimentContext): ExperimentStage {
  if (!context.contractId || context.dirty) return "config";
  if (context.launch.preparedRunId || context.launch.submitStatus === "pending") return "run";
  return "preflight";
}

export function contractPreflightDetail(context: ContractExperimentContext): string {
  if (!context.contractId) return "保存配置后可用";
  if (context.dirty) return "配置已变更，需重新检查";
  if (context.launch.prepareStatus === "pending") return "正在固化运行";
  switch (context.launch.preflightStatus) {
    case "pending": return "检查中";
    case "ok": return "检查通过";
    case "blocked": return "检查未通过";
    case "error": return "检查失败";
    default: return "待执行";
  }
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

export function experimentContractNextAction(context: ContractExperimentContext): { label: string; anchor: string; detail: string } {
  if (!context.contractId) {
    return { label: "继续配置", anchor: "experiment-config-editor", detail: "先完成实验配置并创建 canonical Contract，随后才能执行运行前检查。" };
  }
  if (context.dirty) {
    return { label: "继续配置", anchor: "experiment-config-editor", detail: "当前配置已有修改。先另存为新 Contract；旧运行前检查不会被视为仍然有效。" };
  }
  if (context.launch.submitStatus === "pending") {
    return { label: "查看提交", anchor: "run-launch-heading", detail: "Prepared Run 正在提交到调度系统。" };
  }
  if (context.launch.submitStatus === "error") {
    return { label: "检查提交错误", anchor: "run-launch-heading", detail: "提交未成功；Prepared Run 保留，查看服务器错误后再决定是否重试。" };
  }
  if (context.launch.preparedRunId) {
    return { label: "确认提交", anchor: "run-launch-heading", detail: `Prepared Run ${context.launch.preparedRunId} 已形成。确认脚本、Contract 与风险后提交。` };
  }
  if (context.launch.prepareStatus === "pending") {
    return { label: "查看准备状态", anchor: "run-launch-heading", detail: "运行前检查已通过，正在固化不可变的 Prepared Run。" };
  }
  if (context.launch.prepareStatus === "error") {
    return { label: "检查准备错误", anchor: "run-launch-heading", detail: "运行前检查已经通过，但创建 Prepared Run 失败。查看错误后重试。" };
  }
  switch (context.launch.preflightStatus) {
    case "pending":
      return { label: "查看检查状态", anchor: "run-launch-heading", detail: "正在执行运行前检查；结果由服务器 preflight 返回。" };
    case "ok":
      return { label: "准备运行", anchor: "run-launch-heading", detail: "运行前检查已通过，可以固化 Prepared Run。" };
    case "blocked":
      return { label: "查看阻断项", anchor: "run-launch-heading", detail: "运行前检查未通过；先处理服务器 findings，再重新检查。" };
    case "error":
      return { label: "查看检查错误", anchor: "run-launch-heading", detail: "运行前检查请求失败；查看服务器错误后重试。" };
    default:
      return { label: "开始运行前检查", anchor: "run-launch-heading", detail: "canonical Contract 已绑定。下一步执行运行动态预检。" };
  }
}

function contractStatus(context: ContractExperimentContext): { label: string; tone: "neutral" | "info" | "success" | "warning" | "danger" } {
  if (context.dirty) return { label: "配置有未持久化修改", tone: "warning" };
  if (!context.contractId) return { label: "配置草稿", tone: "neutral" };
  if (context.launch.submitStatus === "pending") return { label: "正在提交运行", tone: "info" };
  if (context.launch.submitStatus === "error") return { label: "运行提交失败", tone: "danger" };
  if (context.launch.preparedRunId) return { label: "等待确认提交", tone: "warning" };
  if (context.launch.prepareStatus === "pending") return { label: "正在准备运行", tone: "info" };
  if (context.launch.prepareStatus === "error") return { label: "准备运行失败", tone: "danger" };
  switch (context.launch.preflightStatus) {
    case "pending": return { label: "运行前检查中", tone: "info" };
    case "ok": return { label: "运行前检查通过", tone: "success" };
    case "blocked": return { label: "运行前检查未通过", tone: "danger" };
    case "error": return { label: "运行前检查失败", tone: "danger" };
    default: return { label: "配置已持久化", tone: "success" };
  }
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
  const stage = run ? runExperimentStage(run.state) : contractExperimentStage(context as ContractExperimentContext);
  const runNext = run ? experimentRunNextAction(run.state) : null;
  const contractNext = context.kind === "contract" ? experimentContractNextAction(context) : null;
  const contractState = context.kind === "contract" ? contractStatus(context) : null;
  const title = context.kind === "contract"
    ? context.title?.trim() || (context.contractId ? "实验配置" : "新建实验")
    : context.run.job_name?.trim() || "未命名实验运行";
  const contractId = context.kind === "contract" ? context.contractId : context.run.contract_id;
  const preparedRunId = context.kind === "contract" ? context.launch.preparedRunId : null;

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
          {run ? <StatusBadge label={runStateLabel(run.state)} tone={runTone(run.state)} /> : contractState ? (
            <StatusBadge label={contractState.label} tone={contractState.tone} />
          ) : null}
          {run ? (
            <button className="button secondary" type="button" onClick={() => navigate(`/runs?user=${encodeURIComponent(user)}`)}>
              <ArrowLeft aria-hidden="true" size={15} /> 返回实验列表
            </button>
          ) : null}
        </div>
      </header>

      <div className="experiment-identity" aria-label="实验上下文标识">
        <span><small>实验配置</small><code>{contractId ?? "尚未持久化"}</code></span>
        {preparedRunId ? <span><small>准备运行</small><code>{preparedRunId}</code></span> : null}
        {run ? <span><small>运行 ID</small><code>{run.run_id}</code></span> : null}
        {run ? <span><small>Slurm Job</small><code>{run.job_id ?? "尚未获得"}</code></span> : null}
        {run?.workdir ? <span className="is-wide"><small>工作目录</small><code>{run.workdir}</code></span> : null}
      </div>

      <nav className="experiment-trajectory" aria-label="实验生命周期">
        <PhaseButton
          label="准备"
          detail={isRun ? "已进入运行阶段" : "资产与路径"}
          done={isRun}
          active={false}
          icon={FolderOpen}
          onClick={!isRun ? () => scrollToSection("contract-assets-heading") : undefined}
          disabled={isRun}
        />
        <PhaseButton
          label="配置"
          detail={context.kind === "contract" && context.dirty ? "有未持久化修改" : contractId ? "canonical 已绑定" : "编辑中"}
          active={stage === "config"}
          done={stage !== "config" && Boolean(contractId)}
          icon={FileCheck2}
          disabled={isRun && !contractId}
          onClick={isRun && contractId ? () => navigate(`/studio/${encodeURIComponent(contractId)}?user=${encodeURIComponent(user)}`) : context.kind === "contract" ? () => scrollToSection("experiment-config-editor") : undefined}
        />
        <PhaseButton
          label="运行前检查"
          detail={context.kind === "contract" ? contractPreflightDetail(context) : "已形成运行对象"}
          active={stage === "preflight"}
          done={stage === "run" || stage === "results" || stage === "repair"}
          icon={CircleDot}
          disabled={context.kind === "contract" ? !contractId : false}
          onClick={context.kind === "contract" && contractId ? () => scrollToSection("run-launch-heading") : isRun ? () => openRunTab("overview") : undefined}
        />
        <PhaseButton
          label="运行"
          detail={run ? runStateLabel(run.state) : context.kind === "contract" && context.launch.submitStatus === "pending" ? "正在提交" : preparedRunId ? "待确认提交" : "提交后进入"}
          active={stage === "run"}
          done={Boolean(run && (stage === "results" || stage === "repair"))}
          icon={PlayCircle}
          disabled={!run && !preparedRunId}
          onClick={run ? () => openRunTab(activeRunStates.has(run.state) ? "logs" : "overview") : preparedRunId ? () => scrollToSection("run-launch-heading") : undefined}
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

      {run && runNext ? (
        <section className="experiment-next-action" aria-label="实验下一步">
          <div><small>下一步</small><strong>{runNext.detail}</strong></div>
          <button className="button primary" type="button" onClick={() => openRunTab(runNext.tab)}>{runNext.label}</button>
        </section>
      ) : null}
      {context.kind === "contract" && contractNext ? (
        <section className="experiment-next-action" aria-label="实验下一步">
          <div><small>下一步</small><strong>{contractNext.detail}</strong></div>
          <button className="button primary" type="button" onClick={() => scrollToSection(contractNext.anchor)}>{contractNext.label}</button>
        </section>
      ) : null}

      <div className="experiment-shell-content">{children}</div>
    </div>
  );
}
'''

experiment_test = r'''import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import {
  EMPTY_CONTRACT_LAUNCH_PROGRESS,
  ExperimentShell,
  contractExperimentStage,
  contractPreflightDetail,
  experimentContractNextAction,
  experimentRunNextAction,
  runExperimentStage,
  type ContractExperimentContext,
} from "./ExperimentShell";
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

function contract(overrides: Partial<ContractExperimentContext> = {}): ContractExperimentContext {
  return {
    kind: "contract",
    contractId: "contract_test",
    title: "test",
    dirty: false,
    launch: EMPTY_CONTRACT_LAUNCH_PROGRESS,
    ...overrides,
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

  it("promotes persisted clean contracts to preflight and prepared runs to run", () => {
    expect(contractExperimentStage(contract())).toBe("preflight");
    expect(contractExperimentStage(contract({ dirty: true }))).toBe("config");
    expect(contractExperimentStage(contract({ contractId: null }))).toBe("config");
    expect(contractExperimentStage(contract({ launch: {
      ...EMPTY_CONTRACT_LAUNCH_PROGRESS,
      preparedRunId: "run_prepared",
      prepareStatus: "prepared",
    } }))).toBe("run");
  });

  it("never treats a changed Contract as retaining a successful preflight", () => {
    const changed = contract({
      dirty: true,
      launch: { ...EMPTY_CONTRACT_LAUNCH_PROGRESS, preflightStatus: "ok" },
    });
    expect(contractPreflightDetail(changed)).toBe("配置已变更，需重新检查");
    expect(experimentContractNextAction(changed).anchor).toBe("experiment-config-editor");
  });

  it("derives contract actions from real launch progress", () => {
    const checked = contract({ launch: { ...EMPTY_CONTRACT_LAUNCH_PROGRESS, preflightStatus: "ok" } });
    expect(experimentContractNextAction(checked).label).toBe("准备运行");
    const prepared = contract({ launch: {
      ...EMPTY_CONTRACT_LAUNCH_PROGRESS,
      preflightStatus: "ok",
      prepareStatus: "prepared",
      preparedRunId: "run_prepared",
    } });
    expect(experimentContractNextAction(prepared).label).toBe("确认提交");
  });

  it("renders a contract lifecycle without inventing a Run identity", () => {
    const markup = renderToStaticMarkup(
      <ExperimentShell
        user="alice"
        location={{ pathname: "/studio/new", search: new URLSearchParams("user=alice") }}
        navigate={vi.fn()}
        context={{ kind: "contract", contractId: null, title: null, dirty: true, launch: EMPTY_CONTRACT_LAUNCH_PROGRESS }}
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

(ROOT / "apps/web/src/ExperimentShell.tsx").write_text(experiment_shell)
(ROOT / "apps/web/src/ExperimentShell.test.tsx").write_text(experiment_test)

studio = ROOT / "apps/web/src/StudioPage.tsx"
replace_once(
    studio,
    'import { ExperimentShell } from "./ExperimentShell";\n',
    'import {\n  EMPTY_CONTRACT_LAUNCH_PROGRESS,\n  ExperimentShell,\n  type ContractLaunchProgress,\n} from "./ExperimentShell";\n',
)
replace_once(
    studio,
    '  const [hydratedContractId, setHydratedContractId] = useState<string | null>(null);\n',
    '  const [hydratedContractId, setHydratedContractId] = useState<string | null>(null);\n  const [launchProgress, setLaunchProgress] = useState<ContractLaunchProgress>(EMPTY_CONTRACT_LAUNCH_PROGRESS);\n',
)
replace_once(
    studio,
    '  useEffect(() => {\n    if (!sourceDirty) setSource(serializeContract(canonical, format));\n  }, [format]); // canonical changes are synchronized through commitCanonical.\n',
    '  useEffect(() => {\n    if (!sourceDirty) setSource(serializeContract(canonical, format));\n  }, [format]); // canonical changes are synchronized through commitCanonical.\n\n  useEffect(() => {\n    setLaunchProgress(EMPTY_CONTRACT_LAUNCH_PROGRESS);\n  }, [contractId]);\n',
)
replace_once(
    studio,
    '        dirty: canonicalDirty,\n      }}\n',
    '        dirty: canonicalDirty,\n        launch: launchProgress,\n      }}\n',
)
replace_once(
    studio,
    '          <div className="studio-body-3col">\n',
    '          <div className="studio-body-3col" id="experiment-config-editor">\n',
)
replace_once(
    studio,
    '          {contractId ? <RunLaunchPanel user={user} contractId={contractId} localDirty={canonicalDirty} navigate={navigate} /> : null}\n',
    '          {contractId ? (\n            <RunLaunchPanel\n              key={contractId}\n              user={user}\n              contractId={contractId}\n              localDirty={canonicalDirty}\n              navigate={navigate}\n              onProgressChange={setLaunchProgress}\n            />\n          ) : null}\n',
)
replace_once(
    studio,
    'function RunLaunchPanel({ user, contractId, localDirty, navigate }: { user: string; contractId: string; localDirty: boolean; navigate: (path: string) => void }) {\n',
    'function RunLaunchPanel({ user, contractId, localDirty, navigate, onProgressChange }: {\n  user: string;\n  contractId: string;\n  localDirty: boolean;\n  navigate: (path: string) => void;\n  onProgressChange: (progress: ContractLaunchProgress) => void;\n}) {\n',
)
replace_once(
    studio,
    '  useEffect(() => {\n    if (localDirty) setConfirmed(false);\n  }, [localDirty]);\n  const preflightOk = preflight.data?.status === "OK";\n',
    '  useEffect(() => {\n    if (localDirty) setConfirmed(false);\n  }, [localDirty]);\n  const preflightOk = preflight.data?.status === "OK";\n  const preflightStatus: ContractLaunchProgress["preflightStatus"] = preflight.isPending\n    ? "pending"\n    : preflight.isError\n      ? "error"\n      : preflight.data\n        ? preflightOk ? "ok" : "blocked"\n        : "idle";\n  const prepareStatus: ContractLaunchProgress["prepareStatus"] = prepare.isPending\n    ? "pending"\n    : prepare.isError\n      ? "error"\n      : prepare.data\n        ? "prepared"\n        : "idle";\n  const submitStatus: ContractLaunchProgress["submitStatus"] = submit.isPending\n    ? "pending"\n    : submit.isError\n      ? "error"\n      : "idle";\n  useEffect(() => {\n    onProgressChange({\n      preflightStatus,\n      prepareStatus,\n      preparedRunId: prepare.data?.run_id ?? null,\n      submitStatus,\n    });\n  }, [onProgressChange, preflightStatus, prepare.data?.run_id, prepareStatus, submitStatus]);\n',
)

visual = ROOT / "tests/ui/visual.spec.js"
text = visual.read_text()
marker = '\nasync function capture(page, filename) {'
if marker not in text:
    raise SystemExit("visual capture marker missing")
new_tests = r'''

test("experiment shell reflects real preflight and prepared-run progress", async ({ page }) => {
  await page.goto("/studio/contract_visual_001?user=alice&tab=basic");
  await expect(page.getByRole("button", { name: "阶段：运行前检查" })).toHaveAttribute("aria-current", "step");
  await expect(page.getByRole("button", { name: "阶段：运行前检查" })).toContainText("待执行");

  await page.getByRole("button", { name: "运行动态预检" }).click();
  await expect(page.getByText("Preflight OK", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "阶段：运行前检查" })).toContainText("检查通过");
  await expect(page.getByText("运行前检查已通过，可以固化 Prepared Run。", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "准备 Run" }).click();
  await expect(page.getByText("run_studio_prepared", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "阶段：运行" })).toHaveAttribute("aria-current", "step");
  await expect(page.getByRole("button", { name: "阶段：运行" })).toContainText("待确认提交");
  await expect(page.getByText(/Prepared Run run_studio_prepared 已形成/)).toBeVisible();
});

test("experiment shell invalidates visible preflight authority after a Contract edit", async ({ page }) => {
  await page.goto("/studio/contract_visual_001?user=alice&tab=basic");
  await page.getByRole("button", { name: "运行动态预检" }).click();
  await expect(page.getByRole("button", { name: "阶段：运行前检查" })).toContainText("检查通过");

  await page.getByRole("textbox", { name: "工作目录", exact: true }).fill("/public/home/alice/changed-after-preflight");
  await expect(page.getByRole("button", { name: "阶段：配置" })).toHaveAttribute("aria-current", "step");
  await expect(page.getByRole("button", { name: "阶段：运行前检查" })).toContainText("配置已变更，需重新检查");
  await expect(page.getByText(/旧运行前检查不会被视为仍然有效/)).toBeVisible();
});
'''
visual.write_text(text.replace(marker, new_tests + marker, 1))

print("A7 experiment progress migration applied")
