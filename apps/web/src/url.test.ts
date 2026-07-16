import { describe, expect, it } from "vitest";
import { globalNavigationPath, withSearch } from "./url";

describe("withSearch", () => {
  it("preserves unrelated state and applies encoded updates", () => {
    const current = new URLSearchParams("user=alice&tab=logs");

    expect(withSearch("/runs/run 1", current, { object: "logs/stdout.json" })).toBe(
      "/runs/run 1?user=alice&tab=logs&object=logs%2Fstdout.json",
    );
    expect(current.toString()).toBe("user=alice&tab=logs");
  });

  it("deletes null and empty values without leaving a question mark", () => {
    const current = new URLSearchParams("user=alice");

    expect(withSearch("/projects", current, { user: null })).toBe("/projects");
    expect(withSearch("/projects", current, { user: "" })).toBe("/projects");
  });

  it("drops page-scoped filters from global navigation", () => {
    expect(globalNavigationPath("/runs", "alice")).toBe("/runs?user=alice");
  });
});
