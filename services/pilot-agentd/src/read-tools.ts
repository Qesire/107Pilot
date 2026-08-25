import type { AgentTool } from "@earendil-works/pi-agent-core";
import { Type, type TSchema } from "typebox";

import {
  A1_READ_TOOL_NAMES,
  type DurableAgentTurnRequest,
  type JsonObject,
  type ReadToolName,
  type ToolInvocation,
  type ToolResult,
} from "./protocol.js";

type A1ToolName = (typeof A1_READ_TOOL_NAMES)[number];

const BoundedId = Type.String({ minLength: 1, maxLength: 4_096 });

const PLATFORM_READ_TOOL_NAMES = [
  "platform_get_snapshot",
  "platform_observation_get",
  "account_observation_get",
] as const satisfies readonly A1ToolName[];
const RUN_READ_TOOL_NAMES = [
  "run_get",
  "run_log_read",
  "run_resources_get",
] as const satisfies readonly A1ToolName[];

const ARGUMENT_SCHEMAS = {
  platform_get_snapshot: Type.Object({}, { additionalProperties: false }),
  platform_observation_get: Type.Object(
    { connection_id: BoundedId },
    { additionalProperties: false },
  ),
  account_observation_get: Type.Object(
    { connection_id: BoundedId },
    { additionalProperties: false },
  ),
  run_get: Type.Object(
    { run_id: BoundedId },
    { additionalProperties: false },
  ),
  run_log_read: Type.Object(
    {
      run_id: BoundedId,
      stream: Type.Union([Type.Literal("stdout"), Type.Literal("stderr")]),
      cursor: Type.Integer({ minimum: 0, maximum: 2_147_483_647 }),
    },
    { additionalProperties: false },
  ),
  evidence_read: Type.Object(
    { run_id: BoundedId, object_id: BoundedId },
    { additionalProperties: false },
  ),
  run_resources_get: Type.Object(
    { run_id: BoundedId },
    { additionalProperties: false },
  ),
} satisfies Record<A1ToolName, TSchema>;

const TOOL_DESCRIPTIONS = {
  platform_get_snapshot: "Read the latest safe platform snapshot for this owner.",
  platform_observation_get: "Read the latest persisted platform resource observation.",
  account_observation_get: "Read the caller's latest persisted account observation.",
  run_get: "Read the safe state and resource summary for one owned Run.",
  run_log_read: "Read a bounded stdout or stderr page for one owned Run.",
  evidence_read: "Read a bounded preview of one owned evidence object.",
  run_resources_get: "Read persisted live or terminal resource facts for one owned Run.",
} satisfies Record<A1ToolName, string>;

export interface ReadToolGateway {
  invoke(
    request: DurableAgentTurnRequest,
    toolCallId: string,
    toolName: ReadToolName,
    arguments_: JsonObject,
    signal: AbortSignal,
  ): Promise<ToolResult>;
}

export function visibleReadToolNames(
  request: DurableAgentTurnRequest,
): readonly A1ToolName[] {
  const names: A1ToolName[] = [...PLATFORM_READ_TOOL_NAMES];
  if (request.input.context_refs.some((reference) => reference.startsWith("run:"))) {
    names.push(...RUN_READ_TOOL_NAMES);
  }
  if (
    request.input.context_refs.some((reference) => reference.startsWith("evidence:"))
  ) {
    names.push("evidence_read");
  }
  return names;
}

export function createReadOnlyTools(
  request: DurableAgentTurnRequest,
  gateway: ReadToolGateway,
): AgentTool[] {
  return visibleReadToolNames(request).map((name) => ({
    name,
    label: name,
    description: TOOL_DESCRIPTIONS[name],
    parameters: ARGUMENT_SCHEMAS[name],
    executionMode: "sequential",
    execute: async (toolCallId, params, signal) => {
      const result = await gateway.invoke(
        request,
        toolCallId,
        name,
        params as JsonObject,
        signal ?? new AbortController().signal,
      );
      if (result.error !== null || result.result === null) {
        throw new Error("The read tool request failed.");
      }
      const details = {
        result: structuredClone(result.result),
        evidence_refs: [...result.evidence_refs],
        bytes_returned: result.bytes_returned,
      };
      return {
        content: [{ type: "text", text: JSON.stringify(result.result) }],
        details,
      };
    },
  }));
}
