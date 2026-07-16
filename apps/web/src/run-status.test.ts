import { describe, expect, it } from "vitest";
import { runStateLabel, runTone } from "./run-status";
import type { RunState } from "./types";

describe("run status presentation", () => {
  it.each<[RunState, string]>([
    ["DRAFT", "neutral"],
    ["VALIDATED", "neutral"],
    ["SUBMITTING", "info"],
    ["SUBMITTED", "info"],
    ["PENDING", "info"],
    ["RUNNING", "info"],
    ["COMPLETING", "info"],
    ["SUCCEEDED", "success"],
    ["FAILED", "danger"],
    ["SUBMIT_FAILED", "danger"],
    ["COLLECTION_FAILED", "danger"],
    ["AUTH_REQUIRED", "danger"],
    ["CANCELLED", "warning"],
    ["UNKNOWN", "warning"],
    ["SUBMISSION_UNCERTAIN", "warning"],
    ["EVIDENCE_PARTIAL", "warning"],
  ])("maps %s to %s", (state, tone) => {
    expect(runTone(state)).toBe(tone);
  });

  it("uses novice-facing labels for ambiguous failure states", () => {
    expect(runStateLabel("AUTH_REQUIRED")).toBe("需要认证");
    expect(runStateLabel("SUBMISSION_UNCERTAIN")).toBe("提交待确认");
    expect(runStateLabel("EVIDENCE_PARTIAL")).toBe("证据不完整");
  });
});
