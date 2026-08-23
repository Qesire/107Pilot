import { describe, expect, it } from "vitest";
import {
  buildValidationEnvelope,
  changeSetStateLabel,
  changeSetTone,
  isValidationEnvelopeInputValid,
  isChangeSetPublishable,
  originLabel,
  riskLabel,
} from "./AgentProjectPanel";
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
    expect(isChangeSetPublishable(changeSet("reviewable"))).toBe(true);
    expect(isChangeSetPublishable(changeSet("published"))).toBe(false);
    expect(isChangeSetPublishable(changeSet("conflicted"))).toBe(false);
  });

  it("binds an approved validation envelope to the current Workspace snapshot", () => {
    const envelope = buildValidationEnvelope({
      owner: "alice",
      snapshotDigest: "a".repeat(64),
      partition: "debug",
      qos: "normal",
      cpus: 2,
      memoryMib: 2048,
      gpus: 0,
      walltimeSeconds: 300,
      now: new Date("2026-08-19T00:00:00Z"),
    });

    expect(envelope.workspace_snapshot_digest).toBe("a".repeat(64));
    expect(envelope.approved_by).toBe("alice");
    expect(envelope.expires_at).toBe("2026-08-19T01:00:00.000Z");
    expect(envelope.max_submissions).toBe(1);
  });

  it("rejects invalid or fractional validation resource inputs", () => {
    expect(isValidationEnvelopeInputValid({
      cpus: 1,
      memoryMib: 1024,
      gpus: 0,
      walltimeSeconds: 300,
    })).toBe(true);
    expect(isValidationEnvelopeInputValid({
      cpus: 0,
      memoryMib: 1024,
      gpus: 0,
      walltimeSeconds: 300,
    })).toBe(false);
    expect(isValidationEnvelopeInputValid({
      cpus: 1.5,
      memoryMib: 1024,
      gpus: 0,
      walltimeSeconds: 300,
    })).toBe(false);
  });
});
