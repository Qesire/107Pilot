import { describe, expect, it } from "vitest";
import {
  agentEventText,
  agentSessionPermissionCopy,
  agentTaskKindLabel,
  groupAgentEvents,
  mergeAgentEvents,
  readonlyConversationSessions,
} from "./AgentSessionPanel";
import type { AgentSession, AgentTurnEvent } from "./types";

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

function session(profileId: AgentSession["profile_id"], id: string): AgentSession {
  return {
    session_id: id,
    owner: "alice",
    request_key: `request-${id}`,
    profile_id: profileId,
    model_profile_id: "campus-default",
    source: {},
    state: "idle",
    state_version: 1,
    resource_usage: {},
    outcome: null,
    created_at: "2026-08-19T00:00:00Z",
    updated_at: "2026-08-19T00:00:00Z",
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

  it("never carries persisted events across a session switch", () => {
    const current = [event({ event_id: 4, session_id: "session-old" })];
    const incoming = [event({ event_id: 7, session_id: "session-new" })];

    expect(mergeAgentEvents(current, incoming, "session-new").map((item) => item.event_id))
      .toEqual([7]);
  });

  it("extracts readable text without rendering event metadata as a message", () => {
    expect(agentEventText(event({ payload: { delta: "排队原因是资源不足" } })))
      .toBe("排队原因是资源不足");
    expect(agentEventText(event({ event_type: "tool_started", payload: { tool_name: "run_get" } })))
      .toBeNull();
  });

  it("coalesces adjacent assistant deltas without losing meaningful spaces", () => {
    const groups = groupAgentEvents([
      event({ event_id: 1, sequence: 1, payload: { delta: "集群" } }),
      event({ event_id: 2, sequence: 2, payload: { delta: "共有" } }),
      event({ event_id: 3, sequence: 3, payload: { delta: " 6 CPU" } }),
    ]);

    expect(groups.filter((group) => group.kind === "assistant")).toHaveLength(1);
    expect(groups[0]?.text).toBe("集群共有 6 CPU");
    expect(groups[0]?.events.map((item) => item.event_id)).toEqual([1, 2, 3]);
  });

  it("keeps whitespace-only deltas in raw events but omits their visual group", () => {
    const raw = [event({ payload: { delta: "\n\n" } })];

    expect(raw).toHaveLength(1);
    expect(groupAgentEvents(raw)).toEqual([]);
  });

  it("groups one tool lifecycle by turn and tool call id", () => {
    const groups = groupAgentEvents([
      event({
        event_id: 1,
        sequence: 1,
        event_type: "tool_call_requested",
        payload: { tool_call_id: "call-1", tool_name: "platform_get_snapshot", arguments: {} },
      }),
      event({
        event_id: 2,
        sequence: 2,
        event_type: "tool_call_started",
        payload: { tool_call_id: "call-1", tool_name: "platform_get_snapshot" },
      }),
      event({
        event_id: 3,
        sequence: 3,
        event_type: "tool_call_completed",
        payload: {
          tool_call_id: "call-1",
          tool_name: "platform_get_snapshot",
          result: { snapshot_id: "platform-1" },
          is_error: false,
        },
      }),
    ]);

    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({ kind: "tool", key: "tool:turn-1:call-1" });
    expect(groups[0]?.events).toHaveLength(3);
  });

  it("projects a structured tool error message and the real task label", () => {
    const groups = groupAgentEvents([
      event({
        event_type: "tool_call_completed",
        payload: {
          tool_call_id: "call-1",
          tool_name: "platform_get_snapshot",
          result: {
            error: {
              code: "platform_facts_unavailable",
              message: "Authoritative VM Slurm facts are unavailable.",
              retryable: false,
            },
          },
          is_error: true,
        },
      }),
    ]);

    expect(groups[0]?.text).toBe("Authoritative VM Slurm facts are unavailable.");
    expect(agentTaskKindLabel("interactive_readonly")).toBe("平台只读 Turn");
    expect(agentTaskKindLabel("experiment_builder")).toBe("实验构建 Turn");
    expect(agentTaskKindLabel("run_diagnosis_repair")).toBe("诊断修复 Turn");
  });

  it("surfaces the Chinese approval summary while raw tool details stay folded", () => {
    expect(agentEventText(event({
      event_type: "tool_call_completed",
      payload: {
        tool_name: "builder_build_submit",
        result: {
          result: {
            approval_summary_zh: "将新增热传导脚本与结果清单，等待用户批准后执行验证。",
          },
        },
      },
    }))).toBe("将新增热传导脚本与结果清单，等待用户批准后执行验证。");
  });

  it("keeps Builder sessions out of the read-only conversation entry", () => {
    const sessions = [
      session("hpc-readonly-v1", "readonly"),
      session("platform_coach", "coach"),
      session("experiment_builder", "builder"),
      session("run_diagnosis_repair", "repair"),
    ];

    expect(readonlyConversationSessions(sessions).map((item) => item.session_id))
      .toEqual(["readonly", "coach"]);
    expect(agentSessionPermissionCopy("experiment_builder")).toMatchObject({
      composerLabel: "继续实验构建 Agent",
    });
  });
});
