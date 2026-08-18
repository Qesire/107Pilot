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

const BoundedId = Type.String({ minLength: 1, maxLength: 4_096 });
const Workspace = Type.String({ minLength: 1, maxLength: 4_096 });

const ARGUMENT_SCHEMAS = {
  platform_get_snapshot: Type.Object({}, { additionalProperties: false }),
  workspace_list: Type.Object(
    { workspace: Workspace },
    { additionalProperties: false },
  ),
  workspace_search: Type.Object(
    {
      workspace: Workspace,
      query: Type.String({ minLength: 1, maxLength: 256 }),
    },
    { additionalProperties: false },
  ),
  workspace_read: Type.Object(
    { workspace: Workspace, path: BoundedId },
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
} satisfies Record<ReadToolName, TSchema>;

const TOOL_DESCRIPTIONS = {
  platform_get_snapshot: "Read the latest safe platform snapshot for this owner.",
  workspace_list: "List bounded tracked paths in an authorized workspace.",
  workspace_search: "Search bounded text in an authorized workspace.",
  workspace_read: "Read bounded UTF-8 text from an authorized workspace file.",
  run_get: "Read the safe state and resource summary for one owned Run.",
  run_log_read: "Read a bounded stdout or stderr page for one owned Run.",
  evidence_read: "Read a bounded preview of one owned evidence object.",
} satisfies Record<ReadToolName, string>;

export interface ReadToolGateway {
  invoke(
    request: DurableAgentTurnRequest,
    toolCallId: string,
    toolName: ReadToolName,
    arguments_: JsonObject,
    signal: AbortSignal,
  ): Promise<ToolResult>;
}

export function createReadOnlyTools(
  request: DurableAgentTurnRequest,
  gateway: ReadToolGateway,
): AgentTool[] {
  return A1_READ_TOOL_NAMES.map((name) => ({
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
