import type { AgentTool } from "@earendil-works/pi-agent-core";
import { Type, type TSchema } from "typebox";

import {
  A2_PROJECT_TOOL_NAMES,
  type DurableAgentTurnRequest,
  type JsonObject,
} from "./protocol.js";
import { toolFailureResult, type ReadToolGateway } from "./read-tools.js";

type ProjectToolName = (typeof A2_PROJECT_TOOL_NAMES)[number];
const Id = Type.String({ minLength: 1, maxLength: 128 });
const Path = Type.String({ minLength: 1, maxLength: 4_096 });
const RelativePath = Type.String({
  minLength: 1,
  maxLength: 4_096,
  pattern: "^(?!/)(?!.*(?:^|/)\\.\\.(?:/|$))[^\\\\]+$",
});
const Scope = { project_id: Id, workspace_id: Id };
const ResourceHints = Type.Object(
  {
    partition: Type.Optional(Type.String({ minLength: 1, maxLength: 128 })),
    qos: Type.Optional(Type.String({ minLength: 1, maxLength: 128 })),
    cpus_per_task: Type.Optional(Type.Integer({ minimum: 1, maximum: 1_048_576 })),
    memory_mib: Type.Optional(Type.Integer({ minimum: 1, maximum: Number.MAX_SAFE_INTEGER })),
    gpus: Type.Optional(Type.Integer({ minimum: 0, maximum: 1_048_576 })),
    time_limit: Type.Optional(Type.String({ minLength: 1, maxLength: 64 })),
  },
  { additionalProperties: false },
);
const ProjectBlueprintSchema = Type.Object(
  {
    goal: Type.String({ minLength: 1, maxLength: 64_000 }),
    entrypoints: Type.Array(RelativePath, { maxItems: 64 }),
    files: Type.Array(Type.Object(
      {
        path: RelativePath,
        purpose: Type.String({ minLength: 1, maxLength: 4_096 }),
        classification: Type.Union([
          Type.Literal("editable"),
          Type.Literal("read_only"),
          Type.Literal("metadata_only"),
          Type.Literal("excluded"),
        ]),
      },
      { additionalProperties: false },
    ), { maxItems: 4_096 }),
    validations: Type.Array(Type.Object(
      {
        validation_id: Id,
        execution: Type.Union([Type.Literal("sandbox"), Type.Literal("slurm")]),
        argv: Type.Array(Type.String({ minLength: 1, maxLength: 4_096 }), {
          minItems: 1,
          maxItems: 128,
        }),
        expected_outputs: Type.Array(RelativePath, { maxItems: 256 }),
      },
      { additionalProperties: false },
    ), { maxItems: 256 }),
    contract_intent: Type.Object(
      {
        recipe_version_id: Type.Union([
          Type.Null(),
          Type.String({ minLength: 1, maxLength: 256, pattern: "^[A-Za-z0-9._:@-]+$" }),
        ]),
        resource_hints: ResourceHints,
      },
      { additionalProperties: false },
    ),
    expected_outputs: Type.Array(Type.Object(
      {
        path: RelativePath,
        kind: Type.Union([
          Type.Literal("file"),
          Type.Literal("directory"),
          Type.Literal("json"),
          Type.Literal("table"),
          Type.Literal("metric"),
        ]),
        required: Type.Boolean(),
      },
      { additionalProperties: false },
    ), { maxItems: 4_096 }),
    dependencies: Type.Array(Type.Object(
      {
        name: Type.String({ minLength: 1, maxLength: 256 }),
        version: Type.String({ minLength: 1, maxLength: 256 }),
        source: Type.Union([
          Type.Literal("runtime"),
          Type.Literal("module"),
          Type.Literal("conda"),
          Type.Literal("project"),
          Type.Literal("system"),
        ]),
      },
      { additionalProperties: false },
    ), { maxItems: 4_096 }),
    open_questions: Type.Array(Type.String({ minLength: 1, maxLength: 4_096 }), {
      maxItems: 256,
    }),
  },
  { additionalProperties: false },
);
const WorkspacePatch = Type.Object(
  {
    path: Path,
    expected_source_digest: Type.Union([
      Type.Null(),
      Type.String({ pattern: "^[a-f0-9]{64}$" }),
    ]),
    operation: Type.Union([
      Type.Literal("create"),
      Type.Literal("modify"),
      Type.Literal("delete"),
    ]),
    content: Type.Union([Type.Null(), Type.String({ maxLength: 8 * 1024 * 1024 })]),
  },
  { additionalProperties: false },
);

const ARGUMENT_SCHEMAS = {
  project_get: Type.Object(Scope, { additionalProperties: false }),
  project_blueprint_save: Type.Object(
    {
      ...Scope,
      expected_version: Type.Integer({ minimum: 1, maximum: Number.MAX_SAFE_INTEGER }),
      blueprint: ProjectBlueprintSchema,
    },
    { additionalProperties: false },
  ),
  workspace_list: Type.Object(Scope, { additionalProperties: false }),
  workspace_read: Type.Object({ ...Scope, path: Path }, { additionalProperties: false }),
  workspace_patch: Type.Object(
    {
      ...Scope,
      patches: Type.Array(WorkspacePatch, { minItems: 1, maxItems: 256 }),
    },
    { additionalProperties: false },
  ),
  workspace_diff: Type.Object(
    { ...Scope, change_set_id: Id },
    { additionalProperties: false },
  ),
  sandbox_exec: Type.Object(
    {
      ...Scope,
      change_set_id: Id,
      argv: Type.Array(Type.String({ minLength: 1, maxLength: 4_096 }), {
        minItems: 1,
        maxItems: 128,
      }),
      timeout: Type.Number({ minimum: 0.1, maximum: 300 }),
    },
    { additionalProperties: false },
  ),
  validation_schedule: Type.Object(
    {
      ...Scope,
      request_key: Id,
      cpus: Type.Integer({ minimum: 1, maximum: 1_048_576 }),
      memory_mib: Type.Integer({ minimum: 1, maximum: Number.MAX_SAFE_INTEGER }),
      gpus: Type.Integer({ minimum: 0, maximum: 1_048_576 }),
      walltime_seconds: Type.Integer({ minimum: 1, maximum: 31_536_000 }),
      tasks: Type.Integer({ minimum: 1, maximum: 1_024 }),
      submissions: Type.Integer({ minimum: 1, maximum: 1_024 }),
      script: Type.String({ minLength: 1, maxLength: 262_144 }),
      job_name: Id,
    },
    { additionalProperties: false },
  ),
} satisfies Record<ProjectToolName, TSchema>;

const DESCRIPTIONS = {
  project_get: "Read the bound experiment Project, Blueprint, Workspace, and ChangeSets.",
  project_blueprint_save: "Save a complete typed Blueprint for the bound Project.",
  workspace_list: "List bounded regular files in the bound isolated Workspace.",
  workspace_read: "Read one bounded UTF-8 file from the bound isolated Workspace.",
  workspace_patch: "Atomically apply digest-guarded text patches inside the bound Workspace.",
  workspace_diff: "Read the bounded unified diff for one bound ChangeSet.",
  sandbox_exec: "Run one allowlisted argv-only validation in the network-disabled sandbox.",
  validation_schedule:
    "Schedule one approved, bounded Slurm validation and end this Turn while it runs.",
} satisfies Record<ProjectToolName, string>;

export function createProjectTools(
  request: DurableAgentTurnRequest,
  gateway: ReadToolGateway,
): AgentTool[] {
  return A2_PROJECT_TOOL_NAMES.map((name) => ({
    name,
    label: name,
    description: DESCRIPTIONS[name],
    parameters: ARGUMENT_SCHEMAS[name],
    executionMode: "sequential",
    execute: async (toolCallId, params, signal) => {
      const argumentsWithBinding = name === "validation_schedule"
        ? {
            ...(params as JsonObject),
            session_id: request.session_id,
            turn_id: request.turn_id,
          }
        : params as JsonObject;
      const result = await gateway.invoke(
        request,
        toolCallId,
        name,
        argumentsWithBinding,
        signal ?? new AbortController().signal,
      );
      if (result.error !== null) {
        return toolFailureResult(result.error);
      }
      if (result.result === null) {
        throw new Error("The Project tool returned an invalid success envelope.");
      }
      return {
        content: [{ type: "text", text: JSON.stringify(result.result) }],
        details: {
          result: structuredClone(result.result),
          evidence_refs: [...result.evidence_refs],
          bytes_returned: result.bytes_returned,
        },
        terminate: name === "validation_schedule",
      };
    },
  }));
}
