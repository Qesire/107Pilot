import { describe, expect, it } from "vitest";
import { Value } from "typebox/value";

import { createReadOnlyTools } from "../src/read-tools.js";
import { A1_READ_TOOL_NAMES, type ToolResult } from "../src/protocol.js";
import { durableRequest } from "./support/fixtures.js";

describe("A1 read tools", () => {
  it("registers the bounded sequential read tools with closed argument schemas", () => {
    const tools = createReadOnlyTools(durableRequest({
      input: {
        message: "inspect bound resources",
        context_refs: ["run:run-1", "evidence:run-1:object-1"],
      },
    }), {
      invoke: async () => successResult("unused"),
    });

    expect(tools.map((tool) => tool.name)).toEqual([
      "platform_get_snapshot",
      "platform_observation_get",
      "account_observation_get",
      "run_get",
      "run_log_read",
      "run_resources_get",
      "evidence_read",
    ] satisfies (typeof A1_READ_TOOL_NAMES)[number][]);
    expect(tools.every((tool) => tool.executionMode === "sequential")).toBe(true);

    const cases = [
      ["platform_get_snapshot", {}, true],
      ["platform_get_snapshot", { unknown: true }, false],
      ["platform_observation_get", { connection_id: "connection1" }, true],
      ["account_observation_get", { connection_id: "connection1" }, true],
      ["run_get", { run_id: "run-1" }, true],
      ["run_log_read", { run_id: "run-1", stream: "stderr", cursor: 0 }, true],
      ["run_log_read", { run_id: "run-1", stream: "combined", cursor: 0 }, false],
      ["run_log_read", { run_id: "run-1", stream: "stdout", cursor: -1 }, false],
      ["evidence_read", { run_id: "run-1", object_id: "object-1" }, true],
      ["run_resources_get", { run_id: "run-1" }, true],
    ] as const;

    for (const [name, value, expected] of cases) {
      const tool = tools.find((candidate) => candidate.name === name);
      if (tool === undefined) throw new Error(`missing test tool ${name}`);
      expect(Value.Check(tool.parameters, value), `${name} ${JSON.stringify(value)}`).toBe(
        expected,
      );
    }
  });

  it("delegates the validated call and exposes only the safe result envelope", async () => {
    const calls: unknown[][] = [];
    const request = durableRequest();
    const tools = createReadOnlyTools(request, {
      invoke: async (...args) => {
        calls.push(args);
        return successResult("invocation-from-gateway");
      },
    });
    const tool = tools.find((candidate) => candidate.name === "run_get");
    if (tool === undefined) throw new Error("missing run_get tool");
    const signal = new AbortController().signal;

    const result = await tool.execute("call-run-1", { run_id: "run-1" }, signal);

    expect(calls).toEqual([
      [request, "call-run-1", "run_get", { run_id: "run-1" }, signal],
    ]);
    expect(result).toEqual({
      content: [
        {
          type: "text",
          text: JSON.stringify({ run_id: "run-1", status: "FAILED" }),
        },
      ],
      details: {
        result: { run_id: "run-1", status: "FAILED" },
        evidence_refs: ["run:run-1"],
        bytes_returned: 38,
      },
    });
    expect(JSON.stringify(result)).not.toContain(request.capability_token);
    expect(JSON.stringify(result)).not.toContain("invocation-from-gateway");
  });

  it("preserves only the safe structured gateway failure", async () => {
    const tools = createReadOnlyTools(durableRequest(), {
      invoke: async () => ({
        schema_version: "pilot107.agent-tool-result/v1",
        invocation_id: "invocation-secret",
        result: null,
        error: {
          code: "run_not_bound",
          message: "No Run is bound to this Session.",
          retryable: false,
          stack: "private stack",
          authorization: "Bearer private-secret",
          body: { provider: "private" },
        },
        evidence_refs: [],
        bytes_returned: 0,
      }),
    });
    const tool = tools.find((candidate) => candidate.name === "run_get");
    if (tool === undefined) throw new Error("missing run_get tool");

    const result = await tool.execute("call-run-1", { run_id: "run-1" });

    expect(result).toEqual({
      content: [{ type: "text", text: "No Run is bound to this Session." }],
      details: {
        error: {
          code: "run_not_bound",
          message: "No Run is bound to this Session.",
          retryable: false,
        },
      },
    });
    expect(JSON.stringify(result)).not.toContain("private");
  });
});

function successResult(invocationId: string): ToolResult {
  return {
    schema_version: "pilot107.agent-tool-result/v1",
    invocation_id: invocationId,
    result: { run_id: "run-1", status: "FAILED" },
    error: null,
    evidence_refs: ["run:run-1"],
    bytes_returned: 38,
  };
}
