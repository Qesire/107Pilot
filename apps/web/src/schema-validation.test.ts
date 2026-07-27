import { describe, expect, it } from "vitest";
import { compileClientSchemaValidator } from "./schema-validation";

const contractSchema = {
  type: "object",
  required: ["recipe_version_id", "project", "resources"],
  properties: {
    recipe_version_id: { type: "string", minLength: 1 },
    project: {
      type: "object",
      required: ["workdir"],
      properties: { workdir: { type: "string", minLength: 1 } },
    },
    resources: {
      type: "object",
      properties: { nodes: { type: "integer", minimum: 1 } },
    },
  },
  additionalProperties: false,
};

describe("compileClientSchemaValidator", () => {
  it("validates Contract-shape fields without runtime code generation", () => {
    const validate = compileClientSchemaValidator(contractSchema);

    expect(validate?.({ recipe_version_id: "recipe@1.0.0", project: { workdir: "/public/home/alice" }, resources: { nodes: 1 } })).toEqual([]);
    expect(validate?.({ recipe_version_id: "", project: {}, resources: { nodes: 0 }, extra: true })).toMatchObject([
      { instancePath: "/recipe_version_id", keyword: "minLength" },
      { instancePath: "/project", keyword: "required" },
      { instancePath: "/resources/nodes", keyword: "minimum" },
      { instancePath: "/extra", keyword: "additionalProperties" },
    ]);
  });
});
