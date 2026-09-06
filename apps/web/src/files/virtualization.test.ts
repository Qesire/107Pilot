import { describe, expect, it } from "vitest";
import { computeVirtualRange } from "./virtualization";

describe("computeVirtualRange", () => {
  it("keeps a large directory DOM window bounded", () => {
    const range = computeVirtualRange(50_000, 40, 800_000, 720, 8);
    expect(range.end - range.start).toBeLessThanOrEqual(35);
    expect(range.start).toBeGreaterThan(0);
    expect(range.paddingBefore).toBeGreaterThan(0);
    expect(range.paddingAfter).toBeGreaterThan(0);
  });

  it("clamps the first and last windows", () => {
    expect(computeVirtualRange(10, 40, 0, 400, 8)).toEqual({
      start: 0, end: 10, paddingBefore: 0, paddingAfter: 0,
    });
    const tail = computeVirtualRange(1_000, 40, 40_000, 400, 4);
    expect(tail.end).toBe(1_000);
    expect(tail.paddingAfter).toBe(0);
  });
});
