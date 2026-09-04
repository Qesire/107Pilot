import { describe, expect, it } from "vitest";
import { runWorkspaceNextTab, runWorkspaceTabRequirements } from "./run-workspace";

describe("run workspace loading policy", () => {
  it("keeps summary, timeline, and repair facade free of legacy eager reads", () => {
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
    expect(runWorkspaceTabRequirements("repair")).toEqual({
      evidence: false,
      diagnoses: false,
      capsule: false,
      health: false,
    });
  });

  it("loads only the deep read models required by each legacy evidence tab", () => {
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

  it("routes repair intent into the first-class failure workspace", () => {
    expect(runWorkspaceNextTab("prepare_repair")).toBe("repair");
    expect(runWorkspaceNextTab("inspect_failure")).toBe("logs");
    expect(runWorkspaceNextTab("inspect_collection")).toBe("objects");
    expect(runWorkspaceNextTab("view_results")).toBe("results");
    expect(runWorkspaceNextTab("watch_run")).toBe("logs");
    expect(runWorkspaceNextTab("watch_queue")).toBe("overview");
  });
});
