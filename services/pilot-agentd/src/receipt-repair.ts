import { createHash, timingSafeEqual } from "node:crypto";

import { Value } from "typebox/value";

import {
  AgentCheckpointSchema,
  parseExecutableTurnRequest,
  type AgentCheckpoint,
  type DurableAgentTurnRequest,
  type ExecutableAgentTurnRequest,
  type JsonObject,
  type JsonValue,
} from "./protocol.js";
import {
  computeCheckpointDigest,
  sanitizeJsonValue,
  sanitizePublicText,
} from "./checkpoint.js";
import type { EventWrite } from "./events.js";
import type { TurnRunner } from "./server.js";

const REPAIR_PROTOCOL_VERSION = "pilot107.agent-turn-request/v3";
const DURABLE_PROTOCOL_VERSION = "pilot107.agent-turn-request/v2";
const MAX_REPAIRS = 256;
const MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024;
const RESUME_PROMPT =
  "从已清理的 checkpoint 继续被中断的 Turn。" +
  "不要重复 checkpoint 中已经存在的文本。";
const FORBIDDEN_JSON_KEY = /^(?:api_key|authorization|base_url|system_prompt|schema|tools)$/i;
const ID = /^[A-Za-z0-9._:-]{1,128}$/;
const DIGEST = /^[a-f0-9]{64}$/;

export interface ToolReceiptRepair {
  readonly parent_checkpoint_digest: string | null;
  readonly invocation_id: string;
  readonly receipt_ref: string;
  readonly tool_call_id: string;
  readonly tool_name: string;
  readonly arguments: JsonObject;
  readonly assistant_text: string;
  readonly content: string;
  readonly details: JsonObject;
  readonly is_error: false;
}

const repairsByRequest = new WeakMap<object, readonly ToolReceiptRepair[]>();

/**
 * Parse the AC3 recovery envelope without widening the executable Turn
 * contract. A v3 request is validated here, reduced to the existing closed v2
 * request, and associated with its repair delta only for the outer runner.
 */
export function parseRepairableExecutableTurnRequest(
  value: unknown,
): ExecutableAgentTurnRequest {
  if (!isRecord(value) || value.schema_version !== REPAIR_PROTOCOL_VERSION) {
    return parseExecutableTurnRequest(value);
  }
  const repairs = parseRepairs(value.receipt_repairs);
  const base: Record<string, unknown> = { ...value };
  base.schema_version = DURABLE_PROTOCOL_VERSION;
  delete base.receipt_repairs;
  const request = parseExecutableTurnRequest(base);
  if (!isDurableRequest(request) || repairs.length === 0) {
    throw new TypeError("receipt repair envelope must contain a durable repair request");
  }
  repairsByRequest.set(request, repairs);
  return request;
}

export class ReceiptRepairingTurnRunner implements TurnRunner {
  constructor(private readonly base: TurnRunner) {}

  async execute(
    request: ExecutableAgentTurnRequest,
    write: EventWrite,
    signal: AbortSignal,
  ): Promise<void> {
    const repairs = repairsByRequest.get(request);
    if (repairs === undefined || repairs.length === 0) {
      await this.base.execute(request, write, signal);
      return;
    }
    if (!isDurableRequest(request)) {
      throw new TypeError("receipt repairs cannot be applied to a legacy Turn request");
    }
    const checkpoint = applyReceiptRepairs(request, repairs);
    repairsByRequest.delete(request);
    await this.base.execute({ ...request, checkpoint }, write, signal);
  }
}

export function applyReceiptRepairs(
  request: DurableAgentTurnRequest,
  repairs: readonly ToolReceiptRepair[],
): AgentCheckpoint {
  if (repairs.length < 1 || repairs.length > MAX_REPAIRS) {
    throw new TypeError("receipt repair count is invalid");
  }
  const parent = request.checkpoint;
  if (parent !== null) verifyCheckpoint(parent, request);
  const parentDigest = parent?.digest ?? null;
  const existingCompleted = new Set(
    parent?.completed_tools.map((tool) => tool.tool_call_id) ?? [],
  );
  const seenCalls = new Set<string>();
  const seenInvocations = new Set<string>();
  const seenReceipts = new Set<string>();

  for (const repair of repairs) {
    if (repair.parent_checkpoint_digest !== parentDigest) {
      throw new TypeError("receipt repair parent checkpoint digest is stale");
    }
    if (
      existingCompleted.has(repair.tool_call_id)
      || seenCalls.has(repair.tool_call_id)
      || seenInvocations.has(repair.invocation_id)
      || seenReceipts.has(repair.receipt_ref)
    ) {
      throw new TypeError("receipt repair identity is duplicated");
    }
    const expectedInvocation = invocationId(request.turn_id, repair.tool_call_id);
    if (repair.invocation_id !== expectedInvocation) {
      throw new TypeError("receipt repair invocation identity does not match the Turn");
    }
    const expectedReceiptPrefix = `agent-tool-receipt:${expectedInvocation}:sha256:`;
    if (
      !repair.receipt_ref.startsWith(expectedReceiptPrefix)
      || !DIGEST.test(repair.receipt_ref.slice(expectedReceiptPrefix.length))
    ) {
      throw new TypeError("receipt repair reference does not bind the invocation");
    }
    seenCalls.add(repair.tool_call_id);
    seenInvocations.add(repair.invocation_id);
    seenReceipts.add(repair.receipt_ref);
  }

  const lineage = parent === null
    ? []
    : parent.turn_id === request.turn_id
      ? [...parent.lineage]
      : [...parent.lineage, parent.turn_id];
  if (
    lineage.length > 256
    || new Set(lineage).size !== lineage.length
    || lineage.includes(request.turn_id)
  ) {
    throw new TypeError("repaired checkpoint lineage is invalid");
  }

  const messages: AgentCheckpoint["messages"] = parent === null
    ? []
    : structuredClone(parent.messages);
  const completedTools: AgentCheckpoint["completed_tools"] = parent === null
    ? []
    : structuredClone(parent.completed_tools);
  if (parent === null || parent.turn_id !== request.turn_id) {
    messages.push({
      role: "user",
      content: sanitizePublicText(JSON.stringify({ data: request.input })),
      tool_call_id: null,
      tool_name: null,
      is_error: null,
    });
  } else {
    messages.push({
      role: "user",
      content: RESUME_PROMPT,
      tool_call_id: null,
      tool_name: null,
      is_error: null,
    });
  }

  for (const repair of repairs) {
    const arguments_ = objectValue(repair.arguments);
    const details = objectValue(repair.details);
    const publicResult = objectRecord(details.result);
    if (publicResult === undefined) {
      throw new TypeError("receipt repair details do not contain a result object");
    }
    messages.push({
      role: "assistant",
      content: sanitizePublicText(repair.assistant_text),
      tool_call_id: null,
      tool_name: null,
      is_error: null,
    });
    messages.push({
      role: "tool_result",
      content: sanitizePublicText(JSON.stringify(publicResult)),
      tool_call_id: repair.tool_call_id,
      tool_name: repair.tool_name,
      is_error: false,
    });
    completedTools.push({
      tool_call_id: repair.tool_call_id,
      tool_name: repair.tool_name,
      arguments: arguments_,
      result: details,
      is_error: false,
    });
  }

  const body = {
    schema_version: "pilot107.agent-checkpoint/v1" as const,
    turn_id: request.turn_id,
    lineage,
    model_profile_id: request.model_profile_id,
    prompt_profile_id: request.prompt_profile_id,
    messages,
    completed_tools: completedTools,
    usage: parent === null
      ? {
          input_tokens: null,
          output_tokens: null,
          cache_read_tokens: null,
          cache_write_tokens: null,
        }
      : structuredClone(parent.usage),
  };
  const candidate = {
    ...body,
    digest: "0".repeat(64),
  } as AgentCheckpoint;
  candidate.digest = computeCheckpointDigest(candidate);
  if (!Value.Check(AgentCheckpointSchema, candidate)) {
    throw new TypeError("repaired checkpoint does not satisfy the checkpoint schema");
  }
  if (Buffer.byteLength(JSON.stringify(candidate), "utf8") > MAX_CHECKPOINT_BYTES) {
    throw new RangeError("repaired checkpoint exceeds the serialized size limit");
  }
  return candidate;
}

function parseRepairs(value: unknown): readonly ToolReceiptRepair[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > MAX_REPAIRS) {
    throw new TypeError("receipt repair list is invalid");
  }
  const repairs: ToolReceiptRepair[] = [];
  for (const raw of value) repairs.push(parseRepair(raw));
  return repairs;
}

function parseRepair(value: unknown): ToolReceiptRepair {
  if (!isRecord(value)) throw new TypeError("receipt repair must be an object");
  const expectedKeys = new Set([
    "parent_checkpoint_digest",
    "invocation_id",
    "receipt_ref",
    "tool_call_id",
    "tool_name",
    "arguments",
    "assistant_text",
    "content",
    "details",
    "is_error",
  ]);
  const keys = Object.keys(value);
  if (keys.length !== expectedKeys.size || keys.some((key) => !expectedKeys.has(key))) {
    throw new TypeError("receipt repair does not match the closed schema");
  }
  const parent = value.parent_checkpoint_digest;
  if (parent !== null && (typeof parent !== "string" || !DIGEST.test(parent))) {
    throw new TypeError("receipt repair parent digest is invalid");
  }
  const invocationId = id(value.invocation_id, "invocation_id");
  const toolCallId = id(value.tool_call_id, "tool_call_id");
  const toolName = id(value.tool_name, "tool_name");
  const receiptRef = boundedText(value.receipt_ref, 1, 512, "receipt_ref");
  if (/\r|\n|\0/.test(receiptRef)) throw new TypeError("receipt repair reference is invalid");
  const assistantText = boundedText(value.assistant_text, 0, 256_000, "assistant_text");
  const content = boundedText(value.content, 0, 256_000, "content");
  if (value.is_error !== false) {
    throw new TypeError("AC3 receipt repair currently accepts only successful tool results");
  }
  const arguments_ = jsonObject(value.arguments, "arguments");
  const details = jsonObject(value.details, "details");
  const detailKeys = Object.keys(details).sort();
  if (detailKeys.join("\0") !== ["bytes_returned", "evidence_refs", "result"].join("\0")) {
    throw new TypeError("receipt repair details do not match normal tool details");
  }
  if (objectRecord(details.result) === undefined) {
    throw new TypeError("receipt repair result must be an object");
  }
  if (
    !Array.isArray(details.evidence_refs)
    || details.evidence_refs.length > 256
    || details.evidence_refs.some(
      (reference) => typeof reference !== "string" || reference.length < 1 || reference.length > 4_096,
    )
  ) {
    throw new TypeError("receipt repair evidence refs are invalid");
  }
  if (
    typeof details.bytes_returned !== "number"
    || !Number.isSafeInteger(details.bytes_returned)
    || details.bytes_returned < 0
    || details.bytes_returned > 1_048_576
  ) {
    throw new TypeError("receipt repair byte count is invalid");
  }
  return {
    parent_checkpoint_digest: parent,
    invocation_id: invocationId,
    receipt_ref: receiptRef,
    tool_call_id: toolCallId,
    tool_name: toolName,
    arguments: arguments_,
    assistant_text: assistantText,
    content,
    details,
    is_error: false,
  };
}

function verifyCheckpoint(
  checkpoint: AgentCheckpoint,
  request: DurableAgentTurnRequest,
): void {
  if (!Value.Check(AgentCheckpointSchema, checkpoint)) {
    throw new TypeError("parent checkpoint does not satisfy the checkpoint schema");
  }
  if (!sameDigest(checkpoint.digest, computeCheckpointDigest(checkpoint))) {
    throw new TypeError("parent checkpoint digest does not match its contents");
  }
  if (
    checkpoint.model_profile_id !== request.model_profile_id
    || checkpoint.prompt_profile_id !== request.prompt_profile_id
  ) {
    throw new TypeError("parent checkpoint profile does not match the request");
  }
}

function invocationId(turnId: string, toolCallId: string): string {
  const digest = createHash("sha256")
    .update(turnId)
    .update("\0")
    .update(toolCallId)
    .digest("hex");
  return `inv-${digest}`;
}

function sameDigest(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left, "hex");
  const rightBytes = Buffer.from(right, "hex");
  return leftBytes.length === rightBytes.length && timingSafeEqual(leftBytes, rightBytes);
}

function objectValue(value: unknown): JsonObject {
  const sanitized = sanitizeJsonValue(value);
  return objectRecord(sanitized) ?? {};
}

function objectRecord(value: unknown): JsonObject | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : undefined;
}

function jsonObject(value: unknown, label: string): JsonObject {
  if (!isRecord(value)) throw new TypeError(`receipt repair ${label} must be an object`);
  validateJson(value, new Set<object>());
  return structuredClone(value) as JsonObject;
}

function validateJson(value: unknown, seen: Set<object>): void {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    if (typeof value === "string" && value.length > 256_000) {
      throw new TypeError("receipt repair JSON string exceeds the limit");
    }
    return;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("receipt repair JSON number is not finite");
    return;
  }
  if (typeof value !== "object" || seen.has(value)) {
    throw new TypeError("receipt repair JSON value is invalid or cyclic");
  }
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      if (value.length > 4_096) throw new TypeError("receipt repair JSON array exceeds the limit");
      for (const nested of value) validateJson(nested, seen);
      return;
    }
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length > 4_096) throw new TypeError("receipt repair JSON object exceeds the limit");
    for (const [key, nested] of entries) {
      if (FORBIDDEN_JSON_KEY.test(key)) {
        throw new TypeError("receipt repair JSON contains a forbidden key");
      }
      validateJson(nested, seen);
    }
  } finally {
    seen.delete(value);
  }
}

function id(value: unknown, label: string): string {
  if (typeof value !== "string" || !ID.test(value)) {
    throw new TypeError(`receipt repair ${label} is invalid`);
  }
  return value;
}

function boundedText(
  value: unknown,
  minimum: number,
  maximum: number,
  label: string,
): string {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum) {
    throw new TypeError(`receipt repair ${label} is invalid`);
  }
  return value;
}

function isDurableRequest(
  request: ExecutableAgentTurnRequest,
): request is DurableAgentTurnRequest {
  return "session_id" in request && "capability_token" in request;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
