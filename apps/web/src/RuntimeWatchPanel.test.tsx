import { describe, expect, it } from "vitest";
import { runtimeWatchStateLabel, runtimeWatchTone } from "./RuntimeWatchPanel";

describe("Runtime Watch presentation", () => {
  it("distinguishes live, draining, degraded, and stopped watches", () => {
    expect(runtimeWatchStateLabel("active")).toBe("持续采集中");
    expect(runtimeWatchStateLabel("finalizing")).toBe("终态排空中");
    expect(runtimeWatchStateLabel("stopped")).toBe("日志已封存");
    expect(runtimeWatchTone("degraded")).toBe("danger");
    expect(runtimeWatchTone("active")).toBe("success");
  });
});
