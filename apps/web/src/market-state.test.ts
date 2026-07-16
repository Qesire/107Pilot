import { describe, expect, it } from "vitest";
import { detailVersions } from "./market-state";

describe("detailVersions", () => {
  it("keeps a directly requested withdrawn version missing from market results", () => {
    expect(detailVersions(
      [{ template_id: "template_a", release_version: "1.1.0" }],
      "template_a",
      "1.0.0",
    )).toEqual(["1.1.0", "1.0.0"]);
  });

  it("sorts versions and ignores releases belonging to another template", () => {
    expect(detailVersions([
      { template_id: "template_a", release_version: "1.2.0" },
      { template_id: "template_b", release_version: "9.0.0" },
      { template_id: "template_a", release_version: "1.10.0" },
    ], "template_a", null)).toEqual(["1.10.0", "1.2.0"]);
  });
});
