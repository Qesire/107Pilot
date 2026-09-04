import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  Archive,
  ArrowRight,
  Blocks,
  Bot,
  Braces,
  Clock3,
  Cpu,
  Gauge,
  HardDrive,
  Search,
  Server,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  TerminalSquare,
  Users,
} from "lucide-react";
import { api, ApiRequestError } from "./api";
import type { TerminalCommandResult } from "./types";
import {
  FactState,
  formatTimestamp,
  QueryBoundary,
  RefreshButton,
  SectionHeading,
  StatusBadge,
} from "./components";
import { ConnectionPanel } from "./ConnectionStatus";
import { ExperimentShell } from "./ExperimentShell";
import { ResourceDashboard } from "./ResourceDashboard";
import { useCapabilities, useLatestEntitlement, useLatestPlatform, useRun, useRunPages, useRuns, useRunWorkspace } from "./query";
import { RunList } from "./RunList";
import { RunTable } from "./RunTable";
import { RunPicker } from "./RunPicker";
import { nativeRunCommands, RunEvidencePanel } from "./RunEvidencePanel";
import { clearRunFilters, loadRunFilters, saveRunFilters } from "./run-filters";
import { runStateLabel, runTone } from "./run-status";
import type { LocationState } from "./url";
import { withSearch } from "./url";

interface PageProps {
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
}

export function WorkspacePage({ user, location, navigate }: PageProps) {
  const queryClient = useQueryClient();
  const runs = useRuns(user, undefined, undefined, "6");
  const capabilities = useCapabilities(user);
  const platform = useLatestPlatform(user);
  const entitlement = useLatestEntitlement(user);
  const platformMissing = snapshotMissing(platform.error);
  const entitlementMissing = snapshotMissing(entitlement.error);
  const refresh = () => void queryClient.invalidateQueries();
  const items = runs.data?.items ?? [];
  const active = items.filter((run) => ["PENDING", "RUNNING", "COMPLETING"].includes(run.state));
  const failed = items.filter((run) => ["FAILED", "SUBMIT_FAILED", "COLLECTION_FAILED"].includes(run.state));

  return (
    <>
      <SectionHeading
        eyebrow="Projects / 工作台"
        title="把下一次提交建立在可验证事实之上"
        detail="这里聚合你的近期作业、平台快照和 Slurm 授权；所有数值都来自 live API。"
        action={<RefreshButton onClick={refresh} pending={runs.isFetching || capabilities.isFetching} />}
      />

      <section className="signal-strip" aria-label="工作台摘要">
        <article>
          <span className="signal-icon info"><Clock3 aria-hidden="true" /></span>
          <div><strong>{active.length}</strong><p>近期活动作业</p></div>
        </article>
        <article>
          <span className="signal-icon danger"><Gauge aria-hidden="true" /></span>
          <div><strong>{failed.length}</strong><p>需要处理</p></div>
        </article>
        <article>
          <span className="signal-icon success"><ShieldCheck aria-hidden="true" /></span>
          <div>
            <strong>{entitlement.data?.default_account ?? "—"}</strong>
            <p>DefaultAccount</p>
          </div>
        </article>
        <article>
          <span className="signal-icon neutral"><Server aria-hidden="true" /></span>
          <div>
            <strong>{capabilities.data?.partitions.length ?? "—"}</strong>
            <p>可见分区</p>
          </div>
        </article>
      </section>

      <ResourceDashboard user={user} />

      <div className="workspace-grid">
        <section className="panel span-2" aria-labelledby="recent-runs-heading">
          <div className="panel-heading">
            <div><p className="panel-kicker">Recent runs</p><h2 id="recent-runs-heading">最近作业</h2></div>
            <button className="text-link" type="button" onClick={() => navigate(withSearch("/runs", location.search, {}))}>
              查看全部 <ArrowRight aria-hidden="true" size={15} />
            </button>
          </div>
          <QueryBoundary
            pending={runs.isPending}
            error={runs.error}
            empty={items.length === 0}
            emptyTitle="还没有 Run"
            emptyDetail="从 Contract Studio 或模板采用流程创建第一份 Contract。"
          >
            <RunTable runs={items} onSelect={(runId) => navigate(`/runs/${runId}?user=${user}`)} />
          </QueryBoundary>
        </section>

        <aside className="panel action-panel" aria-labelledby="start-heading">
          <div className="panel-heading"><div><p className="panel-kicker">Start</p><h2 id="start-heading">开始工作</h2></div></div>
          <button className="action-row" type="button" onClick={() => navigate(`/studio/new?user=${user}`)}>
            <span className="action-icon"><Braces aria-hidden="true" /></span>
            <span><strong>新建 Contract</strong><small>从 canonical schema 开始</small></span>
            <ArrowRight aria-hidden="true" />
          </button>
          <button className="action-row" type="button" onClick={() => navigate(`/market?user=${user}`)}>
            <span className="action-icon"><Blocks aria-hidden="true" /></span>
            <span><strong>采用模板</strong><small>从审核过的 release 创建</small></span>
            <ArrowRight aria-hidden="true" />
          </button>
          <button className="action-row" type="button" onClick={() => navigate(`/cluster?user=${user}`)}>
            <span className="action-icon"><Server aria-hidden="true" /></span>
            <span><strong>检查集群</strong><small>查看 freshness 与授权</small></span>
            <ArrowRight aria-hidden="true" />
          </button>
        </aside>

        <section className="panel" aria-labelledby="platform-facts-heading">
          <div className="panel-heading">
            <div><p className="panel-kicker">Platform facts</p><h2 id="platform-facts-heading">平台事实</h2></div>
            <FactState status={platform.data?.freshness} />
          </div>
          <QueryBoundary
            pending={platform.isPending}
            error={platformMissing ? null : platform.error}
            empty={platformMissing}
            emptyTitle="尚无平台快照"
            emptyDetail="先运行平台采集；静态 capability 不会被冒充为动态事实。"
          >
            <dl className="fact-list">
              <div><dt>范围</dt><dd>{platform.data?.scope ?? "login_node"}</dd></div>
              <div><dt>来源</dt><dd>{platform.data?.source_type ?? capabilities.data?.source_authority ?? "—"}</dd></div>
              <div><dt>观测时间</dt><dd>{formatTimestamp(platform.data?.observed_at)}</dd></div>
              <div><dt>数据质量</dt><dd><FactState status={platform.data?.data_quality} /></dd></div>
            </dl>
          </QueryBoundary>
        </section>

        <section className="panel" aria-labelledby="entitlement-heading">
          <div className="panel-heading">
            <div><p className="panel-kicker">Entitlement</p><h2 id="entitlement-heading">我的 Slurm 授权</h2></div>
            <FactState status={entitlement.data?.freshness} />
          </div>
          <QueryBoundary
            pending={entitlement.isPending}
            error={entitlementMissing ? null : entitlement.error}
            empty={entitlementMissing}
            emptyTitle="尚无授权快照"
            emptyDetail="采集 Slurm association 后才会显示账户、分区和 QoS。"
          >
            <dl className="fact-list">
              <div><dt>默认账户</dt><dd className="mono">{entitlement.data?.default_account ?? "未声明"}</dd></div>
              <div><dt>Association</dt><dd>{entitlement.data?.associations?.length ?? 0} 条</dd></div>
              <div><dt>数据质量</dt><dd><FactState status={entitlement.data?.data_quality} /></dd></div>
              <div><dt>观测时间</dt><dd>{formatTimestamp(entitlement.data?.observed_at)}</dd></div>
            </dl>
          </QueryBoundary>
        </section>
      </div>
    </>
  );
}

export function RunsPage({ user, location, navigate }: PageProps) {
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
  const workspace = useRunWorkspace(user, runId);
  const run = selectedRun.data;
  const model = workspace.data;
  return (
    <QueryBoundary
      pending={selectedRun.isPending || workspace.isPending}
      error={selectedRun.error ?? workspace.error}
    >
      {run && model ? (
        <ExperimentShell
          user={user}
          location={location}
          navigate={navigate}
          context={{ kind: "run", run }}
        >
          <section className="experiment-run-workspace" aria-labelledby="run-detail-heading">
            <header className="experiment-run-heading">
              <div>
                <h2 id="run-detail-heading">运行详情</h2>
                <p>首屏只读取运行对象与聚合工作区模型；日志、结果、诊断、自动归档和证据对象按视图加载。</p>
              </div>
              {model.evidence_summary.capsule_available ? (
                <button
                  className="run-capsule-link"
                  type="button"
                  onClick={() => navigate(withSearch(location.pathname, location.search, { tab: "capsule", object: null }))}
                >
                  <Archive aria-hidden="true" size={15} /> 查看自动归档
                </button>
              ) : null}
            </header>

            <RunEvidencePanel
              user={user}
              run={run}
              workspace={model}
              location={location}
              navigate={navigate}
            />
          </section>
        </ExperimentShell>
      ) : null}
    </QueryBoundary>
  );
}

export function TerminalCollaborationPage({ user, location, navigate, terminalDeepLink }: PageProps & { terminalDeepLink: string | null }) {
  const runId = location.search.get("run");
  const run = useRun(user, runId);
  const runs = useRuns(user);
  const [copied, setCopied] = useState<string | null>(null);
  const [terminalResult, setTerminalResult] = useState<TerminalCommandResult | null>(null);
  const terminalCommand = useMutation({
    mutationFn: (command: "identity" | "cluster" | "my_jobs" | "run_status") =>
      api.terminalCommand(user, command, command === "run_status" ? run.data?.run_id : null),
    onSuccess: setTerminalResult,
  });
  const commands = run.data?.job_id
    ? nativeRunCommands(run.data.job_id, run.data.workdir ?? null)
    : [];
  return (
    <>
      <SectionHeading
        eyebrow="Terminal / safe collaboration"
        title="与平台终端协同，并执行受审计的只读诊断"
        detail="模拟器可运行固定的 Slurm 诊断命令；107Pilot 不提供生产 PTY，也不会向浏览器下发长期凭据。"
      />
      <div className="terminal-collaboration-grid">
        <section className="panel">
          <div className="panel-heading"><div><p className="panel-kicker">Selected Run</p><h2>对象绑定</h2></div></div>
          <QueryBoundary
            pending={Boolean(runId) && run.isPending}
            error={run.error}
            empty={!runId}
            emptyTitle="选择一个 Run 进入终端协同"
            emptyDetail={
              <>
                <RunPicker
                  runs={runs.data?.items ?? []}
                  onSelect={(selectedId) => navigate(withSearch("/terminal", location.search, { run: selectedId }))}
                />
                <p className="terminal-safety-note">
                  选择 Run 后，Run 状态诊断将绑定其 Job ID；浏览器不会执行任意 shell。
                </p>
              </>
            }
          >
            {run.data ? <>
              <dl className="fact-list">
                <div><dt>Run</dt><dd className="mono wrap-anywhere">{run.data.run_id}</dd></div>
                <div><dt>Job</dt><dd className="mono">{run.data.job_id ?? "尚未提交"}</dd></div>
                <div><dt>Workdir</dt><dd className="mono wrap-anywhere">{run.data.workdir ?? "未记录"}</dd></div>
                <div><dt>State</dt><dd>{runStateLabel(run.data.state)}</dd></div>
              </dl>
              <button className="button secondary" type="button" onClick={() => navigate(`/runs/${encodeURIComponent(run.data?.run_id ?? "")}?user=${encodeURIComponent(user)}&tab=overview`)}>返回 Run</button>
            </> : null}
          </QueryBoundary>
        </section>
        <section className="panel native-commands terminal-command-panel">
          <header><h2>可复制命令</h2><p>复制不会执行命令；`Cancel` 仍需在目标终端中由已授权用户主动运行。</p></header>
          {commands.length ? commands.map((item) => <div key={item.label}><span><strong>{item.label}</strong>{item.dangerous ? <small>会修改作业状态</small> : null}</span><code>{item.command}</code><button type="button" onClick={() => void navigator.clipboard.writeText(item.command).then(() => setCopied(item.label))}>{copied === item.label ? "已复制" : "复制"}</button></div>) : <p className="no-findings">当前 Run 尚无 Job ID，因此不会生成命令。</p>}
        </section>
        <section className="panel terminal-command-panel">
          <div className="panel-heading"><div><p className="panel-kicker">Audited simulator diagnostics</p><h2>受限 Shell</h2></div></div>
          <p className="side-detail">命令由服务端固定、以当前身份在命令网关执行，并写入网关审计日志；不接受任意命令或脚本。</p>
          <div className="agent-action-row">
            <button className="button secondary" type="button" disabled={terminalCommand.isPending} onClick={() => terminalCommand.mutate("identity")}>身份</button>
            <button className="button secondary" type="button" disabled={terminalCommand.isPending} onClick={() => terminalCommand.mutate("cluster")}>集群</button>
            <button className="button secondary" type="button" disabled={terminalCommand.isPending} onClick={() => terminalCommand.mutate("my_jobs")}>我的作业</button>
            <button className="button primary" type="button" disabled={terminalCommand.isPending || !run.data?.job_id} onClick={() => terminalCommand.mutate("run_status")}>所选 Run 状态</button>
          </div>
          {terminalCommand.isError ? <p className="limitation" role="alert">诊断命令失败：{terminalCommand.error.message}</p> : null}
          {terminalResult ? <div className="terminal-result"><p className="mono">$ {terminalResult.argv.join(" ")} · exit {terminalResult.returncode}</p><pre><code>{terminalResult.stdout || terminalResult.stderr || "(no output)"}</code></pre></div> : null}
        </section>
        <section className="panel">
          <div className="panel-heading"><div><p className="panel-kicker">Platform terminal</p><h2>配置化 deep link</h2></div></div>
          {terminalDeepLink ? <a className="button primary" href={terminalDeepLink} target="_blank" rel="noreferrer">打开平台终端</a> : <p className="limitation">部署未配置生产 PTY URL；上方的受限 Shell 仍可用于模拟器诊断。</p>}
        </section>
      </div>
    </>
  );
}

export function ClusterPage({ user }: PageProps) {
  const capabilities = useCapabilities(user);
  const platform = useLatestPlatform(user);
  const entitlement = useLatestEntitlement(user);
  const platformMissing = snapshotMissing(platform.error);
  const entitlementMissing = snapshotMissing(entitlement.error);
  return (
    <>
      <SectionHeading
        eyebrow="Cluster / 集群"
        title="资源能力、动态事实与个人授权"
        detail="静态 capability 不是实时空闲量；动态快照带有来源、时间和 freshness。"
      />
      <QueryBoundary pending={capabilities.isPending} error={capabilities.error}>
        <section className="cluster-summary">
          <div><Cpu aria-hidden="true" /><span><strong>{capabilities.data?.partitions.length ?? 0}</strong><small>Partitions</small></span></div>
          <div><Gauge aria-hidden="true" /><span><strong>{capabilities.data?.qos.length ?? 0}</strong><small>QoS profiles</small></span></div>
          <div><HardDrive aria-hidden="true" /><span><strong>{platform.data?.data_quality ?? "—"}</strong><small>Snapshot quality</small></span></div>
          <div><Users aria-hidden="true" /><span><strong>{entitlement.data?.default_account ?? "—"}</strong><small>DefaultAccount</small></span></div>
        </section>

        <div className="cluster-grid">
          <section className="panel">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Real platform relay</p>
                <h2>真实 107 连接</h2>
              </div>
            </div>
            <ConnectionPanel user={user} />
          </section>
          <section className="panel span-2">
            <div className="panel-heading"><div><p className="panel-kicker">Partitions</p><h2>可调度分区</h2></div><FactState status={platform.data?.freshness} /></div>
            <div className="partition-grid">
              {(capabilities.data?.partitions ?? []).map((partition) => (
                <article className="partition-row" key={partition.name}>
                  <div><Server aria-hidden="true" /><span><strong>{partition.name}</strong><small>{partition.nodes ?? "节点由动态事实提供"}</small></span></div>
                  <dl>
                    <div><dt>Nodes</dt><dd>{partition.total_nodes ?? "—"}</dd></div>
                    <div><dt>GPU</dt><dd>{partition.gpu_types?.join(", ") || "CPU"}</dd></div>
                    <div><dt>QoS</dt><dd>{partition.allow_qos?.length ?? 0}</dd></div>
                  </dl>
                </article>
              ))}
            </div>
          </section>
          <aside className="panel">
            <div className="panel-heading"><div><p className="panel-kicker">Authority</p><h2>事实边界</h2></div></div>
            <dl className="fact-list">
              <div><dt>Profile</dt><dd className="mono wrap-anywhere">{capabilities.data?.profile_id ?? "—"}</dd></div>
              <div><dt>来源</dt><dd>{capabilities.data?.source_authority ?? "—"}</dd></div>
              <div><dt>Snapshot</dt><dd><FactState status={platform.data?.freshness} /></dd></div>
              <div><dt>Entitlement</dt><dd><FactState status={entitlement.data?.freshness} /></dd></div>
            </dl>
            {platformMissing ? <p className="limitation">尚无动态平台快照；当前只显示 capability 声明。</p> : null}
            {platform.isError && !platformMissing ? <p className="limitation" role="alert">平台快照读取失败：{platform.error.message}</p> : null}
            {entitlementMissing ? <p className="limitation">尚无个人授权快照；不会推断账户或 QoS。</p> : null}
            {entitlement.isError && !entitlementMissing ? <p className="limitation" role="alert">授权快照读取失败：{entitlement.error.message}</p> : null}
            {(capabilities.data?.limitations ?? []).map((item) => <p className="limitation" key={item}>{item}</p>)}
          </aside>
        </div>
      </QueryBoundary>
    </>
  );
}

export function PlannedPage({ location, navigate, user }: PageProps) {
  const isMarket = location.pathname.startsWith("/market") || location.pathname.startsWith("/templates/");
  const isAgent = location.pathname === "/agent";
  const isTerminal = location.pathname === "/terminal";
  const title = isMarket ? "模板市场" : isAgent ? "Agent" : isTerminal ? "终端" : "Contract Studio";
  const icon = isMarket ? <Blocks aria-hidden="true" /> : isAgent ? <Bot aria-hidden="true" /> : <TerminalSquare aria-hidden="true" />;
  const detail = isMarket
    ? "后端市场已完成；交互界面将在下一切片接入 live release、diff 与 adoption。"
    : isAgent
      ? "Agent 将基于 Run、Evidence 和 lineage 提供证据约束的解释与修复建议。"
      : isTerminal
        ? "终端会在权限、审计和执行边界明确后接入，不会在当前切片模拟命令执行。"
        : "五种投影将共享同一 canonical Contract，不会用表单状态覆盖未知字段。";
  return (
    <div className="planned-page">
      <span className="planned-icon">{icon}</span>
      <p className="eyebrow">Phase 3D / Next slice</p>
      <h1>{title}</h1>
      <p>{detail}</p>
      <div className="planned-facts">
        <span><Sparkles aria-hidden="true" /> canonical state</span>
        <span><ShieldCheck aria-hidden="true" /> server validation</span>
        <span><TerminalSquare aria-hidden="true" /> equivalent CLI</span>
      </div>
      <button className="button primary" type="button" onClick={() => navigate(`/projects?user=${user}`)}>返回工作台</button>
    </div>
  );
}

export function NotFoundPage({ navigate, user }: PageProps) {
  return (
    <div className="planned-page">
      <span className="planned-icon"><Search aria-hidden="true" /></span>
      <p className="eyebrow">404</p>
      <h1>没有这个工作区</h1>
      <p>URL 未匹配 107Pilot 当前公开的信息架构。</p>
      <button className="button primary" type="button" onClick={() => navigate(`/projects?user=${user}`)}>返回工作台</button>
    </div>
  );
}

export function snapshotMissing(error: unknown): boolean {
  return error instanceof ApiRequestError && error.status === 404;
}
