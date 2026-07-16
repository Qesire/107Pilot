import { ArrowUpRight } from "lucide-react";
import { formatTimestamp, StatusBadge } from "./components";
import { runStateLabel, runTone } from "./run-status";
import type { RunSummary } from "./types";

export function RunTable({
  runs,
  onSelect,
}: {
  runs: RunSummary[];
  onSelect: (runId: string) => void;
}) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>作业</th>
            <th>状态</th>
            <th>证据</th>
            <th>Slurm Job</th>
            <th>更新时间</th>
            <th><span className="sr-only">操作</span></th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.run_id}>
              <td>
                <button className="text-button mono" type="button" onClick={() => onSelect(run.run_id)}>
                  {run.run_id}
                </button>
                <span className="cell-subline">{run.recipe_version_id ?? "未绑定 Recipe"}</span>
              </td>
              <td><StatusBadge label={runStateLabel(run.state)} tone={runTone(run.state)} /></td>
              <td><span className="compact-state">{run.collection_state}</span></td>
              <td className="mono">{run.job_id ?? "—"}</td>
              <td>{formatTimestamp(run.updated_at)}</td>
              <td>
                <button
                  className="icon-button"
                  type="button"
                  title={`查看 ${run.run_id}`}
                  aria-label={`查看 ${run.run_id}`}
                  onClick={() => onSelect(run.run_id)}
                >
                  <ArrowUpRight aria-hidden="true" size={17} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
