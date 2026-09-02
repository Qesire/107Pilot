import {
  fauxAssistantMessage,
  fauxThinking,
  fauxText,
  fauxToolCall,
} from "@earendil-works/pi-ai";
import { describe, expect, it } from "vitest";

import {
  checkpointFromState,
  computeCheckpointDigest,
} from "../src/checkpoint.js";
import type { ModelProfile } from "../src/config.js";
import { AgentdTurnError } from "../src/errors.js";
import {
  createFauxModelRuntime,
  type FauxModelRuntime,
  type ModelRuntime,
} from "../src/models.js";
import {
  assertTerminalInvariant,
  type AgentCheckpoint,
  type AgentTurnEvent,
  type AgentTurnRequest,
} from "../src/protocol.js";
import { TurnExecutor } from "../src/turn-executor.js";
import {
  contractPatchRequest,
  deltaText,
  explainRequest,
  interactiveRequest,
  neverAbort,
  remediationRequest,
  terminal,
} from "./support/fixtures.js";

async function executeCollect(
  target: TurnExecutor,
  request: AgentTurnRequest,
  signal: AbortSignal = neverAbort,
): Promise<AgentTurnEvent[]> {
  const events: AgentTurnEvent[] = [];
  await target.execute(request, (event) => {
    events.push(event);
  }, signal);
  return events;
}

function executor(
  runtime: ModelRuntime,
  sleeps: number[] = [],
): TurnExecutor {
  return new TurnExecutor(
    () => runtime,
    async (milliseconds) => {
      sleeps.push(milliseconds);
    },
  );
}

function withProfile(
  runtime: FauxModelRuntime,
  patch: Partial<ModelProfile>,
): FauxModelRuntime {
  return { ...runtime, profile: { ...runtime.profile, ...patch } };
}

function userText(message: { content: string | readonly { type: string; text?: string }[] }): string {
  return typeof message.content === "string"
    ? message.content
    : message.content
        .filter((item) => item.type === "text")
        .map((item) => item.text ?? "")
        .join("");
}

function failedCheckpoint(events: AgentTurnEvent[]): AgentCheckpoint {
  const event = terminal(events);
  if (event.type !== "turn_failed" || event.payload.checkpoint === undefined) {
    throw new Error("expected a failed Turn with a checkpoint");
  }
  return event.payload.checkpoint;
}

describe("faux Turn execution", () => {
  it("streams an interactive response through the real Pi Agent and completes once", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage([fauxText("hello world")], { timestamp: 1 }),
    ]);

    const events = await executeCollect(executor(runtime), interactiveRequest());

    expect(
      events
        .filter((event) => event.type === "message_delta")
        .map(deltaText)
        .join(""),
    ).toBe("hello world");
    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: {
        result: "hello world",
        provider: "faux-default",
        model: "faux-1",
        model_profile_id: "faux-default",
        provider_calls: 1,
      },
    });
    expect(runtime.faux.state.callCount).toBe(1);
    expect(assertTerminalInvariant(events)).toBe(events.at(-1));
  });

  it.each([
    {
      name: "explanation",
      request: explainRequest(),
      result: {
        summary: "作业失败。",
        narrative: "退出码为 1。",
        recommendations: ["检查 stderr。"],
        warnings: [],
        citations: [
          { fact_id: "fact-1", evidence_object_ids: ["object-1"] },
        ],
      },
    },
    {
      name: "contract patch",
      request: contractPatchRequest(),
      result: {
        suggested_patch: { "resources.cpus_per_task": 4 },
        explanation_zh: "将 CPU 调整为 4。",
      },
    },
    {
      name: "remediation plan",
      request: remediationRequest(),
      result: {
        schema_version: "pilot107.remediation-plan/v1",
        summary: "调整内存请求。",
        fact_ids: ["fact-1"],
        required_inputs: [],
        proposals: [
          {
            proposal_key: "raise-memory",
            action_type: "contract_patch",
            rationale: "作业内存不足。",
            evidence_fact_ids: ["fact-1"],
            parameters: { "resources.memory": "8G" },
          },
        ],
        stop_conditions: ["仍然 OOM 时停止。"],
      },
    },
  ])("returns validated $name arguments from the real emit_result tool", async ({
    request,
    result,
  }) => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("emit_result", result, { id: `call-${request.task_kind}` })],
        { stopReason: "toolUse", timestamp: 1 },
      ),
    ]);

    const events = await executeCollect(executor(runtime), request);

    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: { result, provider_calls: 1 },
    });
    expect(events.map((event) => event.type)).toContain("tool_call_completed");
    expect(runtime.faux.state.callCount).toBe(1);
    expect(assertTerminalInvariant(events)).toBe(events.at(-1));
  });
});

describe("structured output repair", () => {
  it("uses one fixed repair prompt after invalid tool arguments", async () => {
    const runtime = createFauxModelRuntime();
    let repairPrompt = "";
    let repairUserCount = 0;
    runtime.faux.setResponses([
      fauxAssistantMessage(
        [
          fauxToolCall(
            "emit_result",
            { suggested_patch: { "resources.cpus_per_task": 4 } },
            { id: "invalid-patch" },
          ),
        ],
        { stopReason: "toolUse", timestamp: 1 },
      ),
      (context) => {
        const users = context.messages.filter((message) => message.role === "user");
        repairUserCount = users.length;
        repairPrompt = userText(users.at(-1)!);
        return fauxAssistantMessage(
          [
            fauxToolCall(
              "emit_result",
              {
                suggested_patch: { "resources.cpus_per_task": 4 },
                explanation_zh: "将 CPU 调整为 4。",
              },
              { id: "repaired-patch" },
            ),
          ],
          { stopReason: "toolUse", timestamp: 2 },
        );
      },
    ]);

    const events = await executeCollect(executor(runtime), contractPatchRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: {
        result: {
          suggested_patch: { "resources.cpus_per_task": 4 },
          explanation_zh: "将 CPU 调整为 4。",
        },
        provider_calls: 2,
      },
    });
    expect(repairPrompt).toContain("emit_result");
    expect(repairPrompt).not.toContain("use four CPUs");
    expect(repairUserCount).toBe(2);
    expect(runtime.faux.state.callCount).toBe(2);
    expect(assertTerminalInvariant(events)).toBe(events.at(-1));
  });

  it("fails closed after exactly one repair still omits emit_result", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage([fauxText("not a tool call")], { timestamp: 1 }),
      fauxAssistantMessage([fauxText("still not a tool call")], { timestamp: 2 }),
    ]);

    const events = await executeCollect(executor(runtime), remediationRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: {
        error: { code: "output_contract_violation", retryable: false },
      },
    });
    expect(runtime.faux.state.callCount).toBe(2);
    expect(assertTerminalInvariant(events)).toBe(events.at(-1));
  });
});

describe("provider retry policy", () => {
  it("retries a provider response with neither text nor tool calls", async () => {
    const runtime = createFauxModelRuntime();
    const sleeps: number[] = [];
    runtime.faux.setResponses([
      fauxAssistantMessage([], { stopReason: "stop", timestamp: 1 }),
      fauxAssistantMessage([fauxText("recovered from empty response")], {
        timestamp: 2,
      }),
    ]);

    const events = await executeCollect(executor(runtime, sleeps), interactiveRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: { result: "recovered from empty response", provider_calls: 2 },
    });
    expect(runtime.faux.state.callCount).toBe(2);
    expect(sleeps).toEqual([100]);
  });

  it("retries a leading 429 provider error even when Pi never reports an HTTP response", async () => {
    const runtime = createFauxModelRuntime();
    const sleeps: number[] = [];
    runtime.faux.setResponses([
      fauxAssistantMessage([], {
        stopReason: "error",
        errorMessage: '429: {"error":{"message":"rate limited"}}',
        timestamp: 1,
      }),
      fauxAssistantMessage([fauxText("rate limit recovered")], { timestamp: 2 }),
    ]);

    const events = await executeCollect(executor(runtime, sleeps), interactiveRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: { result: "rate limit recovered", provider_calls: 2 },
    });
    expect(runtime.faux.state.callCount).toBe(2);
    expect(sleeps).toEqual([100]);
  });

  it("maps a leading 401 provider error to non-retryable authentication failure", async () => {
    const runtime = createFauxModelRuntime();
    const sleeps: number[] = [];
    runtime.faux.setResponses([
      fauxAssistantMessage([], {
        stopReason: "error",
        errorMessage: "401 Incorrect API key ending in secret-value",
        timestamp: 1,
      }),
      fauxAssistantMessage([fauxText("must not retry")], { timestamp: 2 }),
    ]);

    const events = await executeCollect(executor(runtime, sleeps), interactiveRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: {
        error: {
          code: "provider_auth",
          retryable: false,
          provider_status: 401,
        },
      },
    });
    expect(JSON.stringify(events)).not.toContain("secret-value");
    expect(runtime.faux.state.callCount).toBe(1);
    expect(runtime.faux.getPendingResponseCount()).toBe(1);
    expect(sleeps).toEqual([]);
  });

  it("maps a provider authentication message without an HTTP status to authentication failure", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage([], {
        stopReason: "error",
        errorMessage: "No API key for provider: campus-default",
        timestamp: 1,
      }),
      fauxAssistantMessage([fauxText("must not retry")], { timestamp: 2 }),
    ]);

    const events = await executeCollect(executor(runtime), interactiveRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: {
        error: {
          code: "provider_auth",
          retryable: false,
        },
      },
    });
    expect(runtime.faux.state.callCount).toBe(1);
    expect(runtime.faux.getPendingResponseCount()).toBe(1);
  });

  it("does not infer an HTTP status from arbitrary numbers inside provider text", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage([], {
        stopReason: "error",
        errorMessage: "Provider rejected an invalid record containing value 429.",
        timestamp: 1,
      }),
      fauxAssistantMessage([fauxText("must not retry")], { timestamp: 2 }),
    ]);

    const events = await executeCollect(executor(runtime), interactiveRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: {
        error: { code: "provider_invalid_response", retryable: false },
      },
    });
    expect(runtime.faux.state.callCount).toBe(1);
    expect(runtime.faux.getPendingResponseCount()).toBe(1);
  });

  it.each([
    ["429", 429],
    ["5xx", 503],
  ])("retries a %s response before public interactive output", async (_name, status) => {
    const runtime = createFauxModelRuntime();
    const sleeps: number[] = [];
    let retryUserCount = 0;
    runtime.faux.setResponses([
      async (_context, options, _state, model) => {
        await options?.onResponse?.({ status, headers: {} }, model);
        throw Object.assign(new Error(`HTTP ${status}`), { status });
      },
      (context) => {
        retryUserCount = context.messages.filter(
          (message) => message.role === "user",
        ).length;
        return fauxAssistantMessage([fauxText("recovered")], { timestamp: 2 });
      },
    ]);

    const events = await executeCollect(executor(runtime, sleeps), interactiveRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: { result: "recovered", provider_calls: 2 },
    });
    expect(runtime.faux.state.callCount).toBe(2);
    expect(retryUserCount).toBe(1);
    expect(sleeps).toEqual([100]);
    expect(assertTerminalInvariant(events)).toBe(events.at(-1));
  });

  it("retries a transport failure encoded by Pi as an error assistant message", async () => {
    const runtime = createFauxModelRuntime();
    const sleeps: number[] = [];
    runtime.faux.setResponses([
      fauxAssistantMessage([], {
        stopReason: "error",
        errorMessage: "fetch failed: ECONNRESET",
        timestamp: 1,
      }),
      fauxAssistantMessage([fauxText("network recovered")], { timestamp: 2 }),
    ]);

    const events = await executeCollect(executor(runtime, sleeps), interactiveRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: { result: "network recovered", provider_calls: 2 },
    });
    expect(runtime.faux.state.callCount).toBe(2);
    expect(sleeps).toEqual([100]);
  });

  it("uses the bounded 100ms and 400ms backoff before a third attempt", async () => {
    const runtime = withProfile(createFauxModelRuntime(), { maxAttempts: 3 });
    const sleeps: number[] = [];
    runtime.faux.setResponses([
      fauxAssistantMessage([], {
        stopReason: "error",
        errorMessage: "fetch failed: ECONNRESET",
        timestamp: 1,
      }),
      fauxAssistantMessage([], {
        stopReason: "error",
        errorMessage: "fetch failed: ECONNRESET",
        timestamp: 2,
      }),
      fauxAssistantMessage([fauxText("third attempt")], { timestamp: 3 }),
    ]);

    const events = await executeCollect(executor(runtime, sleeps), interactiveRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: { result: "third attempt", provider_calls: 3 },
    });
    expect(runtime.faux.state.callCount).toBe(3);
    expect(sleeps).toEqual([100, 400]);
  });

  it("does not replay an interactive request after meaningful public output", async () => {
    const runtime = createFauxModelRuntime();
    const sleeps: number[] = [];
    runtime.faux.setResponses([
      async (_context, options, _state, model) => {
        await options?.onResponse?.({ status: 503, headers: {} }, model);
        return fauxAssistantMessage([fauxText("partial public answer")], {
          stopReason: "error",
          errorMessage: "HTTP 503",
          timestamp: 1,
        });
      },
      fauxAssistantMessage([fauxText("must not replay")], { timestamp: 2 }),
    ]);

    const events = await executeCollect(executor(runtime, sleeps), interactiveRequest());

    expect(
      events.filter((event) => event.type === "message_delta").map(deltaText).join(""),
    ).toBe("partial public answer");
    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: { error: { code: "provider_unavailable", retryable: true } },
    });
    expect(runtime.faux.state.callCount).toBe(1);
    expect(runtime.faux.getPendingResponseCount()).toBe(1);
    expect(sleeps).toEqual([]);
    expect(assertTerminalInvariant(events)).toBe(events.at(-1));
  });

  it("can restart a constrained attempt after public text because emit_result has no side effects", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      async (_context, options, _state, model) => {
        await options?.onResponse?.({ status: 503, headers: {} }, model);
        return fauxAssistantMessage([fauxText("interrupted explanation")], {
          stopReason: "error",
          errorMessage: "HTTP 503",
          timestamp: 1,
        });
      },
      fauxAssistantMessage(
        [
          fauxToolCall(
            "emit_result",
            {
              suggested_patch: {},
              explanation_zh: "证据不足，不修改。",
            },
            { id: "safe-retry" },
          ),
        ],
        { stopReason: "toolUse", timestamp: 2 },
      ),
    ]);

    const events = await executeCollect(executor(runtime), contractPatchRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: { provider_calls: 2 },
    });
    expect(runtime.faux.state.callCount).toBe(2);
  });

  it("aggregates usage from a failed constrained attempt and its successful retry", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage([fauxText("x".repeat(480))], {
        stopReason: "error",
        errorMessage: "503: provider unavailable",
        timestamp: 1,
      }),
      fauxAssistantMessage(
        [
          fauxToolCall(
            "emit_result",
            {
              suggested_patch: {},
              explanation_zh: "重试后返回安全结果。",
            },
            { id: "usage-retry" },
          ),
        ],
        { stopReason: "toolUse", timestamp: 2 },
      ),
    ]);

    const events = await executeCollect(executor(runtime), contractPatchRequest());
    const completed = terminal(events);
    if (completed.type !== "turn_completed" || completed.payload.checkpoint === undefined) {
      throw new Error("expected a completed Turn with a checkpoint");
    }

    expect(completed.payload.provider_calls).toBe(2);
    expect(completed.payload.usage.input_tokens).toBeGreaterThan(0);
    expect(completed.payload.usage.output_tokens).toBeGreaterThan(120);
    expect(completed.payload.usage.cache_read_tokens).toBeGreaterThan(0);
    expect(completed.payload.usage.cache_write_tokens).toBeGreaterThan(0);
    expect(completed.payload.checkpoint.usage).toEqual(completed.payload.usage);
    expect(computeCheckpointDigest(completed.payload.checkpoint)).toBe(
      completed.payload.checkpoint.digest,
    );
  });

  it("exports a policy helper that enforces attempts, retryability, and interactive no-replay", async () => {
    const { shouldRetry } = await import("../src/turn-executor.js");
    const retryable = new AgentdTurnError(
      "provider_unavailable",
      true,
      "unavailable",
    );

    expect(
      shouldRetry({
        taskKind: "interactive",
        error: retryable,
        publicOutputEmitted: false,
        attempt: 1,
        maxAttempts: 2,
      }),
    ).toBe(true);
    expect(
      shouldRetry({
        taskKind: "interactive",
        error: retryable,
        publicOutputEmitted: true,
        attempt: 1,
        maxAttempts: 2,
      }),
    ).toBe(false);
    expect(
      shouldRetry({
        taskKind: "contract_patch",
        error: retryable,
        publicOutputEmitted: true,
        attempt: 1,
        maxAttempts: 2,
      }),
    ).toBe(true);
    expect(
      shouldRetry({
        taskKind: "contract_patch",
        error: new AgentdTurnError("provider_auth", false, "auth"),
        publicOutputEmitted: false,
        attempt: 1,
        maxAttempts: 2,
      }),
    ).toBe(false);
    expect(
      shouldRetry({
        taskKind: "contract_patch",
        error: retryable,
        publicOutputEmitted: false,
        attempt: 2,
        maxAttempts: 2,
      }),
    ).toBe(false);
  });
});

describe("abort, timeout, and checkpoint restore", () => {
  it("checkpoints an aborted partial response and resumes it in a fresh Agent without duplicating input", async () => {
    const firstRuntime = createFauxModelRuntime({ tokensPerSecond: 100 });
    firstRuntime.faux.setResponses([
      fauxAssistantMessage(
        [
          fauxThinking("secret thinking that must never be checkpointed"),
          fauxText("partial response that is long enough to abort"),
        ],
        { timestamp: 1 },
      ),
    ]);
    const controller = new AbortController();
    const firstEvents: AgentTurnEvent[] = [];
    await executor(firstRuntime).execute(
      interactiveRequest(),
      (event) => {
        firstEvents.push(event);
        if (event.type === "message_delta") controller.abort();
      },
      controller.signal,
    );

    expect(terminal(firstEvents)).toMatchObject({
      type: "turn_failed",
      payload: { error: { code: "aborted", retryable: false } },
    });
    expect(assertTerminalInvariant(firstEvents)).toBe(firstEvents.at(-1));
    const checkpoint = failedCheckpoint(firstEvents);
    expect(JSON.stringify(checkpoint)).not.toContain("secret thinking");

    const secondRuntime = createFauxModelRuntime();
    let finalPrompt = "";
    secondRuntime.faux.setResponses([
      (context) => {
        const users = context.messages.filter((message) => message.role === "user");
        finalPrompt = userText(users.at(-1)!);
        return fauxAssistantMessage([fauxText("resumed")], { timestamp: 2 });
      },
    ]);
    const resumedEvents = await executeCollect(
      executor(secondRuntime),
      interactiveRequest({ checkpoint }),
    );

    expect(terminal(resumedEvents)).toMatchObject({
      type: "turn_completed",
      payload: { result: "resumed" },
    });
    expect(finalPrompt).toContain("从已清理的 checkpoint 继续被中断的 Turn");
    expect(finalPrompt).not.toContain('"message":"hello"');
    expect(JSON.stringify(resumedEvents)).not.toContain("secret thinking");
    expect(assertTerminalInvariant(resumedEvents)).toBe(resumedEvents.at(-1));
  });

  it("aborts at the stricter request deadline and returns provider_timeout", async () => {
    const runtime = createFauxModelRuntime({ tokensPerSecond: 20 });
    runtime.faux.setResponses([
      fauxAssistantMessage([fauxText("this response cannot finish before the deadline")], {
        timestamp: 1,
      }),
    ]);
    const request: AgentTurnRequest = {
      ...interactiveRequest(),
      limits: { timeout_ms: 100, max_output_tokens: 128 },
    };

    const events = await executeCollect(executor(runtime), request);

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: { error: { code: "provider_timeout", retryable: true } },
    });
    expect(runtime.faux.state.callCount).toBe(1);
    expect(assertTerminalInvariant(events)).toBe(events.at(-1));
  });

  it("fails a signal that was already aborted without calling the provider", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage([fauxText("must not run")], { timestamp: 1 }),
    ]);
    const controller = new AbortController();
    controller.abort();

    const events = await executeCollect(
      executor(runtime),
      interactiveRequest(),
      controller.signal,
    );

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: { error: { code: "aborted" } },
    });
    expect(runtime.faux.state.callCount).toBe(0);
  });

  it.each(["digest", "model profile"])(
    "fails closed on a restored checkpoint %s mismatch before calling the provider",
    async (kind) => {
      const runtime = createFauxModelRuntime();
      const base = checkpointFromState(interactiveRequest(), { messages: [] });
      let invalid: AgentCheckpoint;
      if (kind === "digest") {
        invalid = { ...base, digest: "b".repeat(64) };
      } else {
        const changed = { ...base, model_profile_id: "campus-default" };
        invalid = { ...changed, digest: computeCheckpointDigest(changed) };
      }

      const events = await executeCollect(
        executor(runtime),
        interactiveRequest({ checkpoint: invalid }),
      );

      expect(terminal(events)).toMatchObject({
        type: "turn_failed",
        payload: { error: { code: "internal_error", retryable: false } },
      });
      expect(runtime.faux.state.callCount).toBe(0);
      expect(assertTerminalInvariant(events)).toBe(events.at(-1));
    },
  );

  it("does not count restored checkpoint usage again in the resumed execution", async () => {
    const request = interactiveRequest();
    const prior = {
      ...fauxAssistantMessage([fauxText("prior response")], { timestamp: 1 }),
      usage: {
        input: 100_000,
        output: 100_000,
        cacheRead: 100_000,
        cacheWrite: 100_000,
        totalTokens: 400_000,
        cost: {
          input: 0,
          output: 0,
          cacheRead: 0,
          cacheWrite: 0,
          total: 0,
        },
      },
    };
    const checkpoint = checkpointFromState(request, { messages: [prior] });
    expect(checkpoint.usage.output_tokens).toBe(100_000);

    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage([fauxText("fresh")], { timestamp: 2 }),
    ]);

    const events = await executeCollect(
      executor(runtime),
      interactiveRequest({ checkpoint }),
    );
    const completed = terminal(events);
    if (completed.type !== "turn_completed") {
      throw new Error("expected a completed resumed Turn");
    }

    expect(completed.payload.result).toBe("fresh");
    expect(completed.payload.usage.output_tokens).toBe(2);
    expect(completed.payload.usage.input_tokens).toBeLessThan(100_000);
    expect(completed.payload.usage.cache_read_tokens).toBe(0);
    expect(completed.payload.usage.cache_write_tokens).toBeGreaterThan(0);
  });
});

describe("execution boundaries", () => {
  it("reports unavailable campus streaming usage as null in completion and checkpoint", async () => {
    const faux = createFauxModelRuntime();
    const runtime: FauxModelRuntime = {
      ...faux,
      profile: {
        ...faux.profile,
        provider: "campus-openai-compatible",
        baseUrl: "https://campus.invalid/v1",
      },
      model: {
        ...faux.model,
        compat: {
          ...faux.model.compat,
          supportsUsageInStreaming: false,
        },
      },
    };
    runtime.faux.setResponses([
      fauxAssistantMessage([fauxText("campus response without stream usage")], {
        timestamp: 1,
      }),
    ]);

    const events = await executeCollect(executor(runtime), interactiveRequest());
    const completed = terminal(events);
    if (completed.type !== "turn_completed" || completed.payload.checkpoint === undefined) {
      throw new Error("expected a completed Turn with a checkpoint");
    }

    expect(completed.payload.usage).toEqual({
      input_tokens: null,
      output_tokens: null,
      cache_read_tokens: null,
      cache_write_tokens: null,
    });
    expect(completed.payload.checkpoint.usage).toEqual(completed.payload.usage);
    expect(computeCheckpointDigest(completed.payload.checkpoint)).toBe(
      completed.payload.checkpoint.digest,
    );
    expect(assertTerminalInvariant(events)).toBe(events.at(-1));
  });

  it.each([
    {
      name: "request",
      profileTimeout: 700,
      profileTokens: 96,
      requestTimeout: 500,
      requestTokens: 64,
      expectedTimeout: 500,
      expectedTokens: 64,
    },
    {
      name: "profile",
      profileTimeout: 300,
      profileTokens: 48,
      requestTimeout: 500,
      requestTokens: 64,
      expectedTimeout: 300,
      expectedTokens: 48,
    },
  ])("lets $name limits only tighten the real provider stream", async ({
    profileTimeout,
    profileTokens,
    requestTimeout,
    requestTokens,
    expectedTimeout,
    expectedTokens,
  }) => {
    const runtime = withProfile(createFauxModelRuntime(), {
      timeoutMs: profileTimeout,
      maxOutputTokens: profileTokens,
    });
    let observed: Record<string, unknown> = {};
    runtime.faux.setResponses([
      (_context, options, _state, model) => {
        observed = {
          model_max_tokens: model.maxTokens,
          option_max_tokens: options?.maxTokens,
          timeout_ms: options?.timeoutMs,
          max_retries: options?.maxRetries,
        };
        return fauxAssistantMessage([fauxText("bounded")], { timestamp: 1 });
      },
    ]);
    const request: AgentTurnRequest = {
      ...interactiveRequest(),
      limits: {
        timeout_ms: requestTimeout,
        max_output_tokens: requestTokens,
      },
    };

    await executeCollect(executor(runtime), request);

    expect(observed).toMatchObject({
      model_max_tokens: expectedTokens,
      option_max_tokens: expectedTokens,
      max_retries: 0,
    });
    expect(observed.timeout_ms).toEqual(expect.any(Number));
    expect(observed.timeout_ms as number).toBeGreaterThan(0);
    expect(observed.timeout_ms as number).toBeLessThanOrEqual(expectedTimeout);
  });

  it("returns a non-retryable provider_unavailable when a profile is not configured", async () => {
    const target = new TurnExecutor(() => undefined as never);

    const events = await executeCollect(target, interactiveRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: {
        error: { code: "provider_unavailable", retryable: false },
      },
    });
    expect(events.filter((event) => event.type === "turn_started")).toHaveLength(1);
    expect(assertTerminalInvariant(events)).toBe(events.at(-1));
  });

  it("turns a malformed Pi event into one provider_invalid_response terminal", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("emit_result", {}, { id: "" })],
        { stopReason: "toolUse", timestamp: 1 },
      ),
    ]);

    const events = await executeCollect(executor(runtime), contractPatchRequest());

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: {
        error: { code: "provider_invalid_response", retryable: false },
      },
    });
    expect(assertTerminalInvariant(events)).toBe(events.at(-1));
  });

  it("propagates a delta writer rejection and never attempts a recursive terminal write", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage([fauxText("writer failure")], { timestamp: 1 }),
    ]);
    const writeError = new Error("socket backpressure failed");
    const attemptedTypes: string[] = [];

    await expect(
      executor(runtime).execute(
        interactiveRequest(),
        (event) => {
          attemptedTypes.push(event.type);
          if (event.type === "message_delta") throw writeError;
        },
        neverAbort,
      ),
    ).rejects.toBe(writeError);
    expect(attemptedTypes.filter((type) => type === "turn_failed")).toEqual([]);
    expect(attemptedTypes.filter((type) => type === "turn_completed")).toEqual([]);
  });

  it("propagates a terminal writer rejection instead of disguising it as provider failure", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage([fauxText("complete")], { timestamp: 1 }),
    ]);
    const writeError = new Error("terminal socket closed");
    const accepted: AgentTurnEvent[] = [];

    await expect(
      executor(runtime).execute(
        interactiveRequest(),
        (event) => {
          if (event.type === "turn_completed") throw writeError;
          accepted.push(event);
        },
        neverAbort,
      ),
    ).rejects.toBe(writeError);
    expect(accepted.some((event) => event.type === "turn_failed")).toBe(false);
  });
});
