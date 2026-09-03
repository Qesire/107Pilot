import { describe, expect, it } from "vitest";
import { deriveContractAssets } from "./ContractAssetSummary";
import type { JsonObject } from "./types";

const contract: JsonObject = {
  project: { workdir: "/public/home/alice/exp-a" },
  runtime: { environment: { DATA_ROOT: "/public/home/alice/data", KIT_ROOT: "" } },
  outputs: {
    expected: ["outputs/result.json", " logs/train.log ", { kind: "metric", path: "metrics.json" }],
  },
};

const schema = {
  required: ["runtime.environment.DATA_ROOT"],
  "project.workdir": { type: "shared_path" },
  "runtime.environment.DATA_ROOT": { type: "shared_path", contract: "existing dataset" },
  "runtime.environment.KIT_ROOT": { type: "shared_path" },
  "resources.partition": { type: "enum", allowed: ["Students"] },
};

describe("deriveContractAssets", () => {
  it("derives workdir, populated shared paths, and output declarations without duplicating workdir", () => {
    expect(deriveContractAssets(contract, schema)).toEqual({
      workdir: "/public/home/alice/exp-a",
      sharedPaths: [{
        field: "runtime.environment.DATA_ROOT",
        path: "/public/home/alice/data",
        detail: "existing dataset",
      }],
      expectedOutputs: ["outputs/result.json", "logs/train.log"],
      typedOutputCount: 1,
    });
  });

  it("does not invent references from missing or unknown schema fields", () => {
    expect(deriveContractAssets({}, null)).toEqual({
      workdir: null,
      sharedPaths: [],
      expectedOutputs: [],
      typedOutputCount: 0,
    });
  });
});
