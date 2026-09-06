import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Blocks,
  CheckCircle2,
  Clock3,
  FolderOpen,
  Plus,
  RefreshCw,
  Server,
} from "lucide-react";
import { ApiRequestError } from "./api";
import { FactState, formatTimestamp, QueryBoundary } from "./components";
import {
  useCapabilities,
  useLatestEntitlement,
  useLatestPlatform,
  useRuns,
  useStorageUsage,
} from "./query";
import { formatStorageBytes } from "./resource-summary";
import { RunTable } from "./RunTable";
import { runStateLabel } from "./run-status";
import type { RunSummary } from "./types";
import type { LocationState } from "./url";
import { withSearch } from "./url";
import { workareaApi, type LaunchRecord } from "./workarea-api";

interface WorkspacePageV2Props {
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
}

type Tone = "neutral" | "success" | "warning" | "danger" | "info";

const ACTIVE_STATES = new Set(["PENDING", "SUBMITTED", "RUNNING", "COMPLETING"]);
const FAILED_STATES = new Set(["FAILED", "SUBMIT_FAILED", "COLLECTION_FAILED", "ORPHANED"]);
const CURRENT_WORKAREA_STORAGE_PREFIX = "107pilot-current-workarea:";

function StatusChip({ label, tone = "neutral" }: { label: string; tone?: Tone }) {
  const symbol = tone === "success"
    ? "✓"
    : tone === "warning"
      ? "!"
      : tone === "danger"
        ? "×"
        : tone === "info"
          ? "↻"
          : "○";
  return <span className="v2-status-chip" data-tone={tone}>{symbol} {label}</span>;
}

function snapshotMissing(error: Error | null): boolean {
  return error instanceof ApiRequestError && error.status === 404;
}

function currentRun(items: RunSummary[]): RunSummary | null {
  const needsAction = items.find((run) => FAILED_STATES.has(run.state));
  if (needsAction) return needsAction;
  const active = items.find((run) => ACTIVE_STATES.has(run.state));
  return active ?? items[0] ?? null;
}

function runAction(run: RunSummary): { label: string; tab: string; detail: string; tone: Tone } {
  if (FAILED_STATES.has(run.state)) {
    return {
      label: "查看原因",
      tab: "diagnosis",
      detail: "这次运行需要处理。先查看诊断与运行依据。",
      tone: "danger",
    };
  }
  if (["RUNNING", "COMPLETING"].includes(run.state)) {
    return {
      label: "查看运行",
      tab: "logs",
      detail: "运行正在进行，查看最新状态与增量日志。",
      tone: "info",
    };
  }
  if (["PENDING", "SUBMITTED"].includes(run.state)) {
    return {
      label: "查看排队",
      tab: "overview",
      detail: "作业已提交，当前由 Slurm 调度。",
      tone: "warning",
    };
  }
  if (run.state === "SUCCEEDED") {
    return {
      label: "查看结果",
      tab: "results",
      detail: "计算已经结束，可以检查输出和运行证据。",
      tone: "success",
    };
  }
  return {
    label: "查看详情",
    tab: "overview",
    detail: "继续检查这次运行的状态和上下文。",
    tone: "neutral",
  };
}

function storageKey(user: string): string {
  return `${CURRENT_WORKAREA_STORAGE_PREFIX}${user}`;
}

function readCurrentWorkArea(user: string): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(storageKey(user)) ?? "";
  } catch {
    return "";
  }
}

function persistCurrentWorkArea(user: string, workareaId: string): void {
  if (typeof window === "undefined") return;
  try {
    if (workareaId) {
      window.localStorage.setItem(storageKey(user), workareaId);
    } else {
      window.localStorage.removeItem(storageKey(user));
    }
  } catch {
    // Local preference persistence must never become research-domain authority.
  }
}

function launchStatus(launch: LaunchRecord): { label: string; tone: Tone } {
  if (launch.submit_error) return { label: "提交失败", tone: "danger" };
  if (launch.submitted_at) return { label: "已提交", tone: "success" };
  return { label: "已 Commit", tone: "info" };
}

export function WorkspacePageV2({ user, location, navigate }: WorkspacePageV2Props) {
  const queryClient = useQueryClient();
  const [selectedWorkareaId, setSelectedWorkareaId] = useState(() => readCurrentWorkArea(user));

  useEffect(() => {
    setSelectedWorkareaId(readCurrentWorkArea(user));
  }, [user]);

  const workareas = useQuery({
    queryKey: ["workareas", user],
    queryFn: ({ signal }) => workareaApi.list(user, signal),
    retry: false,
  });
  const selectedSummary = workareas.data?.items.find(
    (area) => area.workarea_id === selectedWorkareaId,
  ) ?? null;
  const staleSelection = Boolean(
    selectedWorkareaId && workareas.isSuccess && selectedSummary === null,
  );
  const currentWorkareaId = selectedSummary?.workarea_id ?? "";

  const currentArea = useQuery({
    queryKey: ["workarea", user, currentWorkareaId],
    queryFn: ({ signal }) => workareaApi.get(user, currentWorkareaId, signal),
    enabled: Boolean(currentWorkareaId),
    retry: false,
  });
  const launches = useQuery({
    queryKey: ["workarea-launches", user, currentWorkareaId],
    queryFn: ({ signal }) => workareaApi.launches(user, currentWorkareaId, signal),
    enabled: Boolean(currentWorkareaId),
    retry: false,
  });

  // The API list is global; the WorkArea binding graph is the scope authority.
  // Load a broad recent window, then intersect by explicit/inherited WorkArea edges.
  const runs = useRuns(user, undefined, undefined, "100");
  const capabilities = useCapabilities(user);
  const platform = useLatestPlatform(user);
  const entitlement = useLatestEntitlement(user);
  const storage = useStorageUsage(user);

  const allItems = runs.data?.items ?? [];
  const boundRunIds = new Set(
    currentArea.data?.bindings.runs.map((binding) => binding.target_ref) ?? [],
  );
  const items = currentArea.data
    ? allItems.filter((run) => boundRunIds.has(run.run_id))
    : [];
  const focusRun = currentRun(items);
  const focusAction = focusRun ? runAction(focusRun) : null;
  const active = items.filter((run) => ACTIVE_STATES.has(run.state));
  const failed = items.filter((run) => FAILED_STATES.has(run.state));
  const platformMissing = snapshotMissing(platform.error);
  const entitlementMissing = snapshotMissing(entitlement.error);
  const storagePct = storage.data?.total_bytes
    ? Math.round((storage.data.used_bytes / storage.data.total_bytes) * 100)
    : null;

  const selectWorkArea = (workareaId: string) => {
    persistCurrentWorkArea(user, workareaId);
    setSelectedWorkareaId(workareaId);
  };

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["runs", user] });
    void queryClient.invalidateQueries({ queryKey: ["workareas", user] });
    if (currentWorkareaId) {
      void queryClient.invalidateQueries({ queryKey: ["workarea", user, currentWorkareaId] });
      void queryClient.invalidateQueries({
        queryKey: ["workarea-launches", user, currentWorkareaId],
      });
    }
    void queryClient.invalidateQueries({ queryKey: ["capabilities", user] });
    void queryClient.invalidateQueries({ queryKey: ["platform-snapshot", user] });
    void queryClient.invalidateQueries({ queryKey: ["entitlement", user] });
    void queryClient.invalidateQueries({ queryKey: ["storage-usage", user] });
  };

  const openCurrentWorkArea = () => {
    if (!currentWorkareaId) return;
    navigate(`/workareas/${encodeURIComponent(currentWorkareaId)}?user=${encodeURIComponent(user)}`);
  };

  const startLaunch = () => {
    if (!currentWorkareaId) return;
    navigate(
      `/workareas/${encodeURIComponent(currentWorkareaId)}/launch/new?user=${encodeURIComponent(user)}`,
    );
  };

  return (
    <div className="workbench-v2">
      <header className="workbench-v2-header">
        <div>
          <h1>工作台</h1>
          <p>在明确选择的研究区中准备、发起并检查科研计算。</p>
        </div>
        <div className="workbench-v2-header-actions">
          <label className="workbench-workarea-switcher">
            <span>当前研究区</span>
            <select
              aria-label="当前研究区"
              value={currentWorkareaId}
              onChange={(event) => selectWorkArea(event.target.value)}
              disabled={workareas.isPending}
            >
              <option value="">
                {workareas.isPending
                  ? "读取研究区…"
                  : (workareas.data?.items.length ?? 0) === 0
                    ? "还没有研究区"
                    : "请选择研究区"}
              </option>
              {(workareas.data?.items ?? []).map((area) => (
                <option key={area.workarea_id} value={area.workarea_id}>{area.title}</option>
              ))}
            </select>
          </label>
          <button
            className="button secondary"
            type="button"
            onClick={refresh}
            disabled={runs.isFetching || capabilities.isFetching || workareas.isFetching}
          >
            <RefreshCw aria-hidden="true" size={15} />
            {runs.isFetching || capabilities.isFetching || workareas.isFetching ? "刷新中" : "刷新"}
          </button>
          <button
            className="button primary"
            type="button"
            disabled={!currentWorkareaId}
            onClick={startLaunch}
            title={currentWorkareaId ? "在当前研究区创建 LaunchCandidate" : "请先明确选择研究区"}
          >
            <Plus aria-hidden="true" size={16} /> 新建运行
          </button>
        </div>
      </header>

      {staleSelection ? (
        <div className="workbench-context-warning" role="alert">
          <AlertTriangle aria-hidden="true" size={16} />
          <span>
            已保存的当前研究区 <code>{selectedWorkareaId}</code> 不再可用。系统没有自动切换到其它研究区，请重新选择。
          </span>
        </div>
      ) : null}

      <div className="workbench-v2-grid">
        <section className="workbench-v2-focus" aria-labelledby="continue-work-heading">
          <div className="workbench-card-heading">
            <div>
              <p className="workbench-kicker">研究上下文</p>
              <h2 id="continue-work-heading">当前研究区</h2>
            </div>
            {currentArea.data ? <StatusChip label="已明确选择" tone="success" /> : <StatusChip label="未选择" />}
          </div>

          {currentArea.data ? (
            <>
              <div className="workbench-current-run">
                <strong title={currentArea.data.workarea_id}>{currentArea.data.title}</strong>
                <p>{currentArea.data.description || "这个研究区还没有说明。"}</p>
                <p className="mono">{currentArea.data.workarea_id}</p>
                <div className="workbench-readiness">
                  <StatusChip label={`资产 ${currentArea.data.bindings.assets.length}`} />
                  <StatusChip label={`Run ${currentArea.data.bindings.runs.length}`} />
                  <StatusChip label={`Launch ${launches.data?.items.length ?? "—"}`} />
                </div>
              </div>

              {focusRun && focusAction ? (
                <div className="workbench-next-action">
                  <div>
                    <strong>{focusRun.job_name ?? "当前运行"}</strong>
                    <span>
                      {runStateLabel(focusRun.state)} · {focusAction.detail}
                    </span>
                  </div>
                  <button
                    className="button primary"
                    type="button"
                    onClick={() => navigate(withSearch(
                      `/runs/${focusRun.run_id}`,
                      location.search,
                      { tab: focusAction.tab, object: null },
                    ))}
                  >
                    {focusAction.label} <ArrowRight aria-hidden="true" size={15} />
                  </button>
                </div>
              ) : (
                <div className="workbench-next-action">
                  <div>
                    <strong>下一步</strong>
                    <span>当前研究区还没有需要继续处理的已加载 Run。通过 Review 后显式 Commit 新运行。</span>
                  </div>
                  <button className="button primary" type="button" onClick={startLaunch}>
                    新建运行 <ArrowRight aria-hidden="true" size={15} />
                  </button>
                </div>
              )}

              <button className="workbench-prep-link" type="button" onClick={openCurrentWorkArea}>
                打开研究区 <ArrowRight aria-hidden="true" size={14} />
              </button>
            </>
          ) : currentWorkareaId && currentArea.isPending ? (
            <div className="query-state">
              <strong>正在读取当前研究区</strong>
              <span>等待 PostgreSQL 中的持久化研究上下文。</span>
            </div>
          ) : (
            <div className="workbench-current-run">
              <strong>先明确选择研究区</strong>
              <p>
                107Pilot 不会根据目录名、路径相似度或最近运行猜测研究上下文。选择或创建研究区后，Launch 和 Run 才会在该上下文中继续。
              </p>
              <div className="workbench-next-action">
                <div>
                  <strong>需要用户选择</strong>
                  <span>当前没有可用于 Launch 的 WorkArea。</span>
                </div>
                <button
                  className="button primary"
                  type="button"
                  onClick={() => navigate(`/workareas?user=${encodeURIComponent(user)}`)}
                >
                  {(workareas.data?.items.length ?? 0) === 0 ? "创建研究区" : "管理研究区"}
                  <ArrowRight aria-hidden="true" size={15} />
                </button>
              </div>
            </div>
          )}
        </section>

        <aside className="workbench-v2-actions" aria-labelledby="attention-heading">
          <div className="workbench-card-heading">
            <div>
              <p className="workbench-kicker">需要行动</p>
              <h2 id="attention-heading">当前研究区状态</h2>
            </div>
          </div>
          <div className="workbench-action-list">
            {!currentArea.data ? (
              <div className="workbench-action-item" data-tone="warning">
                <AlertTriangle aria-hidden="true" />
                <span>
                  <strong>研究区未确认</strong>
                  <small>选择研究区后才显示其运行状态。</small>
                </span>
              </div>
            ) : failed.length ? (
              <button
                className="workbench-action-item"
                data-tone="danger"
                type="button"
                onClick={() => navigate(
                  `/runs/${failed[0]!.run_id}?user=${encodeURIComponent(user)}&tab=diagnosis`,
                )}
              >
                <AlertTriangle aria-hidden="true" />
                <span>
                  <strong>{failed.length} 次运行需要处理</strong>
                  <small>直接打开当前研究区最近一次失败的诊断</small>
                </span>
                <span className="workbench-action-count">{failed.length}</span>
              </button>
            ) : (
              <div className="workbench-action-item">
                <CheckCircle2 aria-hidden="true" />
                <span>
                  <strong>没有已加载的失败运行</strong>
                  <small>当前研究区最近运行没有需要立即处理的问题</small>
                </span>
              </div>
            )}
            {currentArea.data && active.length ? (
              <button
                className="workbench-action-item"
                data-tone="warning"
                type="button"
                onClick={() => navigate(
                  `/runs/${active[0]!.run_id}?user=${encodeURIComponent(user)}`,
                )}
              >
                <Clock3 aria-hidden="true" />
                <span>
                  <strong>{active.length} 次运行正在进行</strong>
                  <small>包括排队、运行或终态整理</small>
                </span>
                <span className="workbench-action-count">{active.length}</span>
              </button>
            ) : null}
            {!platformMissing && platform.data?.freshness && platform.data.freshness !== "fresh" ? (
              <button
                className="workbench-action-item"
                data-tone="warning"
                type="button"
                onClick={() => navigate(`/cluster?user=${encodeURIComponent(user)}`)}
              >
                <Server aria-hidden="true" />
                <span>
                  <strong>平台观测不是最新状态</strong>
                  <small>打开计算资源查看 freshness 与来源</small>
                </span>
                <ArrowRight aria-hidden="true" size={14} />
              </button>
            ) : null}
          </div>
        </aside>

        <section className="workbench-v2-prep-card" aria-labelledby="asset-prep-heading">
          <FolderOpen aria-hidden="true" />
          <div>
            <h3 id="asset-prep-heading">文件与资产</h3>
            <p>上传、整理并选择运行需要的数据、代码和模型。</p>
          </div>
          <div className="workbench-prep-facts">
            <div>
              <span>个人存储</span>
              <strong>{storage.data ? formatStorageBytes(storage.data.used_bytes) : "—"}</strong>
            </div>
            <div>
              <span>使用率</span>
              <strong>{storagePct === null ? "总量未知" : `${storagePct}%`}</strong>
            </div>
          </div>
          <button
            className="workbench-prep-link"
            type="button"
            onClick={() => navigate(`/files?user=${encodeURIComponent(user)}`)}
          >
            打开文件 <ArrowRight aria-hidden="true" size={14} />
          </button>
        </section>

        <section className="workbench-v2-prep-card" aria-labelledby="resource-prep-heading">
          <Server aria-hidden="true" />
          <div>
            <h3 id="resource-prep-heading">计算资源</h3>
            <p>先看你被允许使用什么，再看最近观测到的平台负载。</p>
          </div>
          <div className="workbench-prep-facts">
            <div>
              <span>默认账户</span>
              <strong>{entitlement.data?.default_account ?? "未确认"}</strong>
            </div>
            <div>
              <span>可见分区</span>
              <strong>{capabilities.data?.partitions.length ?? "—"}</strong>
            </div>
            <div>
              <span>平台状态</span>
              <strong>{platformMissing ? "未观测" : platform.data?.freshness ?? "—"}</strong>
            </div>
          </div>
          <button
            className="workbench-prep-link"
            type="button"
            onClick={() => navigate(`/cluster?user=${encodeURIComponent(user)}`)}
          >
            查看计算资源 <ArrowRight aria-hidden="true" size={14} />
          </button>
        </section>

        <section className="workbench-v2-prep-card" aria-labelledby="scheme-prep-heading">
          <Blocks aria-hidden="true" />
          <div>
            <h3 id="scheme-prep-heading">可复用方案</h3>
            <p>方案用于准备 Contract；真正提交仍从当前 WorkArea 的 Launch Review 进入。</p>
          </div>
          <div className="workbench-prep-facts">
            <div>
              <span>推荐路径</span>
              <strong>方案 → Contract → Launch</strong>
            </div>
            <div>
              <span>提交边界</span>
              <strong>Review → Commit</strong>
            </div>
          </div>
          <button
            className="workbench-prep-link"
            type="button"
            onClick={() => navigate(`/market?user=${encodeURIComponent(user)}`)}
          >
            浏览方案库 <ArrowRight aria-hidden="true" size={14} />
          </button>
        </section>
      </div>

      <section className="workbench-history" aria-labelledby="recent-launches-heading">
        <div className="workbench-history-heading">
          <div>
            <h2 id="recent-launches-heading">最近 Launch</h2>
            <p className="v2-section-detail">只显示当前研究区中已经显式 Commit 的执行意图。</p>
          </div>
          {currentArea.data ? (
            <button className="workbench-prep-link" type="button" onClick={openCurrentWorkArea}>
              打开研究区 <ArrowRight aria-hidden="true" size={14} />
            </button>
          ) : null}
        </div>
        {!currentArea.data ? (
          <div className="query-state">
            <strong>尚未选择研究区</strong>
            <span>Launch 历史不会跨研究区混合展示。</span>
          </div>
        ) : (
          <QueryBoundary
            pending={launches.isPending}
            error={launches.error}
            empty={(launches.data?.items.length ?? 0) === 0}
            emptyTitle="当前研究区还没有 Launch"
            emptyDetail="新建运行会先生成 Candidate 与 Preflight，只有显式 Commit 后才出现在这里。"
          >
            <div className="workbench-action-list">
              {(launches.data?.items ?? []).slice(0, 6).map((launch) => {
                const status = launchStatus(launch);
                return (
                  <button
                    className="workbench-action-item"
                    type="button"
                    key={launch.launch_id}
                    onClick={() => navigate(
                      `/launches/${encodeURIComponent(launch.launch_id)}?user=${encodeURIComponent(user)}`,
                    )}
                  >
                    <Clock3 aria-hidden="true" />
                    <span>
                      <strong>{launch.run_ids[0] ?? launch.launch_id}</strong>
                      <small>
                        Contract {launch.contract_id} · {formatTimestamp(launch.committed_at)}
                      </small>
                    </span>
                    <StatusChip label={status.label} tone={status.tone} />
                  </button>
                );
              })}
            </div>
          </QueryBoundary>
        )}
      </section>

      <section className="workbench-history" aria-labelledby="recent-runs-heading">
        <div className="workbench-history-heading">
          <div>
            <h2 id="recent-runs-heading">当前研究区运行</h2>
            <p className="v2-section-detail">Run 只有在明确或继承绑定到当前 WorkArea 后才进入这里。</p>
          </div>
          {currentArea.data ? (
            <button className="workbench-prep-link" type="button" onClick={openCurrentWorkArea}>
              查看研究上下文 <ArrowRight aria-hidden="true" size={14} />
            </button>
          ) : null}
        </div>
        {!currentArea.data ? (
          <div className="query-state">
            <strong>尚未选择研究区</strong>
            <span>不会用全局 Run 列表代替当前研究上下文。</span>
          </div>
        ) : (
          <QueryBoundary
            pending={runs.isPending || currentArea.isPending}
            error={runs.error ?? currentArea.error}
            empty={items.length === 0}
            emptyTitle="当前研究区还没有已加载的运行记录"
            emptyDetail="提交 Launch 后，其 Run 会继承当前 WorkArea。"
          >
            <RunTable
              runs={items}
              onSelect={(runId) => navigate(`/runs/${runId}?user=${encodeURIComponent(user)}`)}
            />
          </QueryBoundary>
        )}
      </section>

      <div className="workbench-secondary-details" aria-label="平台辅助事实">
        <section aria-labelledby="platform-facts-heading">
          <div className="workbench-card-heading">
            <div>
              <p className="workbench-kicker">平台事实</p>
              <h2 id="platform-facts-heading">最近平台观测</h2>
            </div>
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
              <div>
                <dt>来源</dt>
                <dd>{platform.data?.source_type ?? capabilities.data?.source_authority ?? "—"}</dd>
              </div>
              <div><dt>观测时间</dt><dd>{formatTimestamp(platform.data?.observed_at)}</dd></div>
              <div><dt>数据质量</dt><dd><FactState status={platform.data?.data_quality} /></dd></div>
            </dl>
          </QueryBoundary>
        </section>

        <section aria-labelledby="entitlement-heading">
          <div className="workbench-card-heading">
            <div>
              <p className="workbench-kicker">我的权限</p>
              <h2 id="entitlement-heading">Slurm 授权</h2>
            </div>
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
