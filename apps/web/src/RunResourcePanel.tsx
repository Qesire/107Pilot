import { Activity, DatabaseZap, Gauge, TriangleAlert } from "lucide-react";
import { ApiRequestError } from "./api";
import { QueryBoundary, StatusBadge, formatTimestamp } from "./components";
import { useRunResources } from "./query";
import type { ObservedMeasure, ResourceMeasures, RunResources } from "./types";

const unavailable: Record<ObservedMeasure["availability"], string> = {
  available: "—",
  unsupported: "不支持",
  permission_denied: "权限不足",
  not_collected: "未采集",
  insufficient_coverage: "覆盖不足",
  invalid: "数据无效",
};

export function formatObservedMeasure(measure?: ObservedMeasure): string {
  if (!measure) return "—";
  if (measure.availability !== "available" || measure.value === null) {
    return unavailable[measure.availability];
  }
  if (typeof measure.value === "string") return measure.value;
  if (measure.unit === "bytes") {
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let value = measure.value;
    let index = 0;
    while (Math.abs(value) >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value.toFixed(index === 0 ? 0 : 2)} ${units[index]}`;
  }
  if (measure.unit === "ratio") return `${(measure.value * 100).toFixed(1)}%`;
  if (measure.unit === "seconds" || measure.unit === "cpu_seconds") {
    if (measure.value >= 60) {
      const minutes = Math.floor(measure.value / 60);
      const seconds = Math.round(measure.value % 60);
      return `${minutes}m ${seconds}s`;
    }
    return `${measure.value.toFixed(1)} s`;
  }
  const suffix: Record<string, string> = {
    cpu: " CPU",
    gpu: " GPU",
    tasks: " tasks",
  };
  return `${measure.value}${suffix[measure.unit] ?? ` ${measure.unit}`}`;
}

const rows = [
  ["CPU time", "total_cpu", "CPU request", "allocated_cpus"],
  ["Peak memory", "max_rss", "Memory request", "allocated_memory"],
  ["Runtime", "elapsed", "Walltime request", "requested_walltime"],
  ["GPU utilization", "gpu_utilization", "GPU request", "allocated_gpus"],
] as const;

function sourceStamp(measures: ResourceMeasures): string | null {
  const measure = Object.values(measures)[0];
  return measure ? `${measure.source_adapter} · ${measure.source_operation}` : null;
}

export function RunResourceFacts({ data }: { data: RunResources }) {
  const used = data.used ?? data.measures ?? {};
  const allocated = data.allocated ?? {};
  const source = sourceStamp(used);
  return (
    <section className="run-resource-panel" aria-label="Run 资源观测">
      <header>
        <div>
          <span className="resource-ledger-kicker">
            <DatabaseZap aria-hidden="true" /> Resource accounting
          </span>
          <h3>资源账本</h3>
          <p>已用与已申请并列展示；缺失、零值和不支持保持不同语义。</p>
        </div>
        <StatusBadge
          label={data.freshness === "terminal" ? "终态已封存" : data.freshness}
          tone={data.freshness === "expired" ? "warning" : "success"}
        />
      </header>
      <div className="resource-ledger" role="table" aria-label="资源使用与申请对照">
        <div className="resource-ledger-head" role="row">
          <strong role="columnheader">Measured</strong>
          <span aria-hidden="true">↔</span>
          <strong role="columnheader">Allocated</strong>
        </div>
        {rows.map(([usedLabel, usedKey, allocatedLabel, allocatedKey]) => (
          <div className="resource-ledger-row" role="row" key={usedKey}>
            <div role="cell">
              <small>{usedLabel}</small>
              <strong>{formatObservedMeasure(used[usedKey])}</strong>
            </div>
            <span className="resource-ledger-axis" aria-hidden="true" />
            <div role="cell">
              <small>{allocatedLabel}</small>
              <strong>{formatObservedMeasure(allocated[allocatedKey])}</strong>
            </div>
          </div>
        ))}
      </div>
      <footer className="resource-source-stamp">
        <Gauge aria-hidden="true" />
        <span>{source ?? "来源未记录"}</span>
        <span>{formatTimestamp(data.captured_at)}</span>
        <code>{data.observation_id}</code>
      </footer>
      {data.warnings.length ? (
        <ul className="resource-warnings">
          {data.warnings.map((warning) => <li key={warning}>{warning}</li>)}
        </ul>
      ) : null}
      {data.evaluations.length ? (
        <div className="resource-evaluations">
          <h4><Activity aria-hidden="true" /> 证据充分的建议</h4>
          {data.evaluations.map((item) => (
            <article key={item.evaluation_id}>
              <TriangleAlert aria-hidden="true" />
              <div>
                <strong>{item.rule_id}</strong>
                <p>{item.summary}</p>
                <small>{item.confidence} confidence · 建议仅为提案，不会自动提交 Run</small>
              </div>
            </article>
          ))}
        </div>
      ) : null}
    </section>
  );
}

export function RunResourcePanel({ user, runId }: { user: string; runId: string }) {
  const resources = useRunResources(user, runId);
  const absent = resources.error instanceof ApiRequestError && resources.error.status === 404;
  if (absent) {
    return (
      <section className="run-resource-panel is-empty">
        <Gauge aria-hidden="true" />
        <div><strong>尚无资源观测</strong><p>Worker 采集后，这里会显示持久化的资源事实。</p></div>
      </section>
    );
  }
  return (
    <QueryBoundary pending={resources.isPending} error={resources.error}>
      {resources.data ? <RunResourceFacts data={resources.data} /> : null}
    </QueryBoundary>
  );
}
