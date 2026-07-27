import { useMemo } from "react";
import { ArrowRight } from "lucide-react";
import { formatTimestamp, StatusBadge } from "./components";
import { runStateLabel, runTone } from "./run-status";
import type { RunSummary } from "./types";

export type RunPickerRun = Pick<RunSummary, "run_id" | "state" | "created_at" | "recipe_version_id">
  & Partial<Pick<RunSummary, "updated_at" | "job_id" | "job_name" | "terminal_state" | "exit_code">>;

export interface RunPickerFilter {
  state?: string;
  states?: readonly string[];
}

export interface RunPickerProps {
  runs: RunPickerRun[];
  filter?: RunPickerFilter;
  selectedRunId?: string | null;
  onSelect: (runId: string) => void;
}

export function filterRuns(runs: RunPickerRun[], filter?: RunPickerFilter): RunPickerRun[] {
  if (filter?.states?.length) return runs.filter((r) => filter.states!.includes(r.state));
  if (!filter?.state) return runs;
  return runs.filter((r) => r.state === filter.state);
}

export function RunPicker({ runs, filter, selectedRunId, onSelect }: RunPickerProps) {
  const filtered = useMemo(() => filterRuns(runs, filter), [runs, filter]);
  if (filtered.length === 0) {
    return (
      <p className="run-picker-empty">
        没有匹配的 Run。请先在 Contract Studio 创建并提交作业。
      </p>
    );
  }
  return (
    <section className="run-picker-shell" aria-label="可选择的 Run">
      <header className="run-picker-heading">
        <div>
          <strong>选择一个 Run</strong>
          <span>{filter?.state
            ? `${runStateLabel(filter.state as RunSummary["state"])}作业`
            : filter?.states?.length
              ? "可修复的终态作业"
              : "最近作业"}</span>
        </div>
        <small>{filtered.length} 个结果</small>
      </header>
      <ul className="run-picker" aria-label="Run 列表">
        {filtered.map((run) => (
          <li key={run.run_id} className="run-picker-item">
            <button
              type="button"
              onClick={() => onSelect(run.run_id)}
              aria-pressed={run.run_id === selectedRunId}
              aria-label={`选择 Run ${run.run_id}`}
              className={`run-picker-button${run.run_id === selectedRunId ? " is-selected" : ""}`}
            >
              <span className="run-picker-identity">
                <strong className="run-id" title={run.job_name ?? run.run_id}>{run.job_name ?? "历史作业"}</strong>
                <small className="mono">{run.run_id}</small>
              </span>
              <StatusBadge label={runStateLabel(run.state)} tone={runTone(run.state)} />
              <span className="run-picker-facts" aria-label="Run 摘要">
                <span><small>sacct Job</small><b className="mono">{run.job_id ?? "未提交"}</b></span>
                <span><small>终态</small><b>{run.terminal_state ?? run.state}</b></span>
                <span><small>更新</small><b>{formatTimestamp(run.updated_at ?? run.created_at)}</b></span>
              </span>
              <ArrowRight className="run-picker-arrow" aria-hidden="true" size={16} />
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
