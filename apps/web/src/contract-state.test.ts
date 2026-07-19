import { describe, expect, it } from "vitest";
import {
  applyPatchToContract,
  createDefaultContract,
  diffText,
  linesToStrings,
  parseContractSource,
  readContractValue,
  serializeContract,
  updateContractPath,
} from "./contract-state";
import type { JsonObject } from "./types";

function advancedContract(): JsonObject {
  return {
    ...createDefaultContract(),
    runtime: {
      conda_env: "ml-2026",
      modules: ["cuda/12.4", "gcc/13"],
      environment: { TOKENIZERS_PARALLELISM: "false" },
      vendor_runtime_flag: "preserve-me",
    },
    resources: {
      partition: "GPU-A100",
      qos: "qos_gpu_short",
      nodes: 2,
      ntasks: 4,
      cpus_per_task: 8,
      time_limit: "01:00:00",
      array: { expression: "0-31", max_concurrency: 4, vendor_array_flag: true },
    },
    workflow: {
      dependencies: ["run_parent_a", "run_parent_b"],
      retry: { max_attempts: 3, backoff_seconds: 30, jitter: "full" },
      dag_metadata: { lane: "training" },
    },
    extensions: {
      "faculty.example": { priority: "teaching" },
      "pilot107.raw_sbatch": {
        directives: ["#SBATCH --exclusive"],
        prologue: "ulimit -n 4096",
      },
    },
  };
}

describe("canonical Contract state", () => {
  it("updates a basic field without losing advanced or unknown nested fields", () => {
    const original = advancedContract();
    const updated = updateContractPath(original, ["project", "workdir"], "/public/home/alice/new");

    expect(readContractValue(updated, ["project", "workdir"], "")).toBe("/public/home/alice/new");
    expect(readContractValue(updated, ["runtime", "vendor_runtime_flag"], "")).toBe("preserve-me");
    expect(readContractValue(updated, ["resources", "array", "vendor_array_flag"], false)).toBe(true);
    expect(readContractValue(updated, ["workflow", "retry", "jitter"], "")).toBe("full");
    expect(readContractValue(updated, ["extensions", "pilot107.raw_sbatch", "prologue"], "")).toBe("ulimit -n 4096");
    expect(readContractValue(original, ["project", "workdir"], "")).not.toBe("/public/home/alice/new");
  });

  it.each(["yaml", "json"] as const)("round-trips array/module/conda/workflow/extensions through %s", (format) => {
    const original = advancedContract();
    const reparsed = parseContractSource(serializeContract(original, format), format);

    expect(reparsed).toEqual(original);
  });

  it("normalizes multiline list projections without inventing values", () => {
    expect(linesToStrings("cuda/12.4\n\n gcc/13 \n")).toEqual(["cuda/12.4", "gcc/13"]);
  });

  it("rejects a source projection whose root is not an object", () => {
    expect(() => parseContractSource("- one\n- two\n", "yaml")).toThrow(/Contract/);
  });

  it("builds a stable line diff between entry command and materialized script", () => {
    expect(diffText("python3 main.py", "#!/bin/bash\npython3 main.py")).toEqual([
      { kind: "added", line: "#!/bin/bash" },
      { kind: "same", line: "python3 main.py" },
    ]);
  });

  it("bounds diff work for large scripts", () => {
    const before = Array.from({ length: 500 }, (_, index) => `before-${index}`).join("\n");
    const after = Array.from({ length: 500 }, (_, index) => `after-${index}`).join("\n");

    const result = diffText(before, after);

    expect(result).toHaveLength(1000);
    expect(result[0]?.kind).toBe("removed");
    expect(result.at(-1)?.kind).toBe("added");
  });

  it("applies a dotted-path patch from the agent without dropping unrelated fields", () => {
    const original = advancedContract();
    const patch = {
      "entry.command": "python3 train.py --epochs 50",
      "resources.cpus_per_task": 8,
      "resources.memory": "32G",
    };

    const patched = applyPatchToContract(original, patch);

    expect(readContractValue(patched, ["entry", "command"], "")).toBe(
      "python3 train.py --epochs 50",
    );
    expect(readContractValue(patched, ["resources", "cpus_per_task"], 0)).toBe(8);
    expect(readContractValue(patched, ["resources", "memory"], "")).toBe("32G");
    // Untouched nested vendor fields stay intact.
    expect(readContractValue(patched, ["runtime", "vendor_runtime_flag"], "")).toBe(
      "preserve-me",
    );
    expect(
      readContractValue(patched, ["resources", "array", "vendor_array_flag"], false),
    ).toBe(true);
    // Original is not mutated.
    expect(readContractValue(original, ["entry", "command"], "")).toBe("python3 main.py");
  });

  it("creates intermediate objects when the agent patch reaches a missing path", () => {
    const original = createDefaultContract();
    const patched = applyPatchToContract(original, {
      "policy.max_remediation_attempts": 5,
      "workflow.retry.backoff_seconds": 60,
    });

    expect(readContractValue(patched, ["policy", "max_remediation_attempts"], 0)).toBe(5);
    expect(readContractValue(patched, ["workflow", "retry", "backoff_seconds"], 0)).toBe(
      60,
    );
  });

  it("ignores empty patch keys gracefully", () => {
    const original = createDefaultContract();
    const patched = applyPatchToContract(original, { "": "ignored", "entry.command": "echo ok" });

    expect(readContractValue(patched, ["entry", "command"], "")).toBe("echo ok");
  });
});
