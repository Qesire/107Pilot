import { describe, expect, it } from "vitest";
import { agentEventText, mergeAgentEvents } from "./AgentSessionPanel";
import type { AgentTurnEvent } from "./types";

function event(overrides: Partial<AgentTurnEvent>): AgentTurnEvent {
  return {
    event_id: 1,
    turn_id: "turn-1",
    session_id: "session-1",
    sequence: 1,
    event_type: "message_delta",
    payload: { delta: "hello" },
    created_at: "2026-08-19T00:00:00Z",
    ...overrides,
  };
}

describe("durable Agent event replay", () => {
  it("deduplicates by global event id and keeps equal per-Turn sequences", () => {
    const current = [event({ event_id: 4, turn_id: "turn-1", sequence: 1 })];
    const incoming = [
      event({ event_id: 4, turn_id: "turn-1", sequence: 1, payload: { delta: "duplicate" } }),
      event({ event_id: 6, turn_id: "turn-2", sequence: 1, payload: { delta: "second turn" } }),
      event({ event_id: 5, turn_id: "turn-1", sequence: 2, payload: { delta: "next" } }),
    ];

    expect(mergeAgentEvents(current, incoming).map((item) => item.event_id)).toEqual([4, 5, 6]);
    expect(mergeAgentEvents(current, incoming)[0]?.payload).toEqual({ delta: "hello" });
  });

  it("extracts readable text without rendering event metadata as a message", () => {
    expect(agentEventText(event({ payload: { delta: "排队原因是资源不足" } })))
      .toBe("排队原因是资源不足");
    expect(agentEventText(event({ event_type: "tool_started", payload: { tool_name: "run_get" } })))
      .toBeNull();
  });
});
