import { createHash, timingSafeEqual } from "node:crypto";

import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type {
  Api,
  AssistantMessage,
  Message,
  Model,
  ToolCall,
  Usage,
} from "@earendil-works/pi-ai";
import { Value } from "typebox/value";

import {
  AgentCheckpointSchema,
  parseCheckpoint,
  type AgentCheckpoint,
  type AgentTurnRequest,
  type JsonValue,
} from "./protocol.js";
import { CHECKPOINT_PROTOCOL_VERSION } from "./version.js";

const DEFAULT_MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024;
const MAX_TEXT_LENGTH = 256_000;
const MAX_COLLECTION_SIZE = 4_096;
const SECRET_KEY = /^(?:headers?|(?:x[-_])?api[-_]?key|authorization|(?:access[-_]?|refresh[-_]?|auth[-_]?)?token|password)$/i;
const ZERO_USAGE: Usage = {
  input: 0,
  output: 0,
  cacheRead: 0,
  cacheWrite: 0,
  totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

export interface CheckpointableAgentState {
  readonly messages: readonly AgentMessage[];
}

export interface CheckpointExpectation {
  readonly turn_id: string;
  readonly model_profile_id: string;
  readonly prompt_profile_id: string;
}

export interface RestoreCheckpointOptions {
  readonly maxBytes?: number;
  readonly model?: Pick<Model<Api>, "api" | "provider" | "id">;
}

export function checkpointFromState(
  request: AgentTurnRequest,
  state: CheckpointableAgentState,
): AgentCheckpoint {
  const previous =
    request.checkpoint === null
      ? undefined
      : verifyCheckpoint(request.checkpoint, request, DEFAULT_MAX_CHECKPOINT_BYTES);
  const lineage = previous === undefined
    ? []
    : previous.turn_id === request.turn_id
      ? [...previous.lineage]
      : [...previous.lineage, previous.turn_id];
  if (lineage.length > 256 || new Set(lineage).size !== lineage.length) {
    throw new TypeError("checkpoint lineage is invalid");
  }
  if (lineage.includes(request.turn_id)) {
    throw new TypeError("checkpoint lineage would contain the current turn");
  }

  const normalized = normalizeMessages(state.messages);
  const body = {
    schema_version: CHECKPOINT_PROTOCOL_VERSION,
    turn_id: request.turn_id,
    lineage,
    model_profile_id: request.model_profile_id,
    prompt_profile_id: request.prompt_profile_id,
    messages: normalized.messages,
    completed_tools: normalized.completedTools,
    usage: aggregateUsage(state.messages),
  };
  const checkpoint = {
    ...body,
    digest: digestBody(body),
  } as AgentCheckpoint;
  if (!Value.Check(AgentCheckpointSchema, checkpoint)) {
    throw new TypeError("generated checkpoint does not satisfy the wire schema");
  }
  ensureSize(checkpoint, DEFAULT_MAX_CHECKPOINT_BYTES);
  return checkpoint;
}

export function computeCheckpointDigest(checkpoint: AgentCheckpoint): string {
  const { digest: _digest, ...body } = checkpoint;
  return digestBody(body);
}

export function restoreMessages(
  checkpoint: unknown,
  expected: CheckpointExpectation,
  options: RestoreCheckpointOptions = {},
): Message[] {
  if (checkpoint === null || checkpoint === undefined) return [];
  const verified = verifyCheckpoint(
    checkpoint,
    expected,
    options.maxBytes ?? DEFAULT_MAX_CHECKPOINT_BYTES,
  );
  const completedById = new Map(
    verified.completed_tools.map((tool) => [tool.tool_call_id, tool] as const),
  );
  const restored: Message[] = [];
  const restoredToolIds = new Set<string>();

  for (const message of verified.messages) {
    switch (message.role) {
      case "user":
        restored.push({ role: "user", content: message.content, timestamp: 0 });
        break;
      case "assistant":
        restored.push(
          restoredAssistant(
            [{ type: "text", text: message.content }],
            verified,
            options.model,
          ),
        );
        break;
      case "tool_result": {
        const toolCallId = message.tool_call_id;
        const toolName = message.tool_name;
        if (toolCallId === null || toolName === null || message.is_error === null) {
          throw new TypeError("checkpoint tool_result linkage is incomplete");
        }
        const completed = completedById.get(toolCallId);
        if (
          completed === undefined ||
          completed.tool_name !== toolName ||
          completed.is_error !== message.is_error ||
          restoredToolIds.has(toolCallId)
        ) {
          throw new TypeError("checkpoint completed tool linkage is invalid");
        }
        restoredToolIds.add(toolCallId);
        restored.push(
          restoredAssistant(
            [
              {
                type: "toolCall",
                id: completed.tool_call_id,
                name: completed.tool_name,
                arguments: structuredClone(completed.arguments),
              },
            ],
            verified,
            options.model,
            "toolUse",
          ),
        );
        restored.push({
          role: "toolResult",
          toolCallId,
          toolName,
          content: [{ type: "text", text: message.content }],
          details: structuredClone(completed.result),
          isError: message.is_error,
          timestamp: 0,
        });
        break;
      }
    }
  }
  if (restoredToolIds.size !== verified.completed_tools.length) {
    throw new TypeError("checkpoint completed tool has no matching tool_result");
  }
  return restored;
}

export function sanitizeJsonValue(value: unknown): JsonValue {
  return sanitizeValue(value, new Set<object>());
}

export function sanitizePublicText(text: string): string {
  return text
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [REDACTED]")
    .replace(/https?:\/\/[^\s<>'"\])}]+/gi, stripUrlSecrets)
    .replace(
      /\b((?:api[-_]?key|authorization|(?:access[-_]?|refresh[-_]?|auth[-_]?)?token|password)\s*[:=]\s*)[^\s,;]+/gi,
      "$1[REDACTED]",
    )
    .slice(0, MAX_TEXT_LENGTH);
}

function verifyCheckpoint(
  value: unknown,
  expected: CheckpointExpectation,
  maxBytes: number,
): AgentCheckpoint {
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) {
    throw new RangeError("checkpoint size limit must be a positive integer");
  }
  ensureSize(value, maxBytes);
  const checkpoint = parseCheckpoint(value);
  const computed = computeCheckpointDigest(checkpoint);
  if (!sameDigest(checkpoint.digest, computed)) {
    throw new TypeError("checkpoint digest does not match its contents");
  }
  if (checkpoint.model_profile_id !== expected.model_profile_id) {
    throw new TypeError("checkpoint model profile does not match the request");
  }
  if (checkpoint.prompt_profile_id !== expected.prompt_profile_id) {
    throw new TypeError("checkpoint prompt profile does not match the request");
  }
  const uniqueLineage = new Set(checkpoint.lineage);
  if (
    uniqueLineage.size !== checkpoint.lineage.length ||
    uniqueLineage.has(checkpoint.turn_id) ||
    (checkpoint.turn_id !== expected.turn_id && uniqueLineage.has(expected.turn_id))
  ) {
    throw new TypeError("checkpoint lineage is invalid for the request turn");
  }
  return checkpoint;
}

function ensureSize(value: unknown, maxBytes: number): void {
  let serialized: string;
  try {
    serialized = JSON.stringify(value);
  } catch {
    throw new TypeError("checkpoint cannot be serialized");
  }
  if (serialized === undefined || Buffer.byteLength(serialized, "utf8") > maxBytes) {
    throw new RangeError("checkpoint exceeds the serialized size limit");
  }
}

function normalizeMessages(messages: readonly AgentMessage[]): {
  messages: AgentCheckpoint["messages"];
  completedTools: AgentCheckpoint["completed_tools"];
} {
  const normalizedMessages: AgentCheckpoint["messages"] = [];
  const completedTools: AgentCheckpoint["completed_tools"] = [];
  const toolCalls = new Map<string, ToolCall>();
  const completedToolIds = new Set<string>();

  for (const message of messages) {
    if (!isStandardMessage(message)) continue;
    switch (message.role) {
      case "user":
        normalizedMessages.push({
          role: "user",
          content: sanitizePublicText(textContent(message.content)),
          tool_call_id: null,
          tool_name: null,
          is_error: null,
        });
        break;
      case "assistant": {
        const publicText = message.content
          .filter((content) => content.type === "text")
          .map((content) => content.text)
          .join("");
        normalizedMessages.push({
          role: "assistant",
          content: sanitizePublicText(publicText),
          tool_call_id: null,
          tool_name: null,
          is_error: null,
        });
        for (const content of message.content) {
          if (content.type !== "toolCall") continue;
          if (toolCalls.has(content.id)) {
            throw new TypeError("checkpoint contains a duplicate tool call id");
          }
          toolCalls.set(content.id, content);
        }
        break;
      }
      case "toolResult": {
        normalizedMessages.push({
          role: "tool_result",
          content: sanitizePublicText(textContent(message.content)),
          tool_call_id: message.toolCallId,
          tool_name: message.toolName,
          is_error: message.isError,
        });
        const call = toolCalls.get(message.toolCallId);
        if (
          call === undefined ||
          call.name !== message.toolName ||
          completedToolIds.has(message.toolCallId)
        ) {
          throw new TypeError("checkpoint contains an orphan or mismatched tool result");
        }
        completedToolIds.add(message.toolCallId);
        completedTools.push({
          tool_call_id: call.id,
          tool_name: call.name,
          arguments: objectValue(call.arguments),
          result: sanitizeJsonValue(
            message.details === undefined
              ? textContent(message.content)
              : message.details,
          ),
          is_error: message.isError,
        });
        break;
      }
    }
  }
  return { messages: normalizedMessages, completedTools };
}

function aggregateUsage(messages: readonly AgentMessage[]): AgentCheckpoint["usage"] {
  let sawAssistant = false;
  let input = 0;
  let output = 0;
  let cacheRead = 0;
  let cacheWrite = 0;
  for (const message of messages) {
    if (!isStandardMessage(message) || message.role !== "assistant") continue;
    sawAssistant = true;
    input += message.usage.input;
    output += message.usage.output;
    cacheRead += message.usage.cacheRead;
    cacheWrite += message.usage.cacheWrite;
  }
  return {
    input_tokens: sawAssistant ? input : null,
    output_tokens: sawAssistant ? output : null,
    cache_read_tokens: sawAssistant ? cacheRead : null,
    cache_write_tokens: sawAssistant ? cacheWrite : null,
  };
}

function restoredAssistant(
  content: AssistantMessage["content"],
  checkpoint: AgentCheckpoint,
  model: RestoreCheckpointOptions["model"],
  stopReason: AssistantMessage["stopReason"] = "stop",
): AssistantMessage {
  return {
    role: "assistant",
    content,
    api: model?.api ?? "pilot107-checkpoint",
    provider: model?.provider ?? checkpoint.model_profile_id,
    model: model?.id ?? checkpoint.model_profile_id,
    usage: structuredClone(ZERO_USAGE),
    stopReason,
    timestamp: 0,
  };
}

function digestBody(body: object): string {
  return createHash("sha256").update(canonicalJson(body)).digest("hex");
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value) ?? "null";
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, nested]) => nested !== undefined)
    .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
  return `{${entries
    .map(([key, nested]) => `${JSON.stringify(key)}:${canonicalJson(nested)}`)
    .join(",")}}`;
}

function sameDigest(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left, "hex");
  const rightBytes = Buffer.from(right, "hex");
  return leftBytes.length === rightBytes.length && timingSafeEqual(leftBytes, rightBytes);
}

function sanitizeValue(value: unknown, seen: Set<object>): JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "number") {
    return typeof value === "number" && !Number.isFinite(value) ? null : value;
  }
  if (typeof value === "string") return sanitizePublicText(value);
  if (typeof value !== "object") return null;
  if (seen.has(value)) return "[Circular]";
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      return value
        .slice(0, MAX_COLLECTION_SIZE)
        .map((item) => sanitizeValue(item, seen));
    }
    const result: Record<string, JsonValue> = {};
    for (const [key, nested] of Object.entries(value).slice(0, MAX_COLLECTION_SIZE)) {
      if (SECRET_KEY.test(key)) continue;
      result[key.slice(0, 4_096)] = sanitizeValue(nested, seen);
    }
    return result;
  } finally {
    seen.delete(value);
  }
}

function stripUrlSecrets(raw: string): string {
  try {
    const url = new URL(raw);
    url.search = "";
    url.hash = "";
    return url.toString();
  } catch {
    return "[REDACTED_URL]";
  }
}

function objectValue(value: unknown): Record<string, JsonValue> {
  const sanitized = sanitizeJsonValue(value);
  return sanitized !== null && typeof sanitized === "object" && !Array.isArray(sanitized)
    ? sanitized
    : {};
}

function textContent(
  content: string | readonly { readonly type: string; readonly text?: string }[],
): string {
  if (typeof content === "string") return content;
  return content
    .filter((item) => item.type === "text" && typeof item.text === "string")
    .map((item) => item.text as string)
    .join("");
}

function isStandardMessage(message: AgentMessage): message is Message {
  if (message === null || typeof message !== "object" || !("role" in message)) {
    return false;
  }
  return (
    message.role === "user" ||
    message.role === "assistant" ||
    message.role === "toolResult"
  );
}
