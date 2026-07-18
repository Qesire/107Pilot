import { parse as parseYaml, stringify as stringifyYaml } from "yaml";
import type { JsonObject } from "./types";

export type SourceFormat = "yaml" | "json";
export type DiffLine = { kind: "same" | "added" | "removed"; line: string };

export function createDefaultContract(): JsonObject {
  return {
    schema_version: "pilot107.contract/v2",
    recipe_version_id: "recipe_python_cpu@1.0.0",
    project: {
      name: "",
      workdir: "/public/home/alice",
    },
    entry: {
      command: "python3 main.py",
    },
    runtime: {
      conda_env: null,
      container_image: null,
      modules: [],
      environment: {},
    },
    resources: {
      partition: "Students",
      qos: "qos_stu_cpu_long",
      nodes: 1,
      ntasks: 1,
      cpus_per_task: 1,
      memory: "4G",
      gpus_per_node: null,
      time_limit: "00:30:00",
      array: null,
    },
    workflow: {
      dependencies: [],
      retry: {
        max_attempts: 1,
        backoff_seconds: 0,
      },
    },
    outputs: {
      expected: [],
      success_conditions: ["slurm_exit_code_zero"],
    },
    policy: {
      automation_level: "explain",
      max_remediation_attempts: 0,
      require_approval: true,
    },
    extensions: {},
  };
}

export function updateContractPath(
  contract: JsonObject,
  path: readonly string[],
  value: unknown,
): JsonObject {
  if (path.length === 0) return asJsonObject(value);
  const root = structuredClone(contract);
  let cursor: JsonObject = root;
  path.slice(0, -1).forEach((key) => {
    const current = cursor[key];
    const next = isJsonObject(current) ? current : {};
    cursor[key] = next;
    cursor = next;
  });
  const finalKey = path.at(-1);
  if (finalKey) cursor[finalKey] = value;
  return root;
}

export function readContractValue<T>(
  contract: JsonObject,
  path: readonly string[],
  fallback: T,
): T {
  let cursor: unknown = contract;
  for (const key of path) {
    if (!isJsonObject(cursor) || !(key in cursor)) return fallback;
    cursor = cursor[key];
  }
  return (cursor as T | undefined) ?? fallback;
}

export function linesToStrings(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function stringsToLines(value: unknown): string {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? value.join("\n")
    : "";
}

export function serializeContract(contract: JsonObject, format: SourceFormat): string {
  return format === "json"
    ? `${JSON.stringify(contract, null, 2)}\n`
    : stringifyYaml(contract, { indent: 2, lineWidth: 0 });
}

export function parseContractSource(source: string, format: SourceFormat): JsonObject {
  const parsed: unknown = format === "json" ? JSON.parse(source) : parseYaml(source);
  return asJsonObject(parsed);
}

export function parseJsonObject(source: string, label: string): JsonObject {
  try {
    return asJsonObject(JSON.parse(source));
  } catch (error) {
    const detail = error instanceof Error ? error.message : "invalid JSON";
    throw new Error(`${label}: ${detail}`);
  }
}

export function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function diffText(before: string, after: string): DiffLine[] {
  const left = before.split("\n");
  const right = after.split("\n");
  if (left.length * right.length > 200_000) {
    return [
      ...left.map((line) => ({ kind: "removed" as const, line })),
      ...right.map((line) => ({ kind: "added" as const, line })),
    ];
  }
  const lengths = Array.from({ length: left.length + 1 }, () =>
    Array<number>(right.length + 1).fill(0),
  );
  for (let leftIndex = left.length - 1; leftIndex >= 0; leftIndex -= 1) {
    for (let rightIndex = right.length - 1; rightIndex >= 0; rightIndex -= 1) {
      lengths[leftIndex]![rightIndex] = left[leftIndex] === right[rightIndex]
        ? 1 + lengths[leftIndex + 1]![rightIndex + 1]!
        : Math.max(lengths[leftIndex + 1]![rightIndex]!, lengths[leftIndex]![rightIndex + 1]!);
    }
  }
  const result: DiffLine[] = [];
  let leftIndex = 0;
  let rightIndex = 0;
  while (leftIndex < left.length && rightIndex < right.length) {
    if (left[leftIndex] === right[rightIndex]) {
      result.push({ kind: "same", line: left[leftIndex] ?? "" });
      leftIndex += 1;
      rightIndex += 1;
    } else if (lengths[leftIndex + 1]![rightIndex]! >= lengths[leftIndex]![rightIndex + 1]!) {
      result.push({ kind: "removed", line: left[leftIndex] ?? "" });
      leftIndex += 1;
    } else {
      result.push({ kind: "added", line: right[rightIndex] ?? "" });
      rightIndex += 1;
    }
  }
  while (leftIndex < left.length) result.push({ kind: "removed", line: left[leftIndex++] ?? "" });
  while (rightIndex < right.length) result.push({ kind: "added", line: right[rightIndex++] ?? "" });
  return result;
}

function asJsonObject(value: unknown): JsonObject {
  if (!isJsonObject(value)) throw new Error("Contract 必须是 JSON/YAML object。 ");
  return value;
}
