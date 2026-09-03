import { describe, expect, it } from "vitest";
import { runtimePollingInterval } from "./runtime-polling";

describe("Runtime polling policy", () => {
  it("keeps active logs responsive while polling alerts less often", () => {
    expect(runtimePollingInterval("summary", "active")).toBe(3_000);
    expect(runtimePollingInterval("logs", "active")).toBe(3_000);
    expect(runtimePollingInterval("alerts", "active")).toBe(5_000);
  });

  it("backs off quiet, waiting, and degraded watches", () => {
    expect(runtimePollingInterval("logs", "waiting_for_log")).toBe(10_000);
    expect(runtimePollingInterval("summary", "degraded")).toBe(10_000);
    expect(runtimePollingInterval("logs", "quiet_backoff")).toBe(15_000);
  });

  it("polls finalization promptly and freezes a stopped watch", () => {
    expect(runtimePollingInterval("summary", "finalizing")).toBe(2_000);
    expect(runtimePollingInterval("logs", "finalizing")).toBe(2_000);
    expect(runtimePollingInterval("summary", "stopped")).toBe(false);
    expect(runtimePollingInterval("logs", "stopped")).toBe(false);
    expect(runtimePollingInterval("alerts", "stopped")).toBe(false);
  });

  it("does not poll hidden viewers or child channels before summary exists", () => {
    expect(runtimePollingInterval("summary", "active", "hidden")).toBe(false);
    expect(runtimePollingInterval("logs", "active", "hidden")).toBe(false);
    expect(runtimePollingInterval("summary", null)).toBe(5_000);
    expect(runtimePollingInterval("logs", null)).toBe(false);
    expect(runtimePollingInterval("alerts", null)).toBe(false);
  });
});
