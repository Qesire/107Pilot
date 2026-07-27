import { ArrowUpRight } from "lucide-react";
import { formatTimestamp, StatusBadge } from "./components";
import { runStateLabel, runTone } from "./run-status";
import type { RunSummary } from "./types";

export function RunList({
  runs,
  selectedRunId,
  onSelect,
}: {
  runs: RunSummary[];
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
}) {
  return (
    <div className="run-list" aria-label="Run 结果">
      {runs.map((run) => {
        const selected = run.run_id === selectedRunId;
        return (
          <button
            key={run.run_id}
            className={`run-list-item${selected ? " is-selected" : ""}`}
            type="button"
            aria-current={selected ? "page" : undefined}
            aria-label={`查看 ${run.run_id}`}
            onClick={() => onSelect(run.run_id)}
          >
            <span className="run-list-topline">
              <span className="run-list-job" title={run.job_name ?? run.run_id}>
                <strong>{run.job_name ?? "历史作业"}</strong>
                <span>· sacct Job <b className="mono">{run.job_id ?? "尚未提交"}</b></span>
              </span>
              <StatusBadge label={runStateLabel(run.state)} tone={runTone(run.state)} />
            </span>
            <strong className="run-list-id mono" title={run.run_id}>{run.run_id}</strong>
            <span className="run-list-facts" aria-label="sacct 与证据摘要">
              <span><small>sacct 状态</small><b>{run.terminal_state ?? "等待回收"}</b></span>
              <span><small>ExitCode</small><b className="mono">{run.exit_code ?? "—"}</b></span>
              <span><small>证据</small><b>{run.collection_state}</b></span>
            </span>
            <span className="run-list-footer">更新于 {formatTimestamp(run.updated_at)}<ArrowUpRight aria-hidden="true" size={15} /></span>
          </button>
        );
      })}
    </div>
  );
}
