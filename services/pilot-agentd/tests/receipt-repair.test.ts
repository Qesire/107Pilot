import { createHash } from "node:crypto";

import { describe, expect, it } from "vitest";

import type { EventWrite } from "../src/events.js";
import {
  applyReceiptRepairs,
  parseRepairableExecutableTurnRequest,
  ReceiptRepairingTurnRunner,
  type ToolReceiptRepair,
} from "../src/receipt-repair.js";
import type {
  AgentTurnEvent,
  ExecutableAgentTurnRequest,
} from "../src/protocol.js";
import type { TurnRunner } from "../src/server.js";
import { durableRequest, neverAbort } from "./support/fixtures.js";

function invocationId(turnId: string, toolCallId: string): string {
  return `inv-${createHash("sha256")
    .update(turnId)
    .update("\0")
    .update(toolCallId)
    .digest("hex")}`;
}

function repair(overrides: Partial<ToolReceiptRepair> = {}): ToolReceiptRepair {
  const expectedInvocation = invocationId("turn-1", "call-1");
  return {
    parent_checkpoint_digest: null,
    invocation_id: expectedInvocation,
    receipt_ref: `agent-tool-receipt:${expectedInvocation}:sha256:${"b".repeat(64)}`,
    tool_call_id: "call-1",
    tool_name: "platform_get_snapshot",
    arguments: {},
    assistant_text: "checking durable state",
    content: '{"ok":true}',
    details: {
      result: { ok: true },
      evidence_refs: ["evidence://snapshot"],
      bytes_returned: 12,
    },
    is_error: false,
    ...overrides,
  };
}

function repairableRequest(
  repairs: readonly ToolReceiptRepair[] = [repair()],
): ExecutableAgentTurnRequest {
  return parseRepairableExecutableTurnRequest({
    ...durableRequest(),
    schema_version: "pilot107.agent-turn-request/v3",
    receipt_repairs: repairs,
  });
}

function startedEvent(request: ExecutableAgentTurnRequest): AgentTurnEvent {
  return {
    schema_version: "pilot107.agent-turn-event/v1",
    turn_id: request.turn_id,
    sequence: 1,
    timestamp: "2026-09-04T12:00:00.000Z",
    type: "turn_started",
    payload: {
      model_profile_id: request.model_profile_id,
      task_kind: request.task_kind,
    },
  };
}

class RecordingRunner implements TurnRunner {
  readonly requests: ExecutableAgentTurnRequest[] = [];
  starts = 0;

  async execute(
    request: ExecutableAgentTurnRequest,
    write: EventWrite,
    signal: AbortSignal,
  ): Promise<void> {
    expect(signal.aborted).toBe(false);
    this.starts += 1;
    this.requests.push(request);
    await write(startedEvent(request));
  }
}

describe("ReceiptRepairingTurnRunner", () => {
  it("commits a repaired checkpoint before resumed executor events", async () => {
    const base = new RecordingRunner();
    const runner = new ReceiptRepairingTurnRunner(base);
    const events: AgentTurnEvent[] = [];

    await runner.execute(
      repairableRequest(),
      (event) => {
        events.push(event);
      },
      neverAbort,
    );

    expect(base.starts).toBe(1);
    expect(events.map((event) => [event.type, event.sequence])).toEqual([
      ["checkpoint", 1],
      ["turn_started", 2],
    ]);
    const resumed = base.requests[0];
    expect(resumed?.checkpoint).not.toBeNull();
    expect(resumed?.checkpoint?.completed_tools).toHaveLength(1);
    expect(resumed?.checkpoint?.completed_tools[0]?.tool_call_id).toBe("call-1");
  });

  it("does not start the base executor when repaired checkpoint commit fails", async () => {
    const base = new RecordingRunner();
    const runner = new ReceiptRepairingTurnRunner(base);

    await expect(
      runner.execute(
        repairableRequest(),
        async (event) => {
          expect(event.type).toBe("checkpoint");
          throw new Error("durable checkpoint write failed");
        },
        neverAbort,
      ),
    ).rejects.toThrow("durable checkpoint write failed");
    expect(base.starts).toBe(0);
  });

  it("leaves ordinary v2 execution and local event sequence unchanged", async () => {
    const base = new RecordingRunner();
    const runner = new ReceiptRepairingTurnRunner(base);
    const events: AgentTurnEvent[] = [];
    const request = parseRepairableExecutableTurnRequest(durableRequest());

    await runner.execute(request, (event) => events.push(event), neverAbort);

    expect(base.starts).toBe(1);
    expect(base.requests[0]?.checkpoint).toBeNull();
    expect(events.map((event) => [event.type, event.sequence])).toEqual([
      ["turn_started", 1],
    ]);
  });

  it("rejects a receipt whose invocation identity is not bound to the Turn", () => {
    const request = durableRequest();

    expect(() =>
      applyReceiptRepairs(request, [
        repair({
          invocation_id: `inv-${"f".repeat(64)}`,
        }),
      ]),
    ).toThrow("does not match the Turn");
  });
});
