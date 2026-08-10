import { readFile } from "node:fs/promises";

import { Type } from "typebox";
import { Value } from "typebox/value";
import { describe, expect, it } from "vitest";

import {
  AgentCheckpointSchema,
  AgentTurnEventSchema,
  AgentTurnRequestSchema,
  assertTerminalInvariant,
  isTerminalEvent,
  parseCheckpoint,
  parseTurnRequest,
} from "../src/protocol.js";
import {
  contractPatchRequest,
  explainRequest,
  interactiveRequest,
  remediationRequest,
} from "./support/fixtures.js";

const schemaDirectory = new URL("../../../schemas/agent/v1/", import.meta.url);

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
});
