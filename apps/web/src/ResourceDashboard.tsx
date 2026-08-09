import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";
import { HardDrive } from "lucide-react";
import { FactState, formatTimestamp } from "./components";
import { useResourceSummary, useStorageUsage } from "./query";
import {
  formatStorageBytes,
  jobStateLabel,
  nodeStateLabel,
  type StateCount,
} from "./resource-summary";

// Canvas charts cannot read CSS custom properties, so the theme palette is
// mirrored here as literal hex values (see styles.css :root).
const NODE_STATE_COLORS: Record<string, string> = {
  idle: "#167b69",
  mixed: "#2b6e9f",
  allocated: "#a56816",
  completing: "#5b8a72",
  draining: "#7b8782",
  down: "#b4423b",
  unknown: "#c6cec7",
};

const JOB_STATE_COLORS: Record<string, string> = {
  RUNNING: "#167b69",
  PENDING: "#a56816",
  COMPLETING: "#2b6e9f",
  COMPLETED: "#5b8a72",
  FAILED: "#b4423b",
  CANCELLED: "#7b8782",
  TIMEOUT: "#b4423b",
  NODE_FAIL: "#b4423b",
};

const FALLBACK_COLOR = "#c6cec7";
const TRACK_COLOR = "#e9ede7";

function nodeColor(state: string): string {
  return NODE_STATE_COLORS[state.toLowerCase()] ?? FALLBACK_COLOR;
}

function jobColor(state: string): string {
  return JOB_STATE_COLORS[state.toUpperCase()] ?? FALLBACK_COLOR;
}

interface DonutDatum {
  name: string;
  value: number;
  itemStyle: { color: string };
}

function donutOption(opts: {
  data: DonutDatum[];
  centerValue: string;
  centerLabel: string;
}): EChartsOption {
  return {
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    title: {
      text: opts.centerValue,
      subtext: opts.centerLabel,
      left: "center",
      top: "31%",
      itemGap: 2,
      textStyle: {
        fontSize: 22,
        fontWeight: 700,
        color: "#17211e",
        fontFamily: "Inter, 'Noto Sans SC', sans-serif",
      },
      subtextStyle: { fontSize: 11, color: "#7b8782" },
    },
    legend: {
      bottom: 0,
      left: "center",
      icon: "circle",
      itemWidth: 8,
      itemHeight: 8,
      itemGap: 12,
      textStyle: { fontSize: 11, color: "#52605b" },
    },
    series: [
      {
        type: "pie",
        radius: ["52%", "72%"],
        center: ["50%", "40%"],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: "#ffffff", borderWidth: 2 },
        label: { show: false },
        labelLine: { show: false },
        emphasis: { scale: true, scaleSize: 4 },
        data: opts.data,
      },
    ],
  };
}

function ChartCard({
  title,
  option,
}: {
  title: string;
  option: EChartsOption;
}) {
  return (
    <div className="resource-card">
      <p className="resource-card-title">{title}</p>
      <ReactECharts
        option={option}
        style={{ height: 210 }}
        opts={{ renderer: "canvas" }}
        notMerge
        lazyUpdate
      />
    </div>
  );
}

function EmptyChart({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="resource-card">
      <p className="resource-card-title">{title}</p>
      <div className="resource-card-empty">{hint}</div>
    </div>
  );
}

function StorageCard({ user }: { user: string }) {
  const storage = useStorageUsage(user);
  const usage = storage.data;
  const used = usage?.used_bytes ?? 0;
  const total = usage?.total_bytes ?? null;
  const pct =
    total && total > 0 ? Math.min(100, Math.round((used / total) * 100)) : null;

  return (
    <div className="resource-card storage-card">
      <p className="resource-card-title">
        <HardDrive aria-hidden="true" size={13} /> 个人存储用量
      </p>
      {storage.isError ? (
        <div className="resource-card-empty">暂无存储数据</div>
      ) : (
        <div className="storage-body">
          <div className="storage-figures">
            <strong>{formatStorageBytes(used)}</strong>
            <span>{total ? `/ ${formatStorageBytes(total)}` : "总量未知"}</span>
          </div>
          <div
            className="storage-bar"
            role="progressbar"
            aria-valuenow={pct ?? undefined}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <span
              className="storage-bar-fill"
              style={{ width: `${pct ?? 0}%` }}
            />
          </div>
          <p className="storage-meta">
            {pct !== null ? `已用 ${pct}%` : "仅统计已用空间"}
            {usage?.observed_at ? ` · 观测于 ${formatTimestamp(usage.observed_at)}` : ""}
          </p>
          {usage?.home ? <p className="storage-path">{usage.home}</p> : null}
        </div>
      )}
    </div>
  );
}

export function ResourceDashboard({ user }: { user: string }) {
  const summary = useResourceSummary(user);
  const data = summary.data;

  const nodeData: DonutDatum[] = (data?.nodes ?? []).map((item: StateCount) => ({
    name: nodeStateLabel(item.state),
    value: item.count,
    itemStyle: { color: nodeColor(item.state) },
  }));
  const nodeTotal = nodeData.reduce((acc, item) => acc + item.value, 0);

  const cpu = data?.cpu ?? { allocated: 0, total: 0 };
  const cpuFree = Math.max(0, cpu.total - cpu.allocated);
  const cpuData: DonutDatum[] =
    cpu.total > 0
      ? [
          { name: "已分配", value: cpu.allocated, itemStyle: { color: "#167b69" } },
          { name: "空闲", value: cpuFree, itemStyle: { color: TRACK_COLOR } },
        ]
      : [];

  const jobData: DonutDatum[] = (data?.jobs ?? []).map((item: StateCount) => ({
    name: jobStateLabel(item.state),
    value: item.count,
    itemStyle: { color: jobColor(item.state) },
  }));
  const jobTotal = jobData.reduce((acc, item) => acc + item.value, 0);

  const hasDetail = data?.hasDetail ?? false;

  return (
    <section className="panel resource-dashboard" aria-label="平台资源仪表盘">
      <div className="panel-heading">
        <div>
          <p className="panel-kicker">Live resources</p>
          <h2>平台资源</h2>
        </div>
        <div className="resource-facts">
          {data?.capturedAt ? (
            <span className="resource-observed">
              观测于 {formatTimestamp(data.capturedAt)}
            </span>
          ) : null}
          <FactState status={data?.freshness} />
        </div>
      </div>

      {summary.isError ? (
        <div className="resource-card-empty">暂无平台快照数据</div>
      ) : (
        <div className="resource-cards">
          {hasDetail && nodeData.length > 0 ? (
            <ChartCard
              title="节点状态"
              option={donutOption({
                data: nodeData,
                centerValue: String(nodeTotal),
                centerLabel: "节点",
              })}
            />
          ) : (
            <EmptyChart title="节点状态" hint="暂无节点数据" />
          )}

          {cpuData.length > 0 ? (
            <ChartCard
              title="CPU 分配"
              option={donutOption({
                data: cpuData,
                centerValue: String(cpu.allocated),
                centerLabel: `/ ${cpu.total} 核`,
              })}
            />
          ) : (
            <EmptyChart title="CPU 分配" hint="暂无 CPU 数据" />
          )}

          {hasDetail && jobData.length > 0 ? (
            <ChartCard
              title="作业状态"
              option={donutOption({
                data: jobData,
                centerValue: String(jobTotal),
                centerLabel: "作业",
              })}
            />
          ) : (
            <EmptyChart title="作业状态" hint="暂无作业数据" />
          )}

          <StorageCard user={user} />
        </div>
      )}
    </section>
  );
}
