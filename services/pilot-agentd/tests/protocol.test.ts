import { readFile } from "node:fs/promises";

import { Type } from "typebox";
import { Value } from "typebox/value";
import { describe, expect, it } from "vitest";

import {
  AgentCheckpointSchema,
  DurableAgentTurnRequestSchema,
  AgentTurnEventSchema,
  AgentTurnRequestSchema,
  ToolInvocationSchema,
  ToolResultSchema,
  assertTerminalInvariant,
  isTerminalEvent,
  parseCheckpoint,
  parseDurableTurnRequest,
  parseToolInvocation,
  parseToolResult,
  parseTurnRequest,
} from "../src/protocol.js";
import {
  contractPatchRequest,
  explainRequest,
  interactiveRequest,
  remediationRequest,
} from "./support/fixtures.js";

const schemaDirectory = new URL("../../../schemas/agent/v1/", import.meta.url);
const v2SchemaDirectory = new URL("../../../schemas/agent/v2/", import.meta.url);

function durableRequest() {
  return {
    schema_version: "pilot107.agent-turn-request/v2" as const,
    session_id: "session-1",
    turn_id: "turn-1",
    owner: "alice",
    state_version: 3,
    task_kind: "interactive_readonly" as const,
    model_profile_id: "faux-default",
    prompt_profile_id: "hpc-readonly-v1" as const,
    toolset_id: "a1-readonly" as const,
    input: {
      message: "why is run-1 pending?",
      context_refs: ["run:run-1"],
    },
    capability_token: "opaque.test.token",
    checkpoint: null,
    limits: { timeout_ms: 60_000, max_output_tokens: 1_200 },
    trace: { correlation_id: "turn-1" },
  };
}

function toolInvocation() {
  return {
    schema_version: "pilot107.agent-tool-invocation/v1" as const,
    invocation_id: "invocation-1",
    idempotency_key: "turn-1:call-1",
    owner: "alice",
    session_id: "session-1",
    turn_id: "turn-1",
    state_version: 3,
    profile_id: "hpc-readonly-v1" as const,
    tool_name: "run_get" as const,
    arguments: { run_id: "run-1" },
    deadline: "2026-08-14T12:00:00Z",
  };
}

function asJson(value: unknown): unknown {
  return JSON.parse(JSON.stringify(value));
}

function completed(sequence = 2) {
  return {
    schema_version: "pilot107.agent-turn-event/v1" as const,
    turn_id: "turn-1",
    sequence,
    timestamp: "2026-08-10T00:00:00.000Z",
    type: "turn_completed" as const,
    payload: {
      result: { text: "done" },
      provider: "faux",
      model: "faux-model",
      model_profile_id: "faux-default",
      usage: {
        input_tokens: null,
        output_tokens: null,
        cache_read_tokens: null,
        cache_write_tokens: null,
      },
      provider_calls: 1,
      checkpoint_digest: "a".repeat(64),
      duration_ms: 5,
    },
  };
}

function started(sequence = 1) {
  return {
    schema_version: "pilot107.agent-turn-event/v1" as const,
    turn_id: "turn-1",
    sequence,
    timestamp: "2026-08-10T00:00:00.000Z",
    type: "turn_started" as const,
    payload: {
      model_profile_id: "faux-default",
      task_kind: "interactive" as const,
    },
  };
}

describe("AgentTurnRequest", () => {
  it("accepts the exact v1 shape for every task kind", () => {
    for (const request of [
      interactiveRequest(),
      explainRequest(),
      contractPatchRequest(),
      remediationRequest(),
    ]) {
      expect(parseTurnRequest(request)).toEqual(request);
    }
  });

  it("rejects an unknown request field", () => {
    const request = interactiveRequest();
    expect(() => parseTurnRequest({ ...request, api_key: "secret" })).toThrow(
      /unknown field/i,
    );
  });

  it("rejects an unknown field inside a task input", () => {
    const request = interactiveRequest();
    expect(() =>
      parseTurnRequest({ ...request, input: { ...request.input, extra: true } }),
    ).toThrow(/unknown field/i);
  });

  it.each([
    "api_key",
    "authorization",
    "base_url",
    "system_prompt",
    "schema",
    "tools",
  ])("recursively rejects the %s injection key", (field) => {
    const request = contractPatchRequest();
    expect(() =>
      parseTurnRequest({
        ...request,
        input: {
          ...request.input,
          current_contract: { nested: [{ safe: { [field]: "injected" } }] },
        },
      }),
    ).toThrow(new RegExp(`forbidden input field.*${field}`, "i"));
  });

  it("rejects injection keys without relying on caller casing", () => {
    const request = contractPatchRequest();
    expect(() =>
      parseTurnRequest({
        ...request,
        input: {
          ...request.input,
          current_contract: { Authorization: "Bearer secret" },
        },
      }),
    ).toThrow(/forbidden input field.*Authorization/i);
  });

  it("the language-neutral schema rejects uppercase injection keys", () => {
    const request = contractPatchRequest();
    expect(
      Value.Check(AgentTurnRequestSchema, {
        ...request,
        input: {
          ...request.input,
          current_contract: { Authorization: "Bearer secret" },
        },
      }),
    ).toBe(false);
  });

  it.each([
    ["interactive", "agent-explain-v1", "a0-none"],
    ["interactive", "hpc-assistant-v1", "emit-explanation-v1"],
    ["explain", "hpc-assistant-v1", "emit-explanation-v1"],
    ["contract_patch", "contract-patch-v1", "a0-none"],
    ["remediation_plan", "agent-explain-v1", "emit-remediation-plan-v1"],
  ])("rejects the invalid %s/%s/%s pairing", (kind, profile, toolset) => {
    const source =
      kind === "interactive"
        ? interactiveRequest()
        : kind === "explain"
          ? explainRequest()
          : kind === "contract_patch"
            ? contractPatchRequest()
            : remediationRequest();
    expect(() =>
      parseTurnRequest({
        ...source,
        prompt_profile_id: profile,
        toolset_id: toolset,
      }),
    ).toThrow(/task.*profile.*toolset.*pairing/i);
  });

  it("rejects input belonging to a different task kind", () => {
    const interactive = interactiveRequest();
    const explain = explainRequest();
    expect(() =>
      parseTurnRequest({ ...interactive, input: explain.input }),
    ).toThrow(/invalid turn request/i);
  });
});

describe("DurableAgentTurnRequest", () => {
  it("accepts the exact A1 read-only envelope", () => {
    expect(parseDurableTurnRequest(durableRequest())).toEqual(durableRequest());
  });

  it("rejects unknown fields and any non-A1 pairing", () => {
    const request = durableRequest();
    expect(() => parseDurableTurnRequest({ ...request, extra: true })).toThrow(
      /unknown field/i,
    );
    expect(() =>
      parseDurableTurnRequest({ ...request, prompt_profile_id: "hpc-assistant-v1" }),
    ).toThrow(/invalid durable turn request/i);
    expect(() =>
      parseDurableTurnRequest({ ...request, toolset_id: "a0-none" }),
    ).toThrow(/invalid durable turn request/i);
  });

  it("does not widen the v1 request contract", () => {
    expect(() => parseTurnRequest(durableRequest())).toThrow(/invalid turn request/i);
    expect(Value.Check(AgentTurnRequestSchema, durableRequest())).toBe(false);
  });
});

describe("Tool Gateway envelopes", () => {
  it("accepts a closed invocation and exactly one result branch", () => {
    expect(parseToolInvocation(toolInvocation())).toEqual(toolInvocation());
    expect(
      parseToolResult({
        schema_version: "pilot107.agent-tool-result/v1",
        invocation_id: "invocation-1",
        result: { run_id: "run-1", state: "PENDING" },
        error: null,
        evidence_refs: ["run:run-1"],
        bytes_returned: 45,
      }),
    ).toEqual({
      schema_version: "pilot107.agent-tool-result/v1",
      invocation_id: "invocation-1",
      result: { run_id: "run-1", state: "PENDING" },
      error: null,
      evidence_refs: ["run:run-1"],
      bytes_returned: 45,
    });
  });

  it("rejects unknown tools, body authority, and ambiguous results", () => {
    expect(() =>
      parseToolInvocation({ ...toolInvocation(), tool_name: "shell_exec" }),
    ).toThrow(/invalid tool invocation/i);
    expect(() =>
      parseToolInvocation({ ...toolInvocation(), capability_token: "secret" }),
    ).toThrow(/unknown field/i);
    expect(() =>
      parseToolResult({
        schema_version: "pilot107.agent-tool-result/v1",
        invocation_id: "invocation-1",
        result: {},
        error: { code: "forbidden", message: "denied", retryable: false },
        evidence_refs: [],
        bytes_returned: 0,
      }),
    ).toThrow(/invalid tool result/i);
  });
});

describe("AgentCheckpoint", () => {
  const valid = {
    schema_version: "pilot107.agent-checkpoint/v1" as const,
    turn_id: "turn-1",
    lineage: ["turn-0", "turn-1"],
    model_profile_id: "faux-default",
    prompt_profile_id: "hpc-assistant-v1",
    messages: [
      {
        role: "user" as const,
        content: "hello",
        tool_call_id: null,
        tool_name: null,
        is_error: null,
      },
    ],
    completed_tools: [],
    usage: {
      input_tokens: 2,
      output_tokens: 1,
      cache_read_tokens: null,
      cache_write_tokens: null,
    },
    digest: "a".repeat(64),
  };

  it("accepts a safe normalized checkpoint", () => {
    expect(parseCheckpoint(valid)).toEqual(valid);
  });

  it("rejects unknown checkpoint fields", () => {
    expect(() => parseCheckpoint({ ...valid, authorization: "secret" })).toThrow(
      /unknown field/i,
    );
  });
});

describe("terminal event invariant", () => {
  it("recognizes only completed and failed as terminal", () => {
    expect(isTerminalEvent(completed())).toBe(true);
    expect(
      isTerminalEvent({ ...completed(), type: "turn_failed" } as never),
    ).toBe(true);
    expect(isTerminalEvent(started())).toBe(false);
  });

  it("requires one final terminal event after a contiguous sequence", () => {
    expect(assertTerminalInvariant([started(), completed()])).toEqual(completed());
    expect(() => assertTerminalInvariant([started()])).toThrow(/exactly one terminal/i);
    expect(() =>
      assertTerminalInvariant([started(), completed(), { ...started(), sequence: 3 }]),
    ).toThrow(/terminal event must be last/i);
    expect(() => assertTerminalInvariant([started(), completed(3)])).toThrow(
      /contiguous/i,
    );
  });

  it("rejects a terminal event from another turn", () => {
    expect(() =>
      assertTerminalInvariant([
        started(),
        { ...completed(), turn_id: "turn-other" },
      ]),
    ).toThrow(/one turn_id/i);
  });

  it("rejects an event with an invalid wire shape", () => {
    expect(() =>
      assertTerminalInvariant([
        started(),
        { ...completed(), schema_version: "pilot107.agent-turn-event/v2" } as never,
      ]),
    ).toThrow(/invalid turn event/i);
  });

  it("requires model and task metadata on turn_started", () => {
    expect(Value.Check(AgentTurnEventSchema, { ...started(), payload: {} })).toBe(false);
  });

  it("requires observability metadata on turn_completed", () => {
    expect(
      Value.Check(AgentTurnEventSchema, {
        ...completed(),
        payload: { result: { text: "done" } },
      }),
    ).toBe(false);
  });
});

describe("checked-in protocol schemas", () => {
  it.each([
    ["turn-request.schema.json", AgentTurnRequestSchema, interactiveRequest()],
    ["turn-event.schema.json", AgentTurnEventSchema, completed()],
    [
      "checkpoint.schema.json",
      AgentCheckpointSchema,
      {
        schema_version: "pilot107.agent-checkpoint/v1",
        turn_id: "turn-1",
        lineage: [],
        model_profile_id: "faux-default",
        prompt_profile_id: "hpc-assistant-v1",
        messages: [],
        completed_tools: [],
        usage: {
          input_tokens: null,
          output_tokens: null,
          cache_read_tokens: null,
          cache_write_tokens: null,
        },
        digest: "0".repeat(64),
      },
    ],
  ])("keeps %s semantically equal to its runtime schema", async (name, runtime, fixture) => {
    const file = JSON.parse(
      await readFile(new URL(name as string, schemaDirectory), "utf8"),
    );
    expect(file).toEqual(asJson(runtime));
    expect(file.$schema).toBe("https://json-schema.org/draft/2020-12/schema");
    expect(file.$id).toBe(`https://107pilot.local/schemas/agent/v1/${name}`);
    expect(Value.Check(file, fixture)).toBe(true);
    expect(Value.Check(file, { ...(fixture as object), unknown: true })).toBe(false);
  });

  it("the event schema rejects unknown payload fields", () => {
    expect(
      Value.Check(AgentTurnEventSchema, {
        ...completed(),
        payload: { result: {}, system_prompt: "inject" },
      }),
    ).toBe(false);
  });

  it("TypeBox 1.3.7 validates the runtime schema rather than a test double", () => {
    expect(Value.Check(AgentTurnRequestSchema, interactiveRequest())).toBe(true);
    expect(Value.Check(Type.Object({ ok: Type.Boolean() }), { ok: "not boolean" })).toBe(
      false,
    );
  });

  it.each([
    ["turn-request.schema.json", DurableAgentTurnRequestSchema, durableRequest()],
    ["tool-invocation.schema.json", ToolInvocationSchema, toolInvocation()],
    [
      "tool-result.schema.json",
      ToolResultSchema,
      {
        schema_version: "pilot107.agent-tool-result/v1",
        invocation_id: "invocation-1",
        result: { run_id: "run-1" },
        error: null,
        evidence_refs: ["run:run-1"],
        bytes_returned: 18,
      },
    ],
  ])("keeps v2/%s semantically equal to its runtime schema", async (name, runtime, fixture) => {
    const file = JSON.parse(
      await readFile(new URL(name as string, v2SchemaDirectory), "utf8"),
    );
    expect(file).toEqual(asJson(runtime));
    expect(file.$schema).toBe("https://json-schema.org/draft/2020-12/schema");
    expect(file.$id).toBe(`https://107pilot.local/schemas/agent/v2/${name}`);
    expect(Value.Check(file, fixture)).toBe(true);
    expect(Value.Check(file, { ...(fixture as object), unknown: true })).toBe(false);
  });
});
