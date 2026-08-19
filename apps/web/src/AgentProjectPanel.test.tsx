import { describe, expect, it } from "vitest";
import { changeSetStateLabel, changeSetTone, originLabel, riskLabel } from "./AgentProjectPanel";
import type { WorkspaceChangeSet } from "./types";

function changeSet(state: WorkspaceChangeSet["state"]): WorkspaceChangeSet {
  return {
    schema_version: "pilot107.workspace-changeset/v1",
    change_set_id: "changeset-1",
    project_id: "project-1",
    workspace_id: "workspace-1",
    owner: "alice",
    base_snapshot_digest: "a".repeat(64),
    digest: "b".repeat(64),
    state,
    version: 1,
    files: [],
    sandbox_results: [],
    approval: null,
    created_at: "2026-08-19T00:00:00Z",
    updated_at: "2026-08-19T00:00:00Z",
  };
}

describe("Agent Project review presentation", () => {
  it("labels project origins and explicit risk levels", () => {
    expect(originLabel("blank")).toBe("空白");
    expect(originLabel("existing")).toBe("现有目录");
    expect(riskLabel("low")).toBe("低风险");
    expect(riskLabel("high")).toBe("高风险");
  });

  it("distinguishes reviewable and failed ChangeSets", () => {
    expect(changeSetStateLabel("reviewable")).toBe("可审阅");
    expect(changeSetTone(changeSet("reviewable"))).toBe("success");
    expect(changeSetTone(changeSet("failed"))).toBe("danger");
  });
});
