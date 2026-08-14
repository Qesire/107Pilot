import { afterEach, describe, expect, it } from "vitest";

import type { ModelProfile } from "../src/config.js";
import { createCampusModelRuntime } from "../src/models.js";
import type { AgentTurnEvent, AgentTurnRequest } from "../src/protocol.js";
import { TurnExecutor } from "../src/turn-executor.js";
import {
  contractPatchRequest,
  deltaText,
  interactiveRequest,
  neverAbort,
  terminal,
} from "./support/fixtures.js";
import {
  delay,
  destroy,
  fragmented,
  response,
  scripted,
  sse,
  startMockOpenAI,
  type MockGateway,
  type MockResponse,
  write,
} from "./support/mock-openai.js";

const gateways = new Set<MockGateway>();

afterEach(async () => {
  await Promise.all([...gateways].map((gateway) => gateway.close()));
  gateways.clear();
});

describe("campus OpenAI-compatible provider", () => {
  it("handles arbitrarily fragmented OpenAI SSE without streaming usage", async () => {
    const gateway = await openGateway([
      sse([
        fragmented(chatChunk({ role: "assistant" }), [1, 2, 5, 3]),
        fragmented(chatChunk({ content: "hello" }), [2, 1, 7, 4, 3]),
        fragmented(chatChunk({}, "stop"), [1, 9, 2]),
        fragmented("data: [DONE]\n\n", [1, 1, 2, 3]),
      ]),
    ]);

    const events = await runCampusTurn(gateway);
    const completed = terminal(events);

    expect(events.filter((event) => event.type === "message_delta").map(deltaText).join(""))
      .toBe("hello");
    expect(completed).toMatchObject({
      type: "turn_completed",
      payload: {
        result: "hello",
        provider: "campus-default",
        model: "campus-model",
        provider_calls: 1,
        usage: {
          input_tokens: null,
          output_tokens: null,
          cache_read_tokens: null,
          cache_write_tokens: null,
        },
      },
    });
  });

  it("sends only the conservative campus payload and keeps bearer auth private", async () => {
    const gateway = await openGateway([toolResultSse()]);

    const events = await runCampusTurn(gateway, {
      request: {
        ...contractPatchRequest(),
        model_profile_id: "campus-default",
        limits: { timeout_ms: 1_000, max_output_tokens: 37 },
      },
    });

    expect(terminal(events)).toMatchObject({
      type: "turn_completed",
      payload: {
        result: {
          suggested_patch: {},
          explanation_zh: "证据不足，不修改。",
        },
      },
    });
    expect(gateway.requests).toHaveLength(1);
    const recorded = gateway.requests[0]!;
    expect(recorded.method).toBe("POST");
    expect(recorded.url).toBe("/v1/chat/completions");
    expect(recorded.headers.authorization).toBe("Bearer campus-test-secret");
    expect(recorded.body).toMatchObject({
      model: "campus-model",
      stream: true,
      max_tokens: 37,
      messages: [
        { role: "system" },
        { role: "user" },
      ],
      tools: [
        {
          type: "function",
          function: { name: "emit_result" },
        },
      ],
    });
    expect(recorded.body).not.toHaveProperty("max_completion_tokens");
    expect(recorded.body).not.toHaveProperty("store");
    expect(recorded.body).not.toHaveProperty("reasoning_effort");
    expect(recorded.body).not.toHaveProperty("stream_options");
    expect(recorded.body).not.toHaveProperty("tool_choice");
    const tools = recorded.body.tools as Array<{ function: Record<string, unknown> }>;
    expect(tools[0]!.function).not.toHaveProperty("strict");
    expect(JSON.stringify(events)).not.toContain("campus-test-secret");
  });

  it.each([
    [401, "provider_auth", 1],
    [403, "provider_auth", 1],
    [408, "provider_timeout", 2],
    [429, "provider_rate_limited", 2],
    [500, "provider_unavailable", 2],
    [503, "provider_unavailable", 2],
  ] as const)(
    "maps HTTP %i to %s with a bounded attempt count",
    async (status, code, calls) => {
      const gateway = await openGateway(
        Array.from({ length: calls }, () => httpFailure(status)),
      );
      const sleeps: number[] = [];

      const events = await runCampusTurn(gateway, { maxAttempts: calls, sleeps });

      expect(terminal(events)).toMatchObject({
        type: "turn_failed",
        payload: {
          error: { code },
        },
      });
      expect(gateway.requests).toHaveLength(calls);
      expect(sleeps).toEqual(calls === 1 ? [] : [100]);
      expect(JSON.stringify(events)).not.toContain("campus-test-secret");
      expect(JSON.stringify(events)).not.toContain("provider-body-secret");
    },
  );

  it("uses only the 100ms and 400ms executor backoff before three failed provider calls", async () => {
    const gateway = await openGateway([
      httpFailure(503),
      httpFailure(503),
      httpFailure(503),
    ]);
    const sleeps: number[] = [];

    const events = await runCampusTurn(gateway, { maxAttempts: 3, sleeps });

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: {
        error: { code: "provider_unavailable", retryable: true },
      },
    });
    expect(gateway.requests).toHaveLength(3);
    expect(sleeps).toEqual([100, 400]);
  });

  it("fails closed on a malformed SSE JSON event", async () => {
    const gateway = await openGateway([
      sse(["data: {not-json}\n\n", "data: [DONE]\n\n"]),
    ]);

    const events = await runCampusTurn(gateway, { maxAttempts: 1 });

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: { error: { code: "provider_invalid_response" } },
    });
    expect(gateway.requests).toHaveLength(1);
  });

  it("fails closed when a stream ends without the OpenAI DONE sentinel", async () => {
    const gateway = await openGateway([
      sse([
        chatChunk({ role: "assistant" }),
        chatChunk({ content: "complete-looking" }),
        chatChunk({}, "stop"),
      ]),
    ]);

    const events = await runCampusTurn(gateway, { maxAttempts: 1 });

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: { error: { code: "provider_invalid_response" } },
    });
    expect(gateway.requests).toHaveLength(1);
  });

  it("does not mistake model text containing [DONE] for the SSE sentinel", async () => {
    const gateway = await openGateway([
      sse([
        chatChunk({ role: "assistant" }),
        chatChunk({ content: "The literal text data: [DONE] is not a sentinel." }),
        chatChunk({}, "stop"),
      ]),
    ]);

    const events = await runCampusTurn(gateway, { maxAttempts: 1 });

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: { error: { code: "provider_invalid_response" } },
    });
  });

  it("retries a transport disconnect before public output and then fails closed", async () => {
    const gateway = await openGateway([
      disconnectBeforeOutput(),
      disconnectBeforeOutput(),
    ]);
    const sleeps: number[] = [];

    const events = await runCampusTurn(gateway, { maxAttempts: 2, sleeps });

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: {
        error: { code: "provider_unavailable", retryable: true },
      },
    });
    expect(gateway.requests).toHaveLength(2);
    expect(sleeps).toEqual([100]);
  });

  it("maps a provider deadline to timeout and does not expose secrets", async () => {
    const gateway = await openGateway([
      scripted([delay(500), ...successfulTextSteps("too late")]),
    ]);

    const events = await runCampusTurn(gateway, {
      maxAttempts: 1,
      timeoutMs: 75,
    });

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: { error: { code: "provider_timeout", retryable: true } },
    });
    expect(gateway.requests).toHaveLength(1);
    expect(JSON.stringify(events)).not.toContain("campus-test-secret");
  });

  it("preserves an explicit Turn abort while the gateway body is pending", async () => {
    const gateway = await openGateway([
      scripted([delay(500), ...successfulTextSteps("too late")]),
    ]);
    const controller = new AbortController();

    const pending = runCampusTurn(gateway, {
      maxAttempts: 1,
      timeoutMs: 1_000,
      signal: controller.signal,
    });
    await waitForRequest(gateway);
    controller.abort();
    const events = await pending;

    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: { error: { code: "aborted", retryable: false } },
    });
    expect(gateway.requests).toHaveLength(1);
  });

  it("does not retry a disconnect after public interactive text", async () => {
    const gateway = await openGateway([
      scripted([
        write(chatChunk({ role: "assistant" })),
        write(chatChunk({ content: "partial" })),
        delay(25),
        destroy(),
      ]),
      sse([
        chatChunk({ role: "assistant" }),
        chatChunk({ content: "must not replay" }),
        chatChunk({}, "stop"),
        "data: [DONE]\n\n",
      ]),
    ]);
    const sleeps: number[] = [];

    const events = await runCampusTurn(gateway, { maxAttempts: 2, sleeps });

    expect(events.filter((event) => event.type === "message_delta").map(deltaText).join(""))
      .toBe("partial");
    expect(terminal(events)).toMatchObject({
      type: "turn_failed",
      payload: { error: { code: "provider_unavailable", retryable: true } },
    });
    expect(gateway.requests).toHaveLength(1);
    expect(sleeps).toEqual([]);
  });
});

async function openGateway(
  responses: Parameters<typeof startMockOpenAI>[0],
): Promise<MockGateway> {
  const gateway = await startMockOpenAI(responses);
  gateways.add(gateway);
  return gateway;
}

async function runCampusTurn(
  gateway: MockGateway,
  options: {
    readonly maxAttempts?: 1 | 2 | 3;
    readonly timeoutMs?: number;
    readonly sleeps?: number[];
    readonly request?: AgentTurnRequest;
    readonly signal?: AbortSignal;
  } = {},
): Promise<AgentTurnEvent[]> {
  const timeoutMs = options.timeoutMs ?? 1_000;
  const runtime = createCampusModelRuntime(
    campusProfile(gateway.url, options.maxAttempts ?? 2, timeoutMs),
  );
  const events: AgentTurnEvent[] = [];
  await new TurnExecutor(() => runtime, async (milliseconds) => {
    options.sleeps?.push(milliseconds);
  }).execute(
    options.request ?? {
      ...interactiveRequest(),
      model_profile_id: "campus-default",
      limits: { timeout_ms: timeoutMs, max_output_tokens: 64 },
    },
    (event) => {
      events.push(event);
    },
    options.signal ?? neverAbort,
  );
  return events;
}

async function waitForRequest(gateway: MockGateway): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (gateway.requests.length > 0) return;
    await new Promise<void>((resolve) => setTimeout(resolve, 1));
  }
  throw new Error("mock gateway did not receive the request");
}

function campusProfile(
  baseUrl: string,
  maxAttempts: 1 | 2 | 3 = 2,
  timeoutMs = 1_000,
): ModelProfile {
  return {
    id: "campus-default",
    provider: "campus-openai-compatible",
    baseUrl,
    model: "campus-model",
    apiKey: "campus-test-secret",
    timeoutMs,
    maxOutputTokens: 64,
    maxAttempts,
    contextWindow: 32_768,
  };
}

function httpFailure(status: number): MockResponse {
  return response(
    status,
    JSON.stringify({
      error: {
        message: `provider-body-secret-${status} campus-test-secret`,
        type: "mock_provider_error",
        code: `mock_${status}`,
      },
    }),
  );
}

function disconnectBeforeOutput(): MockResponse {
  return scripted([destroy()]);
}

function successfulTextSteps(text: string) {
  return [
    write(chatChunk({ role: "assistant" })),
    write(chatChunk({ content: text })),
    write(chatChunk({}, "stop")),
    write("data: [DONE]\n\n"),
  ] as const;
}

function toolResultSse(): MockResponse {
  const result = JSON.stringify({
    suggested_patch: {},
    explanation_zh: "证据不足，不修改。",
  });
  return sse([
    chatChunk({ role: "assistant" }),
    chatChunk({
      tool_calls: [
        {
          index: 0,
          id: "call-mock-1",
          type: "function",
          function: { name: "emit_result", arguments: result },
        },
      ],
    }),
    chatChunk({}, "tool_calls"),
    "data: [DONE]\n\n",
  ]);
}

function chatChunk(
  delta: Record<string, unknown>,
  finishReason: string | null = null,
): string {
  return `data: ${JSON.stringify({
    id: "chatcmpl-mock-1",
    object: "chat.completion.chunk",
    created: 1_786_320_000,
    model: "campus-model",
    choices: [
      {
        index: 0,
        delta,
        logprobs: null,
        finish_reason: finishReason,
      },
    ],
  })}\n\n`;
}
