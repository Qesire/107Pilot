import { describe, expect, it } from "vitest";
import { runWorkspaceNextTab, runWorkspaceTabRequirements } from "./run-workspace";

describe("run workspace loading policy", () => {
  it("keeps overview and timeline free of Evidence/diagnosis/capsule eager reads", () => {
    expect(runWorkspaceTabRequirements("overview")).toEqual({
      evidence: false,
      diagnoses: false,
      capsule: false,
      health: false,
    });
    expect(runWorkspaceTabRequirements("timeline")).toEqual({
      evidence: false,
      diagnoses: false,
      capsule: false,
      health: false,
    });
  });

  it("loads only the deep read models required by each evidence tab", () => {
    expect(runWorkspaceTabRequirements("results")).toEqual({
      evidence: true,
      diagnoses: false,
      capsule: false,
      health: false,
    });
    expect(runWorkspaceTabRequirements("diagnosis")).toEqual({
      evidence: true,
      diagnoses: true,
      capsule: false,
      health: true,
    });
    expect(runWorkspaceTabRequirements("capsule")).toEqual({
      evidence: true,
      diagnoses: false,
      capsule: true,
      health: false,
    });
  });

  it("maps server next actions onto existing deep views without inventing new workflow states", () => {
    expect(runWorkspaceNextTab("prepare_repair")).toBe("diagnosis");
    expect(runWorkspaceNextTab("inspect_failure")).toBe("logs");
    expect(runWorkspaceNextTab("inspect_collection")).toBe("objects");
    expect(runWorkspaceNextTab("view_results")).toBe("results");
    expect(runWorkspaceNextTab("watch_run")).toBe("logs");
    expect(runWorkspaceNextTab("watch_queue")).toBe("overview");
  });
});
