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

  it("keeps only a high emergency ceiling for a pathological read-only loop", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses(
      Array.from({ length: 65 }, (_, index) =>
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

    expect(runtime.faux.state.callCount).toBe(64);
    expect(runtime.faux.getPendingResponseCount()).toBe(1);
    expect(events.filter((event) => event.type === "tool_call_completed")).toHaveLength(64);
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

  it("allows a Project Turn to terminate at validation within twenty Pi steps", async () => {
    const runtime = createFauxModelRuntime();
    const responses = Array.from({ length: 19 }, (_, index) =>
      fauxAssistantMessage(
        [
          fauxToolCall(
            "project_get",
            {},
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
        { stopReason: "toolUse", timestamp: 20 },
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
          context_refs: ["project:project-1", "workspace:workspace-1"],
        },
      }),
      (event) => events.push(event),
      new AbortController().signal,
    );

    expect(runtime.faux.state.callCount).toBe(20);
    expect(events[0]).toMatchObject({
      type: "turn_started",
      payload: { task_kind: "experiment_builder" },
    });
    expect(terminal(events)).toMatchObject({ type: "turn_completed" });
  });

  it("completes the phase-aware Builder in two Pi steps", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("builder_context_get", {}, { id: "call-context" })],
        { stopReason: "toolUse", timestamp: 1 },
      ),
      fauxAssistantMessage(
        [fauxToolCall("builder_build_submit", builderSubmission("build-1"), {
          id: "call-submit",
        })],
        { stopReason: "toolUse", timestamp: 2 },
      ),
    ]);
    const called: string[] = [];
    const gateway = {
      invoke: async (_request, _callId, name: string) => {
        called.push(name);
        return {
          schema_version: "pilot107.agent-tool-result/v1" as const,
          invocation_id: `inv-${called.length}`,
          result: name === "builder_build_submit"
            ? { status: "scheduled", phase: "validation_scheduled", task_id: "task-1" }
            : { phase: "drafting", next_action: "builder_build_submit" },
          error: null,
          evidence_refs: [],
          bytes_returned: 64,
        };
      },
    };
    const target = new TurnExecutor(
      () => runtime,
      async () => undefined,
      gateway,
      true,
    );
    const events: AgentTurnEvent[] = [];

    await target.execute(
      builderTurnRequest(),
      (event) => events.push(event),
      new AbortController().signal,
    );

    expect(called).toEqual(["builder_context_get", "builder_build_submit"]);
    expect(runtime.faux.state.callCount).toBe(2);
    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: {
        provider_calls: 2,
        pi_steps: 2,
        tool_invocations: 2,
        build_submissions: 1,
        repair_submissions: 0,
        no_progress_rejections: 0,
        terminal_phase: "validation_scheduled",
      },
    });
  });

  it("accepts a natural-language status when context already reports validation scheduled", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("builder_context_get", {}, { id: "call-context" })],
        { stopReason: "toolUse", timestamp: 1 },
      ),
      fauxAssistantMessage([fauxText("验证任务已排队，等待 Slurm 运行结果。")], { timestamp: 2 }),
    ]);
    const called: string[] = [];
    const gateway = {
      invoke: async (_request, _callId, name: string) => {
        called.push(name);
        return {
          schema_version: "pilot107.agent-tool-result/v1" as const,
          invocation_id: `inv-${called.length}`,
          result: { phase: "validation_scheduled", task_id: "task-existing" },
          error: null,
          evidence_refs: [],
          bytes_returned: 64,
        };
      },
    };
    const target = new TurnExecutor(() => runtime, async () => undefined, gateway, true);
    const events: AgentTurnEvent[] = [];

    await target.execute(
      builderTurnRequest(),
      (event) => events.push(event),
      new AbortController().signal,
    );

    expect(called).toEqual(["builder_context_get"]);
    expect(runtime.faux.state.callCount).toBe(2);
    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: { terminal_phase: "validation_scheduled" },
    });
  });

  it("repairs a phase-aware Builder that narrates but forgets the structured submission", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("builder_context_get", {}, { id: "call-context" })],
        { stopReason: "toolUse", timestamp: 1 },
      ),
      fauxAssistantMessage(
        [fauxText("我会创建科学计算脚本并等待审批。")],
        { timestamp: 2 },
      ),
      fauxAssistantMessage(
        [fauxToolCall("builder_build_submit", builderSubmission("build-repaired"), {
          id: "call-submit-repaired",
        })],
        { stopReason: "toolUse", timestamp: 3 },
      ),
    ]);
    const called: string[] = [];
    const gateway = {
      invoke: async (_request, _callId, name: string) => {
        called.push(name);
        return {
          schema_version: "pilot107.agent-tool-result/v1" as const,
          invocation_id: `inv-${called.length}`,
          result: name === "builder_build_submit"
            ? { status: "scheduled", phase: "validation_scheduled", task_id: "task-repaired" }
            : { phase: "drafting", next_action: "builder_build_submit" },
          error: null,
          evidence_refs: [],
          bytes_returned: 64,
        };
      },
    };
    const target = new TurnExecutor(
      () => runtime,
      async () => undefined,
      gateway,
      true,
    );
    const events: AgentTurnEvent[] = [];

    await target.execute(
      builderTurnRequest(),
      (event) => events.push(event),
      new AbortController().signal,
    );

    expect(called).toEqual(["builder_context_get", "builder_build_submit"]);
    expect(runtime.faux.state.callCount).toBe(3);
    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: {
        terminal_phase: "validation_scheduled",
        build_submissions: 1,
      },
    });
  });

  it("keeps one repair receipt nonterminal and completes in three Pi steps", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("builder_context_get", {}, { id: "call-context" })],
        { stopReason: "toolUse", timestamp: 1 },
      ),
      fauxAssistantMessage(
        [fauxToolCall("builder_build_submit", builderSubmission("build-1"), {
          id: "call-submit-1",
        })],
        { stopReason: "toolUse", timestamp: 2 },
      ),
      fauxAssistantMessage(
        [fauxToolCall("builder_build_submit", {
          ...builderSubmission("build-2"),
          expected_project_version: 2,
          base_change_set_id: "changeset-1",
        }, { id: "call-submit-2" })],
        { stopReason: "toolUse", timestamp: 3 },
      ),
    ]);
    let submissions = 0;
    const gateway = {
      invoke: async (_request, _callId, name: string) => {
        if (name === "builder_build_submit") submissions += 1;
        return {
          schema_version: "pilot107.agent-tool-result/v1" as const,
          invocation_id: `inv-${name}-${submissions}`,
          result: name === "builder_context_get"
            ? { phase: "drafting" }
            : submissions === 1
            ? {
                status: "repair_required",
                phase: "sandbox_failed",
                change_set_id: "changeset-1",
              }
            : {
                status: "scheduled",
                phase: "validation_scheduled",
                task_id: "task-1",
              },
          error: null,
          evidence_refs: [],
          bytes_returned: 64,
        };
      },
    };
    const target = new TurnExecutor(
      () => runtime,
      async () => undefined,
      gateway,
      true,
    );
    const events: AgentTurnEvent[] = [];

    await target.execute(
      builderTurnRequest(),
      (event) => events.push(event),
      new AbortController().signal,
    );

    expect(submissions).toBe(2);
    expect(runtime.faux.state.callCount).toBe(3);
    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: {
        pi_steps: 3,
        tool_invocations: 3,
        build_submissions: 1,
        repair_submissions: 1,
        no_progress_rejections: 0,
        terminal_phase: "validation_scheduled",
      },
    });
  });

  it("allows a phase-aware Builder to recover beyond the old twenty-step budget", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      ...Array.from({ length: 20 }, (_, index) =>
        fauxAssistantMessage(
          [fauxToolCall("builder_context_get", {}, {
            id: `call-context-${index + 1}`,
          })],
          { stopReason: "toolUse", timestamp: index + 1 },
        ),
      ),
      fauxAssistantMessage(
        [fauxToolCall("builder_build_submit", builderSubmission("build-21"), {
          id: "call-submit-21",
        })],
        { stopReason: "toolUse", timestamp: 21 },
      ),
    ]);
    const gateway = {
      invoke: async (_request, _callId, name: string) => ({
        schema_version: "pilot107.agent-tool-result/v1" as const,
        invocation_id: "inv-context",
        result: name === "builder_build_submit"
          ? { status: "scheduled", phase: "validation_scheduled", task_id: "task-21" }
          : { phase: "drafting", next_action: "builder_build_submit" },
        error: null,
        evidence_refs: [],
        bytes_returned: 64,
      }),
    };
    const target = new TurnExecutor(
      () => runtime,
      async () => undefined,
      gateway,
      true,
    );
    const events: AgentTurnEvent[] = [];

    await target.execute(
      builderTurnRequest(),
      (event) => events.push(event),
      new AbortController().signal,
    );

    expect(runtime.faux.state.callCount).toBe(21);
    expect(runtime.faux.getPendingResponseCount()).toBe(0);
    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: {
        terminal_phase: "validation_scheduled",
      },
    });
  });
});

function builderTurnRequest() {
  return durableRequest({
    task_kind: "experiment_builder",
    prompt_profile_id: "experiment_builder",
    toolset_id: "a2-project",
    input: {
      message: "build the bound Project",
      context_refs: ["project:project-1", "workspace:workspace-1"],
    },
  });
}

function builderSubmission(requestKey: string) {
  return {
    request_key: requestKey,
    approval_summary_zh: "创建实验文件，并在沙箱通过后提交一次受限验证。",
    expected_project_version: 1,
    expected_workspace_snapshot_digest: "a".repeat(64),
    base_change_set_id: null,
    blueprint: {
      goal: "validate",
      entrypoints: ["scripts/run.sh"],
      files: [],
      validations: [
        {
          validation_id: "sandbox",
          execution: "sandbox",
          argv: ["python", "validate.py"],
          expected_outputs: [],
        },
        {
          validation_id: "slurm",
          execution: "slurm",
          argv: ["bash", "scripts/run.sh"],
          expected_outputs: [],
        },
      ],
      contract_intent: { recipe_version_id: null, resource_hints: {} },
      expected_outputs: [],
      dependencies: [],
      open_questions: [],
    },
    patches: [{
      path: "main.py",
      expected_source_digest: null,
      operation: "create",
      content: "print(1)\n",
    }],
  };
}
