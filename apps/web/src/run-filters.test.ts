import { describe, expect, it } from "vitest";
import { clearRunFilters, loadRunFilters, saveRunFilters } from "./run-filters";

describe("saved Run filters", () => {
  it("keeps filters owner-local and round trips valid values", () => {
    const storage = memoryStorage();

    saveRunFilters("alice", { state: "FAILED", search: "run 42" }, storage);

    expect(loadRunFilters("alice", storage)).toEqual({ state: "FAILED", search: "run 42" });
    expect(loadRunFilters("bob", storage)).toBeNull();
    clearRunFilters("alice", storage);
    expect(loadRunFilters("alice", storage)).toBeNull();
  });

  it("fails closed for corrupt, unsupported, or oversized stored values", () => {
    const storage = memoryStorage();
    storage.setItem("pilot107.run-filters.v1.alice", "not json");
    expect(loadRunFilters("alice", storage)).toBeNull();
    storage.setItem("pilot107.run-filters.v1.alice", JSON.stringify({ state: "ROOT", search: "" }));
    expect(loadRunFilters("alice", storage)).toBeNull();
    storage.setItem("pilot107.run-filters.v1.alice", JSON.stringify({ state: "", search: "x".repeat(257) }));
    expect(loadRunFilters("alice", storage)).toBeNull();
  });
});

function memoryStorage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  };
}
