import * as http from "node:http";
import type { AddressInfo } from "node:net";
import type { Socket } from "node:net";

const MAX_REQUEST_BYTES = 1_048_576;

export interface RecordedRequest {
  readonly method: string;
  readonly url: string;
  readonly headers: http.IncomingHttpHeaders;
  readonly body: Record<string, unknown>;
}

export interface MockGateway {
  readonly url: string;
  readonly requests: RecordedRequest[];
  close(): Promise<void>;
}

export type MockResponseStep =
  | { readonly type: "write"; readonly data: Uint8Array }
  | { readonly type: "delay"; readonly milliseconds: number }
  | { readonly type: "destroy" }
  | { readonly type: "end" };

export interface MockResponse {
  readonly status: number;
  readonly headers?: Readonly<Record<string, string>>;
  readonly steps: readonly MockResponseStep[];
}

type ChunkInput = string | Uint8Array | readonly (string | Uint8Array)[];

export function fragmented(
  input: string | Uint8Array,
  sizes: readonly number[],
): Uint8Array[] {
  if (sizes.length === 0 || sizes.some((size) => !Number.isInteger(size) || size <= 0)) {
    throw new RangeError("fragment sizes must be positive integers");
  }
  const bytes = typeof input === "string" ? new TextEncoder().encode(input) : input;
  const chunks: Uint8Array[] = [];
  let offset = 0;
  let index = 0;
  while (offset < bytes.byteLength) {
    const size = sizes[index % sizes.length]!;
    const end = Math.min(bytes.byteLength, offset + size);
    chunks.push(bytes.slice(offset, end));
    offset = end;
    index += 1;
  }
  return chunks;
}

export function sse(chunks: readonly ChunkInput[]): MockResponse {
  return {
    status: 200,
    headers: { "content-type": "text/event-stream; charset=utf-8" },
    steps: [
      ...chunks.flatMap((chunk) =>
        Array.isArray(chunk)
          ? chunk.map(writeStep)
          : [writeStep(chunk as string | Uint8Array)],
      ),
      { type: "end" },
    ],
  };
}

export function response(
  status: number,
  body: string,
  headers: Readonly<Record<string, string>> = {
    "content-type": "application/json; charset=utf-8",
  },
): MockResponse {
  return {
    status,
    headers,
    steps: [{ type: "write", data: new TextEncoder().encode(body) }, { type: "end" }],
  };
}

export function delay(milliseconds: number): MockResponseStep {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) {
    throw new RangeError("delay must be a non-negative finite number");
  }
  return { type: "delay", milliseconds };
}

export function destroy(): MockResponseStep {
  return { type: "destroy" };
}

export function write(data: string | Uint8Array): MockResponseStep {
  return writeStep(data);
}

export function scripted(
  steps: readonly MockResponseStep[],
  options: {
    readonly status?: number;
    readonly headers?: Readonly<Record<string, string>>;
  } = {},
): MockResponse {
  return {
    status: options.status ?? 200,
    headers: options.headers ?? {
      "content-type": "text/event-stream; charset=utf-8",
    },
    steps,
  };
}

export async function startMockOpenAI(
  responses: readonly MockResponse[],
): Promise<MockGateway> {
  if (responses.length === 0) {
    throw new RangeError("mock gateway requires at least one response");
  }
  const requests: RecordedRequest[] = [];
  const sockets = new Set<Socket>();
  const closing = new AbortController();
  let responseIndex = 0;

  const server = http.createServer(async (request, reply) => {
    try {
      if (request.method !== "POST" || request.url !== "/v1/chat/completions") {
        reply.writeHead(404, { "content-type": "application/json; charset=utf-8" });
        reply.end('{"error":{"message":"not found"}}');
        return;
      }

      const body = await readJsonBody(request);
      requests.push({
        method: request.method,
        url: request.url,
        headers: { ...request.headers },
        body,
      });
      const current = responses[responseIndex];
      responseIndex += 1;
      if (current === undefined) {
        reply.writeHead(500, { "content-type": "application/json; charset=utf-8" });
        reply.end('{"error":{"message":"unexpected mock request"}}');
        return;
      }
      await playResponse(reply, current, closing.signal);
    } catch (error) {
      if (reply.destroyed || closing.signal.aborted) return;
      const status = error instanceof RequestTooLargeError ? 413 : 400;
      reply.writeHead(status, { "content-type": "application/json; charset=utf-8" });
      reply.end('{"error":{"message":"invalid request"}}');
    }
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });

  await new Promise<void>((resolve, reject) => {
    const onError = (error: Error) => {
      server.off("listening", onListening);
      reject(error);
    };
    const onListening = () => {
      server.off("error", onError);
      resolve();
    };
    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(0, "127.0.0.1");
  });

  const address = server.address() as AddressInfo;
  let closePromise: Promise<void> | undefined;
  return {
    url: `http://127.0.0.1:${address.port}/v1`,
    requests,
    close() {
      closePromise ??= new Promise<void>((resolve, reject) => {
        closing.abort();
        for (const socket of sockets) socket.destroy();
        server.close((error) => {
          if (error !== undefined) reject(error);
          else resolve();
        });
        server.closeAllConnections();
      });
      return closePromise;
    },
  };
}

async function readJsonBody(
  request: http.IncomingMessage,
): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const value of request) {
    const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
    size += chunk.byteLength;
    if (size > MAX_REQUEST_BYTES) throw new RequestTooLargeError();
    chunks.push(chunk);
  }
  const text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  const parsed: unknown = JSON.parse(text);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new TypeError("request JSON must be an object");
  }
  return parsed as Record<string, unknown>;
}

async function playResponse(
  reply: http.ServerResponse,
  script: MockResponse,
  signal: AbortSignal,
): Promise<void> {
  reply.writeHead(script.status, script.headers);
  for (const step of script.steps) {
    if (signal.aborted) return;
    switch (step.type) {
      case "write":
        await writeWithBackpressure(reply, step.data);
        break;
      case "delay":
        await abortableDelay(step.milliseconds, signal);
        break;
      case "destroy":
        reply.destroy();
        return;
      case "end":
        reply.end();
        return;
    }
  }
  reply.end();
}

function writeStep(data: string | Uint8Array): MockResponseStep {
  return {
    type: "write",
    data: typeof data === "string" ? new TextEncoder().encode(data) : data,
  };
}

function writeWithBackpressure(
  reply: http.ServerResponse,
  data: Uint8Array,
): Promise<void> {
  if (reply.write(data)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      reply.off("drain", onDrain);
      reply.off("close", onClose);
      reply.off("error", onError);
    };
    const onDrain = () => {
      cleanup();
      resolve();
    };
    const onClose = () => {
      cleanup();
      reject(new Error("mock response closed"));
    };
    const onError = (error: Error) => {
      cleanup();
      reject(error);
    };
    reply.once("drain", onDrain);
    reply.once("close", onClose);
    reply.once("error", onError);
  });
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new Error("mock gateway closed"));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(new Error("mock gateway closed"));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

class RequestTooLargeError extends Error {}
