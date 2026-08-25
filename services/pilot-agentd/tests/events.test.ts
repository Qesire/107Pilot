import type { AgentEvent } from "@earendil-works/pi-agent-core";
import { describe, expect, it } from "vitest";
import { Value } from "typebox/value";

import { AgentdTurnError } from "../src/errors.js";
import { mapPiEvent, TurnEventSink } from "../src/events.js";
import {
  AgentTurnEventSchema,
  assertTerminalInvariant,
  isTerminalEvent,
  type AgentTurnEvent,
} from "../src/protocol.js";

const fixedClock = () => new Date("2026-08-10T00:00:00.000Z");
const USAGE = {
  input_tokens: 7,
  output_tokens: 3,
  cache_read_tokens: null,
  cache_write_tokens: null,
};

function completionPayload() {
  return {
    result: { text: "hi" },
    provider: "faux-default",
    model: "faux-1",
    model_profile_id: "faux-default",
    usage: USAGE,
    provider_calls: 1,
    checkpoint_digest: "a".repeat(64),
    duration_ms: 12,
  };
}

describe("TurnEventSink", () => {
  it("writes schema-valid events with contiguous sequence and one terminal", async () => {
    const events: AgentTurnEvent[] = [];
    const sink = new TurnEventSink(
      "turn-1",
      (event) => {
        expect(Value.Check(AgentTurnEventSchema, event)).toBe(true);
        events.push(event);
      },
      fixedClock,
    );

    await sink.emit("turn_started", {
      model_profile_id: "faux-default",
      task_kind: "interactive",
    });
    await sink.emit("message_delta", { delta: "hi" });
    await sink.complete(completionPayload());

    await expect(
      sink.fail(new AgentdTurnError("internal_error", false, "late")),
    ).rejects.toThrow("already terminal");
    expect(events.map((event) => event.sequence)).toEqual([1, 2, 3]);
    expect(events.filter(isTerminalEvent)).toHaveLength(1);
    expect(assertTerminalInvariant(events)).toBe(events[2]);
  });

  it("rejects an invalid payload before calling the writer", async () => {
    const events: AgentTurnEvent[] = [];
    const sink = new TurnEventSink("turn-1", (event) => events.push(event), fixedClock);

    await expect(
      sink.emit("turn_started", {} as never),
    ).rejects.toThrow("invalid turn event");
    expect(events).toEqual([]);

    await sink.emit("turn_started", {
      model_profile_id: "faux-default",
      task_kind: "interactive",
    });
    expect(events[0]?.sequence).toBe(1);
  });

  it("commits terminal state and sequence only after the write succeeds", async () => {
    const attempts: AgentTurnEvent[] = [];
    let rejectFirstTerminal = true;
    const sink = new TurnEventSink(
      "turn-1",
      async (event) => {
        attempts.push(event);
        if (event.type === "turn_completed" && rejectFirstTerminal) {
          rejectFirstTerminal = false;
          throw new Error("socket backpressure failed");
        }
      },
      fixedClock,
    );

    await sink.emit("turn_started", {
      model_profile_id: "faux-default",
      task_kind: "interactive",
    });
    await expect(sink.complete(completionPayload())).rejects.toThrow(
      "socket backpressure failed",
    );
    await sink.complete(completionPayload());
    await expect(sink.emit("message_delta", { delta: "late" })).rejects.toThrow(
      "already terminal",
    );

    expect(attempts.map((event) => [event.type, event.sequence])).toEqual([
      ["turn_started", 1],
      ["turn_completed", 2],
      ["turn_completed", 2],
    ]);
  });

  it("serializes concurrent writes in invocation order", async () => {
    const events: AgentTurnEvent[] = [];
    const sink = new TurnEventSink(
      "turn-1",
      async (event) => {
        if (event.sequence === 1) await Promise.resolve();
        events.push(event);
      },
      fixedClock,
    );

    await Promise.all([
      sink.emit("turn_started", {
        model_profile_id: "faux-default",
        task_kind: "interactive",
      }),
      sink.emit("message_delta", { delta: "hi" }),
      sink.complete(completionPayload()),
    ]);

    expect(events.map((event) => event.sequence)).toEqual([1, 2, 3]);
    expect(events.map((event) => event.type)).toEqual([
      "turn_started",
      "message_delta",
      "turn_completed",
    ]);
  });
});

describe("Pi event normalization", () => {
  it("emits public text and tool lifecycle events while dropping thinking", async () => {
    const events: AgentTurnEvent[] = [];
    const sink = new TurnEventSink("turn-1", (event) => events.push(event), fixedClock);
    const partial = {
      role: "assistant" as const,
      content: [],
      api: "faux",
      provider: "faux-default",
      model: "faux-1",
      usage: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
      },
      stopReason: "pending" as const,
      timestamp: 1,
    };
    const piEvents: AgentEvent[] = [
      {
        type: "message_update",
        message: partial,
        assistantMessageEvent: {
          type: "thinking_delta",
          contentIndex: 0,
          delta: "private chain of thought",
          partial,
        },
      },
      {
        type: "message_update",
        message: partial,
        assistantMessageEvent: {
          type: "text_delta",
          contentIndex: 1,
          delta: "public answer",
          partial,
        },
      },
      {
        type: "message_update",
        message: partial,
        assistantMessageEvent: {
          type: "toolcall_end",
          contentIndex: 2,
          toolCall: {
            type: "toolCall",
            id: "call-1",
            name: "emit_result",
            arguments: { answer: "safe", Authorization: "Bearer provider-secret" },
          },
          partial,
        },
      },
      {
        type: "tool_execution_start",
        toolCallId: "call-1",
        toolName: "emit_result",
        args: { answer: "safe" },
      },
      {
        type: "tool_execution_update",
        toolCallId: "call-1",
        toolName: "emit_result",
        args: { answer: "safe" },
        partialResult: { content: [{ type: "text", text: "working" }] },
      },
      {
        type: "tool_execution_end",
        toolCallId: "call-1",
        toolName: "emit_result",
        result: {
          content: [{ type: "text", text: "accepted" }],
          details: { accepted: true, token: "provider-secret" },
        },
        isError: false,
      },
    ];

    for (const event of piEvents) await mapPiEvent(event, sink);

    expect(events.map((event) => event.type)).toEqual([
      "message_delta",
      "tool_call_requested",
      "tool_call_started",
      "tool_call_progress",
      "tool_call_completed",
    ]);
    expect(events[0]?.payload).toEqual({ delta: "public answer" });
    expect(JSON.stringify(events)).not.toContain("private chain of thought");
    expect(JSON.stringify(events)).not.toContain("provider-secret");
    expect(events.every((event) => Value.Check(AgentTurnEventSchema, event))).toBe(true);
  });

  it("classifies a malformed provider tool event without calling the writer", async () => {
    const events: AgentTurnEvent[] = [];
    const sink = new TurnEventSink("turn-1", (event) => events.push(event), fixedClock);
    const malformed = {
      type: "message_update",
      message: {},
      assistantMessageEvent: {
        type: "toolcall_end",
        contentIndex: 0,
        toolCall: {
          type: "toolCall",
          id: "",
          name: "emit_result",
          arguments: "not-an-object",
        },
        partial: {},
      },
    } as unknown as AgentEvent;

    await expect(mapPiEvent(malformed, sink)).rejects.toMatchObject({
      code: "provider_invalid_response",
      retryable: false,
    });
    expect(events).toEqual([]);
  });

  it("uses readable text when Pi tool details are empty", async () => {
    const events: AgentTurnEvent[] = [];
    const sink = new TurnEventSink("turn-1", (event) => events.push(event), fixedClock);

    await mapPiEvent({
      type: "tool_execution_end",
      toolCallId: "call-1",
      toolName: "workspace_list",
      result: {
        content: [{ type: "text", text: "No Workspace is bound." }],
        details: {},
      },
      isError: true,
    }, sink);

    expect(events[0]).toMatchObject({
      type: "tool_call_completed",
      payload: {
        result: "No Workspace is bound.",
        is_error: true,
      },
    });
  });
});
