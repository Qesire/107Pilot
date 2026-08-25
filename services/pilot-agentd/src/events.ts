import type { AgentEvent } from "@earendil-works/pi-agent-core";
import { Value } from "typebox/value";

import { sanitizeJsonValue, sanitizePublicText } from "./checkpoint.js";
import { AgentdTurnError } from "./errors.js";
import {
  AgentTurnEventSchema,
  type AgentCheckpoint,
  type AgentTurnEvent,
  type JsonObject,
  type JsonValue,
  type TurnEventType,
} from "./protocol.js";
import { EVENT_PROTOCOL_VERSION } from "./version.js";

type CompletedEvent = Extract<AgentTurnEvent, { type: "turn_completed" }>;
type FailedEvent = Extract<AgentTurnEvent, { type: "turn_failed" }>;
type NonTerminalEvent = Exclude<AgentTurnEvent, CompletedEvent | FailedEvent>;
type EventOfType<T extends TurnEventType> = Extract<AgentTurnEvent, { type: T }>;

export type EventWrite = (event: AgentTurnEvent) => void | Promise<void>;

export class TurnEventSink {
  #sequence = 0;
  #terminal = false;
  #tail: Promise<void> = Promise.resolve();

  constructor(
    private readonly turnId: string,
    private readonly write: EventWrite,
    private readonly now: () => Date = () => new Date(),
  ) {}

  emit<T extends NonTerminalEvent["type"]>(
    type: T,
    payload: EventOfType<T>["payload"],
  ): Promise<void> {
    return this.enqueue(() => this.writeNext(type, payload, false));
  }

  complete(payload: CompletedEvent["payload"]): Promise<void> {
    return this.enqueue(() => this.writeNext("turn_completed", payload, true));
  }

  fail(error: AgentdTurnError, checkpoint?: AgentCheckpoint): Promise<void> {
    const payload: FailedEvent["payload"] = {
      error: error.toPayload(),
      ...(checkpoint === undefined ? {} : { checkpoint }),
    };
    return this.enqueue(() => this.writeNext("turn_failed", payload, true));
  }

  private enqueue(operation: () => Promise<void>): Promise<void> {
    const result = this.#tail.then(operation);
    this.#tail = result.catch(() => undefined);
    return result;
  }

  private async writeNext<T extends TurnEventType>(
    type: T,
    payload: EventOfType<T>["payload"],
    terminal: boolean,
  ): Promise<void> {
    if (this.#terminal) throw new Error("turn event stream is already terminal");
    const sequence = this.#sequence + 1;
    const candidate = {
      schema_version: EVENT_PROTOCOL_VERSION,
      turn_id: this.turnId,
      sequence,
      timestamp: this.now().toISOString(),
      type,
      payload,
    } as AgentTurnEvent;
    if (!Value.Check(AgentTurnEventSchema, candidate)) {
      const first = Value.Errors(AgentTurnEventSchema, candidate)[0];
      throw new TypeError(
        `invalid turn event: ${first?.message ?? "schema mismatch"}${
          first?.instancePath ? ` at ${first.instancePath}` : ""
        }`,
      );
    }
    await this.write(candidate);
    this.#sequence = sequence;
    if (terminal) this.#terminal = true;
  }
}

export async function mapPiEvent(
  event: AgentEvent,
  sink: TurnEventSink,
): Promise<void> {
  if (event.type === "message_update") {
    const update = event.assistantMessageEvent;
    if (update.type === "text_delta") {
      if (typeof update.delta !== "string") throw invalidProviderEvent();
      const delta = sanitizePublicText(update.delta);
      if (delta !== "") await sink.emit("message_delta", { delta });
      return;
    }
    if (update.type === "toolcall_end") {
      const toolCall: unknown = update.toolCall;
      if (!isRecord(toolCall) || !isRecord(toolCall.arguments)) {
        throw invalidProviderEvent();
      }
      const toolCallId = providerId(toolCall.id);
      const toolName = providerId(toolCall.name);
      await sink.emit("tool_call_requested", {
        tool_call_id: toolCallId,
        tool_name: toolName,
        arguments: jsonObject(toolCall.arguments),
      });
    }
    return;
  }

  switch (event.type) {
    case "tool_execution_start":
      await sink.emit("tool_call_started", {
        tool_call_id: providerId(event.toolCallId),
        tool_name: providerId(event.toolName),
      });
      break;
    case "tool_execution_update":
      await sink.emit("tool_call_progress", {
        tool_call_id: providerId(event.toolCallId),
        tool_name: providerId(event.toolName),
        progress: progressText(event.partialResult),
      });
      break;
    case "tool_execution_end":
      if (typeof event.isError !== "boolean") throw invalidProviderEvent();
      await sink.emit("tool_call_completed", {
        tool_call_id: providerId(event.toolCallId),
        tool_name: providerId(event.toolName),
        result: resultValue(event.result),
        is_error: event.isError,
      });
      break;
    case "agent_start":
    case "agent_end":
    case "turn_start":
    case "turn_end":
    case "message_start":
    case "message_end":
      break;
  }
}

function providerId(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 128 ||
    !/^[A-Za-z0-9._:-]+$/.test(value)
  ) {
    throw invalidProviderEvent();
  }
  return value;
}

function invalidProviderEvent(): AgentdTurnError {
  return new AgentdTurnError(
    "provider_invalid_response",
    false,
    "The model provider returned a malformed event.",
  );
}

function jsonObject(value: unknown): JsonObject {
  const sanitized = sanitizeJsonValue(value);
  return sanitized !== null && typeof sanitized === "object" && !Array.isArray(sanitized)
    ? sanitized
    : {};
}

function resultValue(result: unknown): JsonValue {
  if (isRecord(result) && "details" in result && result.details !== undefined) {
    const details = sanitizeJsonValue(result.details);
    if (hasObjectFields(details)) return details;
  }
  if (isRecord(result) && "content" in result) {
    return textContent(result.content);
  }
  return sanitizeJsonValue(result);
}

function hasObjectFields(value: JsonValue): value is Record<string, JsonValue> {
  return isRecord(value) && Object.keys(value).length > 0;
}

function textContent(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return sanitizePublicText(
    content
      .filter(
        (item): item is { type: "text"; text: string } =>
          isRecord(item) && item.type === "text" && typeof item.text === "string",
      )
      .map((item) => item.text)
      .join(""),
  );
}

function progressText(partialResult: unknown): string {
  if (isRecord(partialResult) && Array.isArray(partialResult.content)) {
    const text = partialResult.content
      .filter(
        (item): item is { type: "text"; text: string } =>
          isRecord(item) && item.type === "text" && typeof item.text === "string",
      )
      .map((item) => item.text)
      .join("");
    if (text !== "") return sanitizePublicText(text);
  }
  return sanitizePublicText(JSON.stringify(sanitizeJsonValue(partialResult))).slice(
    0,
    256_000,
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
