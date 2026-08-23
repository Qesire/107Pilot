import { AlertTriangle, Radio, ScrollText } from "lucide-react";
import { useState } from "react";
import { ApiRequestError } from "./api";
import { QueryBoundary, StatusBadge, formatTimestamp } from "./components";
import {
  useRuntimeWatch,
  useRuntimeWatchAlerts,
  useRuntimeWatchLogs,
} from "./query";
import type { RuntimeWatchState } from "./types";

type Tone = "neutral" | "info" | "success" | "warning" | "danger";

export function runtimeWatchStateLabel(state: RuntimeWatchState): string {
  return {
    watching: "等待首轮采集",
    waiting_for_log: "等待日志文件",
    active: "持续采集中",
    quiet_backoff: "静默退避",
    degraded: "采集异常",
    finalizing: "终态排空中",
    stopped: "日志已封存",
  }[state];
}

export function runtimeWatchTone(state: RuntimeWatchState): Tone {
  if (state === "active" || state === "stopped") return "success";
  if (state === "degraded") return "danger";
  if (state === "finalizing" || state === "waiting_for_log") return "warning";
  return "neutral";
}

export function RuntimeWatchPanel({ user, runId }: { user: string; runId: string }) {
  const [stream, setStream] = useState<"stdout" | "stderr">("stdout");
  const watch = useRuntimeWatch(user, runId);
  const logs = useRuntimeWatchLogs(user, runId, stream);
  const alerts = useRuntimeWatchAlerts(user, runId);
  const absent = watch.error instanceof ApiRequestError && watch.error.status === 404;

  if (absent) {
    return (
      <section className="runtime-watch-panel is-empty">
        <ScrollText aria-hidden="true" />
        <div><strong>尚未建立 Runtime Watch</strong><p>作业获得 Slurm Job ID 后，Worker 才会开始持久化增量日志。</p></div>
      </section>
    );
  }

  return (
    <section className="runtime-watch-panel" aria-label="Runtime Watch 实时日志">
      <header>
        <div><span className="runtime-kicker"><Radio aria-hidden="true" /> Runtime Watch</span><h3>增量标准流</h3><p>页面只读取持久化片段；刷新不会直接连接 Slurm 或远端文件系统。</p></div>
        {watch.data ? <StatusBadge label={runtimeWatchStateLabel(watch.data.state)} tone={runtimeWatchTone(watch.data.state)} /> : null}
      </header>
      <QueryBoundary pending={watch.isPending} error={watch.error}>
        <div className="runtime-stream-tabs" role="tablist" aria-label="日志流">
          {(["stdout", "stderr"] as const).map((item) => <button key={item} type="button" role="tab" aria-selected={stream === item} className={stream === item ? "active" : undefined} onClick={() => setStream(item)}>{item}<small>{watch.data?.streams[item].offset ?? 0} B</small></button>)}
        </div>
        <QueryBoundary pending={logs.isPending} error={logs.error}>
          <div className="runtime-log-frame"><div><span>{stream}</span><small>最近检查 {formatTimestamp(watch.data?.streams[stream].last_checked_at)}</small></div><pre><code>{logs.data?.content || "（当前没有日志内容）"}</code></pre></div>
        </QueryBoundary>
        {alerts.data?.items.length ? <div className="runtime-alerts"><h4><AlertTriangle aria-hidden="true" /> 暂定告警</h4>{alerts.data.items.map((alert) => <article key={alert.alert_id}><StatusBadge label={alert.code} tone={alert.severity === "critical" ? "danger" : "warning"} /><div><strong>{alert.summary}</strong><small>generation {alert.generation} · offset {alert.offset} · {formatTimestamp(alert.created_at)}</small></div></article>)}</div> : null}
        {watch.data?.state === "stopped" ? <p className="muted" role="status">日志已排空并封存。终态 Evidence 完成后，Agent 会生成一次结果解释；Slurm 成功不等同于科学结论有效。</p> : null}
      </QueryBoundary>
    </section>
  );
}
