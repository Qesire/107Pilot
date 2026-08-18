import { fauxAssistantMessage, fauxText } from "@earendil-works/pi-ai";
import type { Server } from "node:http";
import http from "node:http";
import net from "node:net";
import { setTimeout as delay } from "node:timers/promises";
import { afterEach, describe, expect, it } from "vitest";

import { configFromEnv, type AgentdConfig } from "../src/config.js";
import type { EventWrite } from "../src/events.js";
import { createFauxModelRuntime } from "../src/models.js";
import type {
  AgentTurnEvent,
  AgentTurnRequest,
  ExecutableAgentTurnRequest,
  JsonValue,
} from "../src/protocol.js";
import {
  closeAgentdServer,
  createAgentdServer,
  type TurnRunner,
} from "../src/server.js";
import { TurnExecutor } from "../src/turn-executor.js";
import { AGENTD_VERSION } from "../src/version.js";
import { durableRequest, interactiveRequest } from "./support/fixtures.js";

const TEST_TOKEN = "test-token";
const MAX_REQUEST_BYTES = 2 * 1024 * 1024;
const runningServers = new Set<Server>();

interface RunningServer {
  readonly server: Server;
  readonly url: string;
}

interface RawResponse {
  readonly status: number;
  readonly headers: http.IncomingHttpHeaders;
  readonly body: Buffer;
}

interface Deferred<T> {
  readonly promise: Promise<T>;
  resolve(value: T): void;
  reject(reason?: unknown): void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function testConfig(env: NodeJS.ProcessEnv = {}): AgentdConfig {
  return configFromEnv({
    NODE_ENV: "test",
    PILOT107_AGENTD_TOKEN: TEST_TOKEN,
    PILOT107_AGENTD_MODEL_PROFILE: "faux-default",
    ...env,
  });
}

async function startServer(
  runner: TurnRunner,
  config: AgentdConfig = testConfig(),
): Promise<RunningServer> {
  const server = createAgentdServer(config, runner);
  runningServers.add(server);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve();
    });
  });
  const address = server.address();
  if (address === null || typeof address === "string") {
    throw new Error("expected an internet listening address");
  }
  return { server, url: `http://127.0.0.1:${address.port}` };
}

afterEach(async () => {
  const servers = [...runningServers];
  runningServers.clear();
  await Promise.all(servers.map(async (server) => closeAgentdServer(server)));
});

function authHeaders(contentType = "application/json"): Record<string, string> {
  return {
    authorization: `Bearer ${TEST_TOKEN}`,
    "content-type": contentType,
  };
}

async function postTurn(
  url: string,
  request: ExecutableAgentTurnRequest = interactiveRequest(),
): Promise<Response> {
  return fetch(`${url}/internal/v1/turns`, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(request),
  });
}

async function cancel(
  url: string,
  encodedTurnId: string,
): Promise<{ response: Response; body: unknown }> {
  const response = await fetch(
    `${url}/internal/v1/turns/${encodedTurnId}/cancel`,
    {
      method: "POST",
      headers: authHeaders(),
      body: "{}",
    },
  );
  return { response, body: await response.json() };
}

async function errorCode(response: Response): Promise<string | undefined> {
  const value = (await response.json()) as {
    error?: { code?: string };
  };
  return value.error?.code;
}

function parseNdjson(text: string): AgentTurnEvent[] {
  return text
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line) as AgentTurnEvent);
}

function rawRequest(
  baseUrl: string,
  options: {
    readonly method?: string;
    readonly path: string;
    readonly headers?: http.OutgoingHttpHeaders;
    readonly chunks?: readonly Buffer[];
  },
): Promise<RawResponse> {
  const target = new URL(baseUrl);
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        hostname: target.hostname,
        port: target.port,
        method: options.method ?? "POST",
        path: options.path,
        headers: options.headers,
      },
      (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.on("end", () => {
          resolve({
            status: response.statusCode ?? 0,
            headers: response.headers,
            body: Buffer.concat(chunks),
          });
        });
      },
    );
    request.once("error", reject);
    for (const chunk of options.chunks ?? []) request.write(chunk);
    request.end();
  });
}

function failedEvent(
  turnId: string,
  sequence: number,
  code: "aborted" | "internal_error" = "aborted",
): AgentTurnEvent {
  return {
    schema_version: "pilot107.agent-turn-event/v1",
    turn_id: turnId,
    sequence,
    timestamp: "2026-08-10T00:00:00.000Z",
    type: "turn_failed",
    payload: {
      error: {
        code,
        retryable: false,
        message: code === "aborted" ? "The Turn was aborted." : "Internal error.",
      },
    },
  };
}

function completedEvent(
  turnId: string,
  sequence: number,
  result: JsonValue = "ok",
): AgentTurnEvent {
  return {
    schema_version: "pilot107.agent-turn-event/v1",
    turn_id: turnId,
    sequence,
    timestamp: "2026-08-10T00:00:00.000Z",
    type: "turn_completed",
    payload: {
      result,
      provider: "scripted",
      model: "scripted-1",
      model_profile_id: "faux-default",
      usage: {
        input_tokens: null,
        output_tokens: null,
        cache_read_tokens: null,
        cache_write_tokens: null,
      },
      provider_calls: 1,
      checkpoint_digest: "a".repeat(64),
      duration_ms: 1,
    },
  };
}

function startedEvent(turnId: string, sequence = 1): AgentTurnEvent {
  return {
    schema_version: "pilot107.agent-turn-event/v1",
    turn_id: turnId,
    sequence,
    timestamp: "2026-08-10T00:00:00.000Z",
    type: "turn_started",
    payload: { model_profile_id: "faux-default", task_kind: "interactive" },
  };
}

class BlockingRunner implements TurnRunner {
  readonly started = deferred<void>();
  readonly aborted = deferred<void>();
  readonly settle = deferred<void>();
  signal: AbortSignal | undefined;

  async execute(
    request: AgentTurnRequest,
    write: EventWrite,
    signal: AbortSignal,
  ): Promise<void> {
    this.signal = signal;
    signal.addEventListener("abort", () => this.aborted.resolve(), { once: true });
    await write(startedEvent(request.turn_id));
    this.started.resolve();
    await this.settle.promise;
    if (signal.aborted) await write(failedEvent(request.turn_id, 2));
    else await write(completedEvent(request.turn_id, 2));
  }
}

describe("public probes and authentication", () => {
  it("exposes minimal unauthenticated health and redacted readiness", async () => {
    const campus = testConfig({
      PILOT107_AGENTD_MODEL_PROFILE: "campus-default",
      PILOT107_LLM_BASE_URL: "https://secret-gateway.invalid/v1",
      PILOT107_LLM_MODEL: "campus-secret-model",
      PILOT107_LLM_API_KEY: "secret-api-key",
    });
    const { url } = await startServer({ execute: async () => undefined }, campus);

    const health = await fetch(`${url}/healthz`);
    expect(health.status).toBe(200);
    expect(await health.json()).toEqual({ ok: true, version: AGENTD_VERSION });

    const ready = await fetch(`${url}/readyz`);
    expect(ready.status).toBe(200);
    expect(await ready.json()).toEqual(campus.publicSummary());
    const publicText = JSON.stringify(campus.publicSummary());
    expect(publicText).not.toContain("test-token");
    expect(publicText).not.toContain("secret-api-key");
    expect(publicText).not.toContain("secret-gateway");
    expect(publicText).not.toContain("campus-secret-model");
  });

  it("authenticates internal requests before content type, parsing, or execution", async () => {
    let executions = 0;
    const { url } = await startServer({
      execute: async () => {
        executions += 1;
      },
    });

    const response = await fetch(`${url}/internal/v1/turns`, {
      method: "POST",
      headers: {
        authorization: "Bearer wrong-secret",
        "content-type": "text/plain",
      },
      body: "secret malformed body {",
    });

    expect(response.status).toBe(401);
    expect(response.headers.get("content-type")).toContain("application/json");
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(await response.json()).toEqual({ error: { code: "unauthorized" } });
    expect(executions).toBe(0);
  });
});

describe("Turn request and response boundaries", () => {
  it("streams a real faux Pi Turn as ordered newline-terminated NDJSON", async () => {
    const runtime = createFauxModelRuntime();
    runtime.faux.setResponses([
      fauxAssistantMessage([fauxText("hello over HTTP")], { timestamp: 1 }),
    ]);
    const executor = new TurnExecutor(() => runtime);
    const { url } = await startServer(executor);

    const response = await postTurn(url);
    const body = await response.text();
    const events = parseNdjson(body);

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe(
      "application/x-ndjson; charset=utf-8",
    );
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(body.endsWith("\n")).toBe(true);
    expect(events.map((event) => event.sequence)).toEqual(
      events.map((_, index) => index + 1),
    );
    expect(events.at(-1)).toMatchObject({
      type: "turn_completed",
      payload: { result: "hello over HTTP" },
    });
  });

  it("accepts a closed v2 durable Turn at the authenticated execution boundary", async () => {
    let received: ExecutableAgentTurnRequest | undefined;
    const { url } = await startServer({
      async execute(request, write) {
        received = request;
        await write({
          ...startedEvent(request.turn_id),
          payload: {
            model_profile_id: request.model_profile_id,
            task_kind: "interactive",
          },
        });
        await write(completedEvent(request.turn_id, 2));
      },
    });
    const request = durableRequest();

    const response = await postTurn(url, request);
    const events = parseNdjson(await response.text());

    expect(response.status).toBe(200);
    expect(received).toEqual(request);
    expect(events.at(-1)?.type).toBe("turn_completed");
  });

  it("holds a scripted executor at real socket backpressure until the client reads", async () => {
    let finished = false;
    const { server, url } = await startServer({
      async execute(request, write) {
        await write(startedEvent(request.turn_id));
        for (let sequence = 2; sequence < 32; sequence += 1) {
          await write({
            ...startedEvent(request.turn_id, sequence),
            type: "message_delta",
            payload: { delta: "x".repeat(220_000) },
          } as AgentTurnEvent);
        }
        await write(completedEvent(request.turn_id, 32));
        finished = true;
      },
    });
    const address = server.address();
    if (address === null || typeof address === "string") throw new Error("not listening");
    const body = Buffer.from(JSON.stringify(interactiveRequest()), "utf8");
    const firstData = deferred<void>();
    const ended = deferred<void>();
    const socket = net.createConnection({ host: "127.0.0.1", port: address.port });
    socket.once("error", ended.reject);
    socket.once("connect", () => {
      socket.write(
        [
          "POST /internal/v1/turns HTTP/1.1",
          `Host: ${new URL(url).host}`,
          `Authorization: Bearer ${TEST_TOKEN}`,
          "Content-Type: application/json",
          `Content-Length: ${body.length}`,
          "Connection: close",
          "",
          "",
        ].join("\r\n"),
      );
      socket.write(body);
    });
    socket.once("data", () => {
      socket.pause();
      firstData.resolve();
    });
    socket.once("end", () => ended.resolve());

    await firstData.promise;
    await delay(75);
    expect(finished).toBe(false);
    socket.resume();
    await ended.promise;
    expect(finished).toBe(true);
  });

  it.each([
    {
      name: "unsupported content type",
      status: 415,
      code: "unsupported_media_type",
      contentType: "text/plain",
      body: Buffer.from("{}"),
    },
    {
      name: "malformed JSON",
      status: 400,
      code: "invalid_json",
      contentType: "application/json",
      body: Buffer.from('{"secret":"do-not-echo"'),
    },
    {
      name: "malformed UTF-8",
      status: 400,
      code: "invalid_utf8",
      contentType: "application/json; charset=utf-8",
      body: Buffer.from([0xc3, 0x28]),
    },
    {
      name: "strict protocol mismatch",
      status: 422,
      code: "invalid_turn",
      contentType: "application/json",
      body: Buffer.from(JSON.stringify({ ...interactiveRequest(), unknown: "secret" })),
    },
  ])("returns a stable non-reflective error for $name", async ({
    status,
    code,
    contentType,
    body,
  }) => {
    const { url } = await startServer({ execute: async () => undefined });
    const response = await rawRequest(url, {
      path: "/internal/v1/turns",
      headers: {
        authorization: `Bearer ${TEST_TOKEN}`,
        "content-type": contentType,
        "content-length": body.length,
      },
      chunks: [body],
    });
    const text = response.body.toString("utf8");

    expect(response.status).toBe(status);
    expect(response.headers["cache-control"]).toBe("no-store");
    expect(JSON.parse(text)).toEqual({ error: { code } });
    expect(text).not.toContain("do-not-echo");
    expect(text).not.toContain("unknown");
  });

  it("rejects an oversized declared request before reading it", async () => {
    const { url } = await startServer({ execute: async () => undefined });
    const response = await rawRequest(url, {
      path: "/internal/v1/turns",
      headers: {
        authorization: `Bearer ${TEST_TOKEN}`,
        "content-type": "application/json",
        "content-length": MAX_REQUEST_BYTES + 1,
      },
      chunks: [Buffer.alloc(MAX_REQUEST_BYTES + 1, 0x20)],
    });

    expect(response.status).toBe(413);
    expect(JSON.parse(response.body.toString("utf8"))).toEqual({
      error: { code: "request_too_large" },
    });
  });

  it("rejects a chunked request when its accumulated body exceeds 2 MiB", async () => {
    const { url } = await startServer({ execute: async () => undefined });
    const response = await rawRequest(url, {
      path: "/internal/v1/turns",
      headers: {
        authorization: `Bearer ${TEST_TOKEN}`,
        "content-type": "application/json",
      },
      chunks: [Buffer.alloc(MAX_REQUEST_BYTES, 0x20), Buffer.from("x")],
    });

    expect(response.status).toBe(413);
    expect(JSON.parse(response.body.toString("utf8"))).toEqual({
      error: { code: "request_too_large" },
    });
  });
});

describe("active Turn lifecycle and cancellation", () => {
  it("registers an active turn before response headers or the first event", async () => {
    const entered = deferred<void>();
    const release = deferred<void>();
    const { url } = await startServer({
      async execute(request, write) {
        entered.resolve();
        await release.promise;
        await write(startedEvent(request.turn_id));
        await write(completedEvent(request.turn_id, 2));
      },
    });
    const first = postTurn(url);
    await entered.promise;

    const duplicate = await postTurn(url);
    expect(duplicate.status).toBe(409);
    expect(await errorCode(duplicate)).toBe("turn_active");

    release.resolve();
    expect(parseNdjson(await (await first).text()).at(-1)?.type).toBe(
      "turn_completed",
    );
  });

  it("keeps a cancelled Turn registered until its executor settles", async () => {
    const runner = new BlockingRunner();
    const { url } = await startServer(runner);
    const running = postTurn(url);
    await runner.started.promise;

    const first = await cancel(url, "turn-1");
    expect(first.response.status).toBe(200);
    expect(first.body).toEqual({ status: "accepted" });
    await runner.aborted.promise;

    expect((await cancel(url, "turn-1")).body).toEqual({ status: "not_active" });
    const duplicate = await postTurn(url);
    expect(duplicate.status).toBe(409);
    expect(await errorCode(duplicate)).toBe("turn_active");

    runner.settle.resolve();
    const events = parseNdjson(await (await running).text());
    expect(events.at(-1)).toMatchObject({
      type: "turn_failed",
      payload: { error: { code: "aborted" } },
    });
    expect((await cancel(url, "turn-1")).body).toEqual({ status: "not_active" });
  });

  it("rejects encoded slashes, query confusion, and invalid cancel ids", async () => {
    const { url } = await startServer({ execute: async () => undefined });
    for (const path of [
      "/internal/v1/turns/turn-1%2Fother/cancel",
      "/internal/v1/turns/turn-1/cancel?turn_id=other",
      "/internal/v1/turns/%25ZZ/cancel",
    ]) {
      const response = await fetch(`${url}${path}`, {
        method: "POST",
        headers: authHeaders(),
        body: "{}",
      });
      expect(response.status).toBe(404);
      expect(await errorCode(response)).toBe("not_found");
    }

    const unauthenticatedMalformed = await fetch(
      `${url}/internal/v1/turns/turn-1%2Fother/cancel`,
      { method: "POST", body: "{}" },
    );
    expect(unauthenticatedMalformed.status).toBe(401);
    expect(await errorCode(unauthenticatedMalformed)).toBe("unauthorized");
  });

  it("aborts the controller when the response consumer disconnects", async () => {
    const runner: TurnRunner & { aborted: Deferred<void> } = {
      aborted: deferred<void>(),
      async execute(request, write, signal) {
        signal.addEventListener("abort", () => this.aborted.resolve(), {
          once: true,
        });
        await write(startedEvent(request.turn_id));
        await new Promise<void>((resolve) => {
          signal.addEventListener("abort", () => resolve(), { once: true });
        });
      },
    };
    const { url } = await startServer(runner);
    const response = await postTurn(url);
    await response.body?.cancel();

    await Promise.race([
      runner.aborted.promise,
      delay(1_000).then(() => {
        throw new Error("response disconnect did not abort the Turn");
      }),
    ]);
  });

  it("does not abort a normally completed response", async () => {
    let signal: AbortSignal | undefined;
    const { url } = await startServer({
      async execute(request, write, receivedSignal) {
        signal = receivedSignal;
        await write(startedEvent(request.turn_id));
        await write(completedEvent(request.turn_id, 2));
      },
    });

    expect((await postTurn(url)).status).toBe(200);
    await delay(0);
    expect(signal?.aborted).toBe(false);
  });
});

describe("stream failure bounds and cleanup", () => {
  it("aborts and truncates a stream when one NDJSON line exceeds its hard limit", async () => {
    const aborted = deferred<void>();
    const { url } = await startServer({
      async execute(request, write, signal) {
        signal.addEventListener("abort", () => aborted.resolve(), { once: true });
        await write(startedEvent(request.turn_id));
        await write({
          ...startedEvent(request.turn_id, 2),
          type: "message_delta",
          payload: { delta: "x".repeat(1024 * 1024) },
        } as AgentTurnEvent);
      },
    });

    await expect(postTurn(url).then(async (response) => response.text())).rejects.toThrow();
    await aborted.promise;
    expect((await cancel(url, "turn-1")).body).toEqual({ status: "not_active" });
  });

  it("aborts and truncates a stream after the cumulative response hard limit", async () => {
    const aborted = deferred<void>();
    const { url } = await startServer({
      async execute(request, write, signal) {
        signal.addEventListener("abort", () => aborted.resolve(), { once: true });
        await write(startedEvent(request.turn_id));
        for (let sequence = 2; sequence < 42; sequence += 1) {
          await write({
            ...startedEvent(request.turn_id, sequence),
            type: "message_delta",
            payload: { delta: "x".repeat(220_000) },
          } as AgentTurnEvent);
        }
      },
    });

    const response = await postTurn(url);
    await expect(response.text()).rejects.toThrow();
    await aborted.promise;
  });

  it("returns JSON 500 for a synchronous pre-stream throw", async () => {
    const { url } = await startServer({
      execute(): Promise<void> {
        throw new Error("secret sync failure");
      },
    });

    const response = await postTurn(url);
    expect(response.status).toBe(500);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(await response.json()).toEqual({ error: { code: "internal_error" } });
  });

  it("returns JSON 500 when an async executor rejects before its first event", async () => {
    const { url } = await startServer({
      async execute(): Promise<void> {
        await Promise.resolve();
        throw new Error("secret async failure");
      },
    });

    const response = await postTurn(url);
    expect(response.status).toBe(500);
    expect(response.headers.get("content-type")).toContain("application/json");
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(await response.json()).toEqual({ error: { code: "internal_error" } });
  });

  it("never fabricates a terminal event after a streamed executor throw", async () => {
    const { url } = await startServer({
      async execute(request, write) {
        await write(startedEvent(request.turn_id));
        throw new Error("secret streamed failure");
      },
    });

    await expect(postTurn(url).then(async (response) => response.text())).rejects.toThrow();
    expect((await cancel(url, "turn-1")).body).toEqual({ status: "not_active" });
  });

  it("does not write an event after a terminal event", async () => {
    const { url } = await startServer({
      async execute(request, write) {
        await write(startedEvent(request.turn_id));
        await write(completedEvent(request.turn_id, 2));
        await write({
          ...startedEvent(request.turn_id, 3),
          type: "message_delta",
          payload: { delta: "must-not-appear" },
        } as AgentTurnEvent);
      },
    });

    const response = await postTurn(url);
    const body = await response.text();
    const events = parseNdjson(body);
    expect(events.map((event) => event.type)).toEqual([
      "turn_started",
      "turn_completed",
    ]);
    expect(body).not.toContain("must-not-appear");
  });
});

describe("routing, shutdown, and bootstrap", () => {
  it("uses stable 404 and 405 JSON responses", async () => {
    const { url } = await startServer({ execute: async () => undefined });
    const missing = await fetch(`${url}/missing`);
    expect(missing.status).toBe(404);
    expect(await errorCode(missing)).toBe("not_found");

    const healthMethod = await fetch(`${url}/healthz`, { method: "POST" });
    expect(healthMethod.status).toBe(405);
    expect(await errorCode(healthMethod)).toBe("method_not_allowed");

    const turnMethod = await fetch(`${url}/internal/v1/turns`, {
      headers: { authorization: `Bearer ${TEST_TOKEN}` },
    });
    expect(turnMethod.status).toBe(405);
    expect(await errorCode(turnMethod)).toBe("method_not_allowed");
  });

  it("aborts active Turns while closing the listener", async () => {
    const aborted = deferred<void>();
    const { server, url } = await startServer({
      async execute(request, write, signal) {
        await write(startedEvent(request.turn_id));
        await new Promise<void>((resolve) => {
          signal.addEventListener(
            "abort",
            () => {
              aborted.resolve();
              resolve();
            },
            { once: true },
          );
        });
      },
    });
    const response = await postTurn(url);

    const closing = closeAgentdServer(server);
    await aborted.promise;
    await expect(response.text()).rejects.toThrow();
    await closing;
    runningServers.delete(server);
  });

  it("bounds shutdown when an executor ignores abort and never settles", async () => {
    const release = deferred<void>();
    const { server, url } = await startServer({
      async execute(request, write) {
        await write(startedEvent(request.turn_id));
        await release.promise;
        await write(completedEvent(request.turn_id, 2));
      },
    });
    const response = await postTurn(url);
    const closing = closeAgentdServer(server, { graceMs: 25 });
    const outcome = await Promise.race([
      closing.then(() => "closed" as const),
      delay(150).then(() => "timed_out" as const),
    ]);
    release.resolve();
    await closing;
    await response.body?.cancel().catch(() => undefined);
    runningServers.delete(server);

    expect(outcome).toBe("closed");
  });

  it("keeps unconfigured campus mode ready but fails Turns in-band", async () => {
    const before = {
      SIGINT: process.listenerCount("SIGINT"),
      SIGTERM: process.listenerCount("SIGTERM"),
    };
    const { createAgentdExecutor } = await import("../src/main.js");
    expect(process.listenerCount("SIGINT")).toBe(before.SIGINT);
    expect(process.listenerCount("SIGTERM")).toBe(before.SIGTERM);
    const config = testConfig({
      PILOT107_AGENTD_MODEL_PROFILE: "campus-default",
      PILOT107_LLM_BASE_URL: "",
      PILOT107_LLM_MODEL: "",
    });
    const { url } = await startServer(createAgentdExecutor(config), config);

    const ready = await fetch(`${url}/readyz`);
    expect(await ready.json()).toMatchObject({ configured: false });
    const response = await postTurn(url, {
      ...interactiveRequest(),
      model_profile_id: "campus-default",
    });
    const events = parseNdjson(await response.text());
    expect(events.at(-1)).toMatchObject({
      type: "turn_failed",
      payload: { error: { code: "provider_unavailable" } },
    });
  });
});
