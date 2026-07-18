import { useMemo } from "react";
import type { RunSummary } from "./types";

export type RunPickerRun = Pick<RunSummary, "run_id" | "state" | "created_at" | "recipe_version_id">;

export interface RunPickerFilter {
  state?: string;
}

export interface RunPickerProps {
  runs: RunPickerRun[];
  filter?: RunPickerFilter;
  onSelect: (runId: string) => void;
}

export function filterRuns(runs: RunPickerRun[], filter?: RunPickerFilter): RunPickerRun[] {
  if (!filter?.state) return runs;
  return runs.filter((r) => r.state === filter.state);
}

export function RunPicker({ runs, filter, onSelect }: RunPickerProps) {
  const filtered = useMemo(() => filterRuns(runs, filter), [runs, filter]);
  if (filtered.length === 0) {
    return (
      <p className="run-picker-empty">
        没有匹配的 Run。请先在 Contract Studio 创建并提交作业。
      </p>
    );
  }
  return (
    <ul className="run-picker" role="listbox" aria-label="Run 列表">
      {filtered.map((run) => (
        <li key={run.run_id} className="run-picker-item">
          <button
            type="button"
            onClick={() => onSelect(run.run_id)}
            aria-label={`选择 Run ${run.run_id}`}
            className="run-picker-button"
          >
            <span className="run-id">{run.run_id}</span>
            <span className="run-state">{run.state}</span>
            <span className="run-recipe">{run.recipe_version_id}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}
