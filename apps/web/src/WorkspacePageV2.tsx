import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Blocks,
  CheckCircle2,
  Clock3,
  FileStack,
  FlaskConical,
  FolderOpen,
  HardDrive,
  Plus,
  RefreshCw,
  Server,
  ShieldCheck,
} from "lucide-react";
import { ApiRequestError } from "./api";
import { FactState, formatTimestamp, QueryBoundary } from "./components";
import { useCapabilities, useLatestEntitlement, useLatestPlatform, useRuns, useStorageUsage } from "./query";
import { formatStorageBytes } from "./resource-summary";
import { RunTable } from "./RunTable";
import { runStateLabel } from "./run-status";
import type { RunSummary } from "./types";
import type { LocationState } from "./url";
import { withSearch } from "./url";

interface WorkspacePageV2Props {
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
}

type Tone = "neutral" | "success" | "warning" | "danger" | "info";

function StatusChip({ label, tone = "neutral" }: { label: string; tone?: Tone }) {
  const symbol = tone === "success" ? "✓" : tone === "warning" ? "!" : tone === "danger" ? "×" : tone === "info" ? "↻" : "○";
  return <span className="v2-status-chip" data-tone={tone}>{symbol} {label}</span>;
}

function snapshotMissing(error: Error | null): boolean {
  return error instanceof ApiRequestError && error.status === 404;
}

function currentRun(items: RunSummary[]): RunSummary | null {
  const needsAction = items.find((run) => ["FAILED", "SUBMIT_FAILED", "COLLECTION_FAILED", "ORPHANED"].includes(run.state));
  if (needsAction) return needsAction;
  const active = items.find((run) => ["PENDING", "SUBMITTED", "RUNNING", "COMPLETING"].includes(run.state));
  return active ?? items[0] ?? null;
}

function runAction(run: RunSummary): { label: string; tab: string; detail: string; tone: Tone } {
  if (["FAILED", "SUBMIT_FAILED", "COLLECTION_FAILED", "ORPHANED"].includes(run.state)) {
    return { label: "查看原因", tab: "diagnosis", detail: "这次运行需要处理。先查看诊断与运行依据。", tone: "danger" };
  }
  if (["RUNNING", "COMPLETING"].includes(run.state)) {
    return { label: "查看运行", tab: "logs", detail: "运行正在进行，查看最新状态与增量日志。", tone: "info" };
  }
  if (["PENDING", "SUBMITTED"].includes(run.state)) {
    return { label: "查看排队", tab: "overview", detail: "作业已提交，当前由 Slurm 调度。", tone: "warning" };
  }
  if (run.state === "SUCCEEDED") {
    return { label: "查看结果", tab: "results", detail: "计算已经结束，可以检查输出和运行证据。", tone: "success" };
  }
  return { label: "查看详情", tab: "overview", detail: "继续检查这次运行的状态和上下文。", tone: "neutral" };
}

export function WorkspacePageV2({ user, location, navigate }: WorkspacePageV2Props) {
  const queryClient = useQueryClient();
  const [newExperimentOpen, setNewExperimentOpen] = useState(false);
  const runs = useRuns(user, undefined, undefined, "6");
  const capabilities = useCapabilities(user);
  const platform = useLatestPlatform(user);
  const entitlement = useLatestEntitlement(user);
  const storage = useStorageUsage(user);
  const items = runs.data?.items ?? [];
  const focusRun = currentRun(items);
  const focusAction = focusRun ? runAction(focusRun) : null;
  const active = items.filter((run) => ["PENDING", "SUBMITTED", "RUNNING", "COMPLETING"].includes(run.state));
  const failed = items.filter((run) => ["FAILED", "SUBMIT_FAILED", "COLLECTION_FAILED", "ORPHANED"].includes(run.state));
  const platformMissing = snapshotMissing(platform.error);
  const entitlementMissing = snapshotMissing(entitlement.error);
  const storagePct = storage.data?.total_bytes
    ? Math.round((storage.data.used_bytes / storage.data.total_bytes) * 100)
    : null;

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["runs", user] });
    void queryClient.invalidateQueries({ queryKey: ["capabilities", user] });
    void queryClient.invalidateQueries({ queryKey: ["platform-snapshot", user] });
    void queryClient.invalidateQueries({ queryKey: ["entitlement", user] });
    void queryClient.invalidateQueries({ queryKey: ["storage-usage", user] });
  };

  return (
    <div className="workbench-v2">
      <header className="workbench-v2-header">
        <div>
          <h1>工作台</h1>
          <p>准备、继续或检查你的科研计算。</p>
        </div>
        <div className="workbench-v2-header-actions">
          <button className="button secondary" type="button" onClick={refresh} disabled={runs.isFetching || capabilities.isFetching}>
            <RefreshCw aria-hidden="true" size={15} />
            {runs.isFetching || capabilities.isFetching ? "刷新中" : "刷新"}
          </button>
          <div className="workbench-new-experiment">
            <button className="button primary" type="button" aria-expanded={newExperimentOpen} onClick={() => setNewExperimentOpen((open) => !open)}>
              <Plus aria-hidden="true" size={16} /> 新建运行
            </button>
            {newExperimentOpen ? (
              <div className="workbench-new-menu v2-surface" role="menu">
                <button type="button" role="menuitem" onClick={() => navigate(`/market?user=${encodeURIComponent(user)}`)}>
                  <Blocks aria-hidden="true" />
                  <span><strong>从可复用方案开始</strong><small>推荐第一次使用时选择</small></span>
                </button>
                <button type="button" role="menuitem" onClick={() => navigate(`/studio/new?user=${encodeURIComponent(user)}`)}>
                  <FlaskConical aria-hidden="true" />
                  <span><strong>从空白配置开始</strong><small>直接进入高级配置</small></span>
                </button>
              </div>
            ) : null}
          </div>
        </div>
      </header>

      <div className="workbench-v2-grid">
        <section className="workbench-v2-focus" aria-labelledby="continue-work-heading">
          <div className="workbench-card-heading">
            <div>
              <p className="workbench-kicker">继续你的工作</p>
              <h2 id="continue-work-heading">当前工作</h2>
            </div>
            {focusRun && focusAction ? <StatusChip label={runStateLabel(focusRun.state)} tone={focusAction.tone} /> : <StatusChip label="尚未开始" />}
          </div>

          {focusRun && focusAction ? (
            <>
              <div className="workbench-current-run">
                <strong title={focusRun.job_name ?? focusRun.run_id}>{focusRun.job_name ?? "未命名运行"}</strong>
                <p className="mono">{focusRun.run_id}</p>
                <div className="workbench-readiness">
                  <StatusChip label={focusRun.job_id ? `Job ${focusRun.job_id}` : "尚未获得 Job ID"} tone={focusRun.job_id ? "success" : "neutral"} />
                  <StatusChip label={focusRun.contract_id ? "配置已持久化" : "配置来源未知"} tone={focusRun.contract_id ? "success" : "warning"} />
                  <StatusChip
                    label={focusRun.collection_state ? `运行证据：${focusRun.collection_state}` : "运行证据未确认"}
                    tone={focusRun.collection_state === "succeeded" ? "success" : "neutral"}
                  />
                </div>
              </div>
              <div className="workbench-next-action">
                <div><strong>下一步</strong><span>{focusAction.detail}</span></div>
                <button
                  className="button primary"
                  type="button"
                  onClick={() => navigate(withSearch(`/runs/${focusRun.run_id}`, location.search, { tab: focusAction.tab, object: null }))}
                >
                  {focusAction.label} <ArrowRight aria-hidden="true" size={15} />
                </button>
              </div>
            </>
          ) : (
            <div className="workbench-current-run">
              <strong>开始你的第一次科研计算</strong>
              <p>先准备文件和可复用方案。107Pilot 会在运行前检查配置与平台事实。</p>
              <div className="workbench-next-action">
                <div><strong>推荐</strong><span>从方案库采用一个经过审核的起点。</span></div>
                <button className="button primary" type="button" onClick={() => navigate(`/market?user=${encodeURIComponent(user)}`)}>
                  从可复用方案开始 <ArrowRight aria-hidden="true" size={15} />
                </button>
              </div>
            </div>
          )}
        </section>

        <aside className="workbench-v2-actions" aria-labelledby="attention-heading">
          <div className="workbench-card-heading">
            <div><p className="workbench-kicker">需要行动</p><h2 id="attention-heading">当前状态</h2></div>
          </div>
          <div className="workbench-action-list">
            {failed.length ? (
              <button className="workbench-action-item" data-tone="danger" type="button" onClick={() => navigate(`/runs/${failed[0]!.run_id}?user=${encodeURIComponent(user)}&tab=diagnosis`)}>
                <AlertTriangle aria-hidden="true" />
                <span><strong>{failed.length} 次运行需要处理</strong><small>直接打开最近一次失败的诊断</small></span>
                <span className="workbench-action-count">{failed.length}</span>
              </button>
            ) : (
              <div className="workbench-action-item">
                <CheckCircle2 aria-hidden="true" />
                <span><strong>没有待处理的失败运行</strong><small>近期运行没有需要立即处理的问题</small></span>
              </div>
            )}
            {active.length ? (
              <button className="workbench-action-item" data-tone="warning" type="button" onClick={() => navigate(`/runs/${active[0]!.run_id}?user=${encodeURIComponent(user)}`)}>
                <Clock3 aria-hidden="true" />
                <span><strong>{active.length} 次运行正在进行</strong><small>包括排队、运行或终态整理</small></span>
                <span className="workbench-action-count">{active.length}</span>
              </button>
            ) : null}
            {!platformMissing && platform.data?.freshness && platform.data.freshness !== "fresh" ? (
              <button className="workbench-action-item" data-tone="warning" type="button" onClick={() => navigate(`/cluster?user=${encodeURIComponent(user)}`)}>
                <Server aria-hidden="true" />
                <span><strong>平台观测不是最新状态</strong><small>打开计算资源查看 freshness 与来源</small></span>
                <ArrowRight aria-hidden="true" size={14} />
              </button>
            ) : null}
          </div>
        </aside>

        <section className="workbench-v2-prep-card" aria-labelledby="asset-prep-heading">
          <FolderOpen aria-hidden="true" />
          <div><h3 id="asset-prep-heading">文件与资产</h3><p>上传、整理并选择运行需要的数据、代码和模型。</p></div>
          <div className="workbench-prep-facts">
            <div><span>个人存储</span><strong>{storage.data ? formatStorageBytes(storage.data.used_bytes) : "—"}</strong></div>
            <div><span>使用率</span><strong>{storagePct === null ? "总量未知" : `${storagePct}%`}</strong></div>
          </div>
          <button className="workbench-prep-link" type="button" onClick={() => navigate(`/files?user=${encodeURIComponent(user)}`)}>打开文件 <ArrowRight aria-hidden="true" size={14} /></button>
        </section>

        <section className="workbench-v2-prep-card" aria-labelledby="resource-prep-heading">
          <Server aria-hidden="true" />
          <div><h3 id="resource-prep-heading">计算资源</h3><p>先看你被允许使用什么，再看最近观测到的平台负载。</p></div>
          <div className="workbench-prep-facts">
            <div><span>默认账户</span><strong>{entitlement.data?.default_account ?? "未确认"}</strong></div>
            <div><span>可见分区</span><strong>{capabilities.data?.partitions.length ?? "—"}</strong></div>
            <div><span>平台状态</span><strong>{platformMissing ? "未观测" : platform.data?.freshness ?? "—"}</strong></div>
          </div>
          <button className="workbench-prep-link" type="button" onClick={() => navigate(`/cluster?user=${encodeURIComponent(user)}`)}>查看计算资源 <ArrowRight aria-hidden="true" size={14} /></button>
        </section>

        <section className="workbench-v2-prep-card" aria-labelledby="scheme-prep-heading">
          <Blocks aria-hidden="true" />
          <div><h3 id="scheme-prep-heading">可复用方案</h3><p>从经过审核的模板和成功运行中选择起点，再按当前研究上下文调整。</p></div>
          <div className="workbench-prep-facts">
            <div><span>推荐路径</span><strong>方案 → 配置 → 检查</strong></div>
            <div><span>高级入口</span><strong>空白配置</strong></div>
          </div>
          <button className="workbench-prep-link" type="button" onClick={() => navigate(`/market?user=${encodeURIComponent(user)}`)}>浏览方案库 <ArrowRight aria-hidden="true" size={14} /></button>
        </section>
      </div>

      <section className="workbench-history" aria-labelledby="recent-runs-heading">
        <div className="workbench-history-heading">
          <div><h2 id="recent-runs-heading">最近运行</h2><p className="v2-section-detail">按时间查看最近的 Run；打开后进入其持久化状态、日志与 Evidence。</p></div>
          <button className="workbench-prep-link" type="button" onClick={() => navigate(withSearch("/runs", location.search, {}))}>查看全部 <ArrowRight aria-hidden="true" size={14} /></button>
        </div>
        <QueryBoundary
          pending={runs.isPending}
          error={runs.error}
          empty={items.length === 0}
          emptyTitle="还没有运行记录"
          emptyDetail="从方案库或空白配置开始准备第一个运行。"
        >
          <RunTable runs={items} onSelect={(runId) => navigate(`/runs/${runId}?user=${encodeURIComponent(user)}`)} />
        </QueryBoundary>
      </section>

      <div className="workbench-secondary-details" aria-label="平台辅助事实">
        <section aria-labelledby="platform-facts-heading">
          <div className="workbench-card-heading">
            <div><p className="workbench-kicker">平台事实</p><h2 id="platform-facts-heading">最近平台观测</h2></div>
            <FactState status={platform.data?.freshness} />
          </div>
          <QueryBoundary
            pending={platform.isPending}
            error={platformMissing ? null : platform.error}
            empty={platformMissing}
            emptyTitle="尚无平台快照"
            emptyDetail="静态 capability 不会被冒充为动态资源事实。"
          >
            <dl className="fact-list">
              <div><dt>范围</dt><dd>{platform.data?.scope ?? "login_node"}</dd></div>
              <div><dt>来源</dt><dd>{platform.data?.source_type ?? capabilities.data?.source_authority ?? "—"}</dd></div>
              <div><dt>观测时间</dt><dd>{formatTimestamp(platform.data?.observed_at)}</dd></div>
              <div><dt>数据质量</dt><dd><FactState status={platform.data?.data_quality} /></dd></div>
            </dl>
          </QueryBoundary>
        </section>

        <section aria-labelledby="entitlement-heading">
          <div className="workbench-card-heading">
            <div><p className="workbench-kicker">我的权限</p><h2 id="entitlement-heading">Slurm 授权</h2></div>
            <FactState status={entitlement.data?.freshness} />
          </div>
          <QueryBoundary
            pending={entitlement.isPending}
            error={entitlementMissing ? null : entitlement.error}
            empty={entitlementMissing}
            emptyTitle="尚无授权快照"
            emptyDetail="授权未知时不能把平台能力等同于当前用户权限。"
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
    </div>
  );
}