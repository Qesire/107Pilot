import {
  fauxAssistantMessage,
  fauxText,
  fauxToolCall,
} from "@earendil-works/pi-ai";
import { describe, expect, it } from "vitest";

import { configFromEnv } from "../src/config.js";
import { createAgentdExecutor } from "../src/main.js";
import { createFauxModelRuntime } from "../src/models.js";
import type {
  AgentTurnEvent,
  ToolInvocation,
} from "../src/protocol.js";
import { ToolGatewayClient } from "../src/tool-gateway.js";
import { TurnExecutor } from "../src/turn-executor.js";
import {
  durableRequest,
  terminal,
} from "./support/fixtures.js";

describe("durable readonly Turn", () => {
  it("wires the configured Gateway through the production executor factory", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("run_get", { run_id: "run-1" }, { id: "call-main" })],
        { stopReason: "toolUse", timestamp: 1 },
      ),
      fauxAssistantMessage([fauxText("factory path complete")], { timestamp: 2 }),
    ]);
    let calls = 0;
    const config = configFromEnv({
      NODE_ENV: "test",
      PILOT107_AGENTD_MODEL_PROFILE: "faux-default",
      PILOT107_AGENTD_TOOL_GATEWAY_URL:
        "http://gateway.invalid/internal/v1/agent-tools/invoke",
    });
    const executor = createAgentdExecutor(config, {
      runtime,
      toolGatewayFetch: async (_input, init) => {
        calls += 1;
        const invocation = JSON.parse(String(init?.body)) as ToolInvocation;
        return new Response(
          JSON.stringify({
            schema_version: "pilot107.agent-tool-result/v1",
            invocation_id: invocation.invocation_id,
            result: { run_id: "run-1", status: "FAILED" },
            error: null,
            evidence_refs: ["run:run-1"],
            bytes_returned: 38,
          }),
          {
            status: 200,
            headers: { "content-type": "application/json; charset=utf-8" },
          },
        );
      },
    });
    const events: AgentTurnEvent[] = [];

    await executor.execute(
      durableRequest(),
      (event) => events.push(event),
      new AbortController().signal,
    );

    expect(calls).toBe(1);
    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: { result: "factory path complete", provider_calls: 2 },
    });
  });

  it("calls a read tool and continues to a public answer without leaking authority", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("run_get", { run_id: "run-1" }, { id: "call-run-1" })],
        { stopReason: "toolUse", timestamp: 1 },
      ),
      fauxAssistantMessage(
        [fauxText("run-1 failed; inspect its stderr evidence next.")],
        { timestamp: 2 },
      ),
    ]);
    const invocations: ToolInvocation[] = [];
    const gatewayUrl =
      "http://gateway.invalid/internal/v1/agent-tools/invoke?gateway-secret=yes";
    const gateway = new ToolGatewayClient({
      url: gatewayUrl,
      now: () => Date.parse("2026-08-19T00:00:00.000Z"),
      fetch: async (_input, init) => {
        const invocation = JSON.parse(String(init?.body)) as ToolInvocation;
        invocations.push(invocation);
        return new Response(
          JSON.stringify({
            schema_version: "pilot107.agent-tool-result/v1",
            invocation_id: invocation.invocation_id,
            result: { run_id: "run-1", status: "FAILED" },
            error: null,
            evidence_refs: ["run:run-1"],
            bytes_returned: 38,
          }),
          {
            status: 200,
            headers: { "content-type": "application/json; charset=utf-8" },
          },
        );
      },
    });
    const executor = new TurnExecutor(
      () => runtime,
      async () => undefined,
      gateway,
    );
    const request = durableRequest({
      capability_token: "opaque-turn-capability-secret",
    });
    const events: AgentTurnEvent[] = [];

    await executor.execute(
      request,
      (event) => events.push(event),
      new AbortController().signal,
    );

    expect(invocations).toHaveLength(1);
    expect(invocations[0]).toMatchObject({
      owner: "alice",
      session_id: "session-1",
      turn_id: "turn-1",
      state_version: 7,
      profile_id: "hpc-readonly-v1",
      tool_name: "run_get",
      arguments: { run_id: "run-1" },
    });
    expect(events.slice(0, 4).map((event) => event.type)).toEqual([
      "turn_started",
      "tool_call_requested",
      "tool_call_started",
      "tool_call_completed",
    ]);
    expect(events[0]).toMatchObject({
      type: "turn_started",
      payload: { task_kind: "interactive_readonly" },
    });
    expect(events.slice(-2).map((event) => event.type)).toEqual([
      "checkpoint",
      "turn_completed",
    ]);
    expect(
      events
        .filter((event) => event.type === "message_delta")
        .map((event) => event.payload.delta)
        .join(""),
    ).toBe("run-1 failed; inspect its stderr evidence next.");
    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: {
        result: "run-1 failed; inspect its stderr evidence next.",
        provider_calls: 2,
      },
    });
    const serialized = JSON.stringify(events);
    expect(serialized).not.toContain(request.capability_token);
    expect(serialized).not.toContain(gatewayUrl);
    expect(serialized).not.toContain("authorization");
    expect(serialized).not.toContain("idempotency_key");
    expect(serialized).not.toContain(invocations[0]?.invocation_id ?? "missing");
  });

  it("fails a read-only tool loop after exactly four Pi steps", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses(
      Array.from({ length: 6 }, (_, index) =>
        fauxAssistantMessage(
          [
            fauxToolCall(
              "platform_get_snapshot",
              {},
              { id: `call-platform-${index + 1}` },
            ),
          ],
          { stopReason: "toolUse", timestamp: index + 1 },
        ),
      ),
    );
    const gateway = {
      invoke: async () => ({
        schema_version: "pilot107.agent-tool-result/v1" as const,
        invocation_id: "inv-platform",
        result: { authority_id: "vm-slurm", snapshot_id: "platform-1" },
        error: null,
        evidence_refs: ["platform-snapshot:platform-1"],
        bytes_returned: 80,
      }),
    };
    const target = new TurnExecutor(() => runtime, async () => undefined, gateway);
    const request = durableRequest({
      input: { message: "inspect the platform", context_refs: [] },
    });
    const events: AgentTurnEvent[] = [];

    await target.execute(
      request,
      (event) => events.push(event),
      new AbortController().signal,
    );

    expect(runtime.faux.state.callCount).toBe(4);
    expect(runtime.faux.getPendingResponseCount()).toBe(2);
    expect(events.filter((event) => event.type === "tool_call_completed")).toHaveLength(4);
    expect(events.filter((event) => event.type === "checkpoint")).toHaveLength(1);
    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: {
        error: {
          code: "tool_step_budget_exhausted",
          retryable: false,
        },
      },
    });
  });

  it("rejects whitespace-only terminal text after a tool call", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("platform_get_snapshot", {}, { id: "call-platform" })],
        { stopReason: "toolUse", timestamp: 1 },
      ),
      fauxAssistantMessage([fauxText("\n\n")], { timestamp: 2 }),
    ]);
    const gateway = {
      invoke: async () => ({
        schema_version: "pilot107.agent-tool-result/v1" as const,
        invocation_id: "inv-platform",
        result: { authority_id: "vm-slurm", snapshot_id: "platform-1" },
        error: null,
        evidence_refs: ["platform-snapshot:platform-1"],
        bytes_returned: 80,
      }),
    };
    const target = new TurnExecutor(() => runtime, async () => undefined, gateway);
    const events: AgentTurnEvent[] = [];

    await target.execute(
      durableRequest({
        input: { message: "inspect the platform", context_refs: [] },
      }),
      (event) => events.push(event),
      new AbortController().signal,
    );

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: {
        error: { code: "empty_provider_response", retryable: false },
      },
    });
  });

  it("allows a Project Turn to terminate at validation within twelve Pi steps", async () => {
    const runtime = createFauxModelRuntime();
    const responses = Array.from({ length: 11 }, (_, index) =>
      fauxAssistantMessage(
        [
          fauxToolCall(
            "project_get",
            { project_id: "project-1", workspace_id: "workspace-1" },
            { id: `call-project-${index + 1}` },
          ),
        ],
        { stopReason: "toolUse", timestamp: index + 1 },
      ),
    );
    responses.push(
      fauxAssistantMessage(
        [
          fauxToolCall(
            "validation_schedule",
            {
              project_id: "project-1",
              workspace_id: "workspace-1",
              request_key: "validation-1",
              cpus: 1,
              memory_mib: 512,
              gpus: 0,
              walltime_seconds: 300,
              tasks: 1,
              submissions: 1,
              script: "true\n",
              job_name: "validation",
            },
            { id: "call-validation" },
          ),
        ],
        { stopReason: "toolUse", timestamp: 12 },
      ),
    );
    runtime.faux.setResponses(responses);
    const gateway = {
      invoke: async () => ({
        schema_version: "pilot107.agent-tool-result/v1" as const,
        invocation_id: "inv-project",
        result: { ok: true, task_id: "task-1" },
        error: null,
        evidence_refs: [],
        bytes_returned: 32,
      }),
    };
    const target = new TurnExecutor(() => runtime, async () => undefined, gateway);
    const events: AgentTurnEvent[] = [];

    await target.execute(
      durableRequest({
        task_kind: "experiment_builder",
        prompt_profile_id: "experiment_builder",
        toolset_id: "a2-project",
        input: {
          message: "validate the bound Project",
          context_refs: ["workspace:workspace-1"],
        },
      }),
      (event) => events.push(event),
      new AbortController().signal,
    );

    expect(runtime.faux.state.callCount).toBe(12);
    expect(events[0]).toMatchObject({
      type: "turn_started",
      payload: { task_kind: "experiment_builder" },
    });
    expect(terminal(events)).toMatchObject({ type: "turn_completed" });
  });
});
