import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { ExperimentShell, experimentRunNextAction, runExperimentStage } from "./ExperimentShell";
import type { RunSummary } from "./types";

function run(state: RunSummary["state"]): RunSummary {
  return {
    run_id: "run_test",
    contract_id: "contract_test",
    owner: "alice",
    state,
    collection_state: "succeeded",
    diagnosis_state: "idle",
    capsule_state: "none",
    result_status: "unknown",
    job_id: "123",
    exit_code: state === "SUCCEEDED" ? "0:0" : "1:0",
    created_at: "2026-09-03T00:00:00Z",
    updated_at: "2026-09-03T00:00:00Z",
  };
}

const location = { pathname: "/runs/run_test", search: new URLSearchParams("user=alice") };

describe("ExperimentShell", () => {
  it("maps authoritative Run states onto lifecycle stages", () => {
    expect(runExperimentStage("VALIDATED")).toBe("preflight");
    expect(runExperimentStage("RUNNING")).toBe("run");
    expect(runExperimentStage("SUCCEEDED")).toBe("results");
    expect(runExperimentStage("FAILED")).toBe("repair");
  });

  it("derives one decision-oriented next action from the Run state", () => {
    expect(experimentRunNextAction("FAILED").tab).toBe("repair");
    expect(experimentRunNextAction("SUCCEEDED").tab).toBe("results");
    expect(experimentRunNextAction("RUNNING").tab).toBe("logs");
  });

  it("renders a contract lifecycle without inventing a Run identity", () => {
    const markup = renderToStaticMarkup(
      <ExperimentShell
        user="alice"
        location={{ pathname: "/studio/new", search: new URLSearchParams("user=alice") }}
        navigate={vi.fn()}
        context={{ kind: "contract", contractId: null, title: null, dirty: true }}
      >
        <div>content</div>
      </ExperimentShell>,
    );
    expect(markup).toContain("实验工作区");
    expect(markup).toContain("尚未持久化");
    expect(markup).not.toContain("运行 ID");
  });

  it("renders Run and Contract identifiers from the existing read model", () => {
    const markup = renderToStaticMarkup(
      <ExperimentShell
        user="alice"
        location={location}
        navigate={vi.fn()}
        context={{ kind: "run", run: run("FAILED") }}
      >
        <div>content</div>
      </ExperimentShell>,
    );
    expect(markup).toContain("contract_test");
    expect(markup).toContain("run_test");
    expect(markup).toContain("失败恢复");
    expect(markup).toContain("进入修复工作区");
  });
});
