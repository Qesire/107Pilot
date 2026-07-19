import { describe, expect, it } from "vitest";
import {
  collectSchemaCompletions,
  compileEditorValidator,
  sourceDiagnostics,
} from "./ContractSourceEditor";
import { isPlaceholderValue } from "./StudioPage";

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

describe("isPlaceholderValue", () => {
  it("flags empty, null and undefined as placeholders", () => {
    expect(isPlaceholderValue("")).toBe(true);
    expect(isPlaceholderValue(null)).toBe(true);
    expect(isPlaceholderValue(undefined)).toBe(true);
  });

  it("flags known template command placeholders", () => {
    expect(isPlaceholderValue("echo ok")).toBe(true);
    expect(isPlaceholderValue("python3 main.py")).toBe(true);
    expect(isPlaceholderValue("  python3 main.py  ")).toBe(true);
  });

  it("leaves real user-provided values alone", () => {
    expect(isPlaceholderValue("python3 train.py --epochs 50")).toBe(false);
    expect(isPlaceholderValue("/public/home/alice/work")).toBe(false);
    expect(isPlaceholderValue(4)).toBe(false);
  });
});
