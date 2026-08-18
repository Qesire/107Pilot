import http, { type Server } from "node:http";
import { setTimeout as delay } from "node:timers/promises";

import { afterEach, describe, expect, it } from "vitest";

import { ToolGatewayClient } from "../src/tool-gateway.js";
import type { ToolInvocation } from "../src/protocol.js";
import { durableRequest } from "./support/fixtures.js";

const runningServers = new Set<Server>();

afterEach(async () => {
  await Promise.all(
    [...runningServers].map(
      (server) =>
        new Promise<void>((resolve, reject) => {
          server.close((error) => (error === undefined ? resolve() : reject(error)));
        }),
    ),
  );
  runningServers.clear();
});

async function openGateway(
  handler: (request: http.IncomingMessage, body: Buffer) => void,
): Promise<string> {
  const server = http.createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      handler(request, Buffer.concat(chunks));
      response.writeHead(200, {
        "content-type": "application/json; charset=utf-8",
      });
      response.end(
        JSON.stringify({
          schema_version: "pilot107.agent-tool-result/v1",
          invocation_id: JSON.parse(Buffer.concat(chunks).toString("utf8"))
            .invocation_id,
          result: { run_id: "run-1", status: "FAILED" },
          error: null,
          evidence_refs: ["run:run-1"],
          bytes_returned: 38,
        }),
      );
    });
  });
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
    throw new Error("expected gateway TCP address");
  }
  return `http://127.0.0.1:${address.port}/internal/v1/agent-tools/invoke`;
}

describe("ToolGatewayClient", () => {
  it("sends the exact bounded invocation with bearer authority and stable ids", async () => {
    const received: Array<{
      authorization: string | undefined;
      contentType: string | undefined;
      invocation: ToolInvocation;
    }> = [];
    const url = await openGateway((request, body) => {
      received.push({
        authorization: request.headers.authorization,
        contentType: request.headers["content-type"],
        invocation: JSON.parse(body.toString("utf8")) as ToolInvocation,
      });
    });
    const client = new ToolGatewayClient({
      url,
      now: () => Date.parse("2026-08-16T12:00:00.000Z"),
    });
    const request = durableRequest();

    const first = await client.invoke(
      request,
      "call-run-1",
      "run_get",
      { run_id: "run-1" },
      new AbortController().signal,
    );
    await client.invoke(
      request,
      "call-run-1",
      "run_get",
      { run_id: "run-1" },
      new AbortController().signal,
    );

    expect(first).toMatchObject({
      result: { run_id: "run-1", status: "FAILED" },
      error: null,
    });
    expect(received).toHaveLength(2);
    expect(received[0]).toEqual(received[1]);
    expect(received[0]).toEqual({
      authorization: "Bearer opaque.capability.token",
      contentType: "application/json; charset=utf-8",
      invocation: {
        schema_version: "pilot107.agent-tool-invocation/v1",
        invocation_id:
          "inv-5bee4c756aa35aa117cf9ee41da4d8e0e3325d308734e7ab5ad9a18d0fe132f1",
        idempotency_key:
          "idem-5bee4c756aa35aa117cf9ee41da4d8e0e3325d308734e7ab5ad9a18d0fe132f1",
        owner: "alice",
        session_id: "session-1",
        turn_id: "turn-1",
        state_version: 7,
        profile_id: "hpc-readonly-v1",
        tool_name: "run_get",
        arguments: { run_id: "run-1" },
        deadline: "2026-08-16T12:00:10.000Z",
      },
    });
  });

  it("aborts one gateway call at the shorter Turn deadline", async () => {
    const fetchUntilAborted: typeof fetch = async (_input, init) =>
      new Promise<Response>((_resolve, reject) => {
        const signal = init?.signal;
        if (signal?.aborted === true) {
          reject(signal.reason);
          return;
        }
        signal?.addEventListener("abort", () => reject(signal.reason), {
          once: true,
        });
      });
    const client = new ToolGatewayClient({
      url: "http://gateway.invalid/internal/v1/agent-tools/invoke",
      fetch: fetchUntilAborted,
    });

    const result = client.invoke(
      durableRequest({ limits: { timeout_ms: 100, max_output_tokens: 256 } }),
      "call-timeout",
      "run_get",
      { run_id: "run-1" },
      new AbortController().signal,
    );

    await expect(
      Promise.race([
        result,
        delay(300).then(() => {
          throw new Error("test deadline expired before gateway timeout");
        }),
      ]),
    ).rejects.toThrow(/gateway.*timed out/i);
  });

  it.each([401, 403, 409, 429, 500, 503])(
    "rejects HTTP %s without disclosing hostile response text or authority",
    async (status) => {
      const fetchError: typeof fetch = async () =>
        new Response(
          JSON.stringify({
            error: {
              code: "HOSTILE",
              message: "authorization=server-secret Bearer response-secret",
            },
          }),
          {
            status,
            headers: { "content-type": "application/json; charset=utf-8" },
          },
        );
      const client = new ToolGatewayClient({
        url: "http://gateway.invalid/internal/v1/agent-tools/invoke?url-secret=yes",
        fetch: fetchError,
      });

      const call = client.invoke(
        durableRequest({ capability_token: "opaque-client-secret" }),
        "call-error",
        "run_get",
        { run_id: "run-1" },
        new AbortController().signal,
      );

      await expect(call).rejects.toThrow(/gateway.*rejected/i);
      try {
        await call;
      } catch (error) {
        const message = String(error);
        expect(message).not.toContain("server-secret");
        expect(message).not.toContain("response-secret");
        expect(message).not.toContain("opaque-client-secret");
        expect(message).not.toContain("url-secret");
      }
    },
  );

  it.each([
    {
      name: "wrong content type",
      response: () =>
        new Response("{}", {
          status: 200,
          headers: { "content-type": "text/plain" },
        }),
    },
    {
      name: "malformed JSON",
      response: () =>
        new Response("{", {
          status: 200,
          headers: { "content-type": "application/json; charset=utf-8" },
        }),
    },
    {
      name: "unknown result field",
      response: (invocation: ToolInvocation) =>
        jsonResponse({
          ...validResult(invocation.invocation_id),
          authority: "must-not-pass",
        }),
    },
    {
      name: "invocation ID mismatch",
      response: () => jsonResponse(validResult("different-invocation")),
    },
    {
      name: "oversized body",
      response: () =>
        new Response("x".repeat(1024 * 1024 + 1), {
          status: 200,
          headers: { "content-type": "application/json; charset=utf-8" },
        }),
    },
  ])("rejects $name", async ({ response }) => {
    const fetchInvalid: typeof fetch = async (_input, init) => {
      const invocation = JSON.parse(String(init?.body)) as ToolInvocation;
      return response(invocation);
    };
    const client = new ToolGatewayClient({
      url: "http://gateway.invalid/internal/v1/agent-tools/invoke",
      fetch: fetchInvalid,
    });

    await expect(
      client.invoke(
        durableRequest(),
        "call-invalid",
        "run_get",
        { run_id: "run-1" },
        new AbortController().signal,
      ),
    ).rejects.toThrow(/gateway.*(?:invalid|mismatched|too large)/i);
  });

  it("cancels response streaming as soon as the 1 MiB ceiling is crossed", async () => {
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(600_000));
        controller.enqueue(new Uint8Array(600_000));
        controller.enqueue(new Uint8Array(1));
        controller.close();
      },
      cancel() {
        cancelled = true;
      },
    });
    const fetchOversized: typeof fetch = async () =>
      new Response(body, {
        status: 200,
        headers: { "content-type": "application/json; charset=utf-8" },
      });
    const client = new ToolGatewayClient({
      url: "http://gateway.invalid/internal/v1/agent-tools/invoke",
      fetch: fetchOversized,
    });

    await expect(
      client.invoke(
        durableRequest(),
        "call-oversized-stream",
        "run_get",
        { run_id: "run-1" },
        new AbortController().signal,
      ),
    ).rejects.toThrow(/too large/i);
    expect(cancelled).toBe(true);
  });
});

function validResult(invocationId: string) {
  return {
    schema_version: "pilot107.agent-tool-result/v1",
    invocation_id: invocationId,
    result: { run_id: "run-1", status: "FAILED" },
    error: null,
    evidence_refs: ["run:run-1"],
    bytes_returned: 38,
  };
}

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
