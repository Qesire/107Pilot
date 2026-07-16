import { describe, expect, it } from "vitest";
import {
  collectSchemaCompletions,
  compileEditorValidator,
  sourceDiagnostics,
} from "./ContractSourceEditor";

const schema = {
  type: "object",
  required: ["project"],
  properties: {
    project: {
      type: "object",
      required: ["workdir"],
      properties: { workdir: { type: "string", minLength: 1 } },
    },
    resources: {
      type: "object",
      properties: { partition: { type: "string" } },
    },
  },
};

describe("Contract source editor", () => {
  it("derives schema-backed field completions", () => {
    const completions = collectSchemaCompletions(schema);

    expect(completions.map((item) => item.label)).toEqual([
      "partition",
      "project",
      "resources",
      "workdir",
    ]);
    expect(completions.find((item) => item.label === "workdir")?.detail).toBe("project.workdir");
  });

  it("reports parser positions and Ajv field diagnostics", () => {
    const parseErrors = sourceDiagnostics('{"project": ', "json", null);
    const schemaErrors = sourceDiagnostics("project: {}\n", "yaml", compileEditorValidator(schema));

    expect(parseErrors).toHaveLength(1);
    expect(parseErrors[0]?.from).toBeGreaterThanOrEqual(0);
    expect(schemaErrors.some((item) => item.message.includes("workdir"))).toBe(true);
  });
});
