import { createHash, timingSafeEqual } from "node:crypto";
import http, {
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";

import type { AgentdConfig } from "./config.js";
import type { EventWrite } from "./events.js";
import {
  isTerminalEvent,
  parseTurnRequest,
  type AgentTurnEvent,
  type AgentTurnRequest,
} from "./protocol.js";
import { AGENTD_VERSION } from "./version.js";

const MAX_REQUEST_BYTES = 2 * 1024 * 1024;
const MAX_NDJSON_LINE_BYTES = 1024 * 1024;
const MAX_NDJSON_RESPONSE_BYTES = 8 * 1024 * 1024;
const TURN_PATH = "/internal/v1/turns";
const INTERNAL_PREFIX = "/internal/";
const TURN_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;

export interface TurnRunner {
  execute(
    request: AgentTurnRequest,
    write: EventWrite,
    signal: AbortSignal,
  ): Promise<void>;
}

interface ActiveTurn {
  readonly controller: AbortController;
}

interface ServerState {
  readonly active: Map<string, ActiveTurn>;
  closing: boolean;
  closePromise?: Promise<void>;
}

export interface AgentdServerCloseOptions {
  /** Maximum time to wait for abort-aware executors before closing sockets. */
  readonly graceMs?: number;
}

class HttpBoundaryError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
    this.name = "HttpBoundaryError";
  }
}

class ResponseBoundaryError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ResponseBoundaryError";
  }
}

const serverStates = new WeakMap<Server, ServerState>();

export function createAgentdServer(
  config: AgentdConfig,
  executor: TurnRunner,
): Server {
  const state: ServerState = { active: new Map(), closing: false };
  const server = http.createServer((request, response) => {
    void handleRequest(config, executor, state, request, response).catch(() => {
      if (!response.headersSent) {
        sendError(response, 500, "internal_error");
      } else if (!response.destroyed) {
        response.destroy();
      }
    });
  });
  serverStates.set(server, state);
  return server;
}

export function closeAgentdServer(
  server: Server,
  options: AgentdServerCloseOptions = {},
): Promise<void> {
  const state = serverStates.get(server);
  if (state?.closePromise !== undefined) return state.closePromise;
  const graceMs = options.graceMs ?? 5_000;
  if (!Number.isSafeInteger(graceMs) || graceMs < 0 || graceMs > 60_000) {
    return Promise.reject(new RangeError("graceMs is outside the supported range"));
  }

  for (const active of state?.active.values() ?? []) {
    active.controller.abort();
  }
  if (state !== undefined) state.closing = true;

  if (!server.listening) return Promise.resolve();
  const closePromise = new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      server.closeAllConnections();
    }, graceMs);
    timer.unref();
    server.close((error) => {
      clearTimeout(timer);
      if (error === undefined) resolve();
      else reject(error);
    });
  });
  if (state !== undefined) state.closePromise = closePromise;
  return closePromise;
}

async function handleRequest(
  config: AgentdConfig,
  executor: TurnRunner,
  state: ServerState,
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const method = request.method ?? "";
  const target = request.url ?? "";

  if (target === "/healthz") {
    if (method !== "GET") return sendError(response, 405, "method_not_allowed");
    return sendJson(response, 200, { ok: true, version: AGENTD_VERSION });
  }
  if (target === "/readyz") {
    if (method !== "GET") return sendError(response, 405, "method_not_allowed");
    return sendJson(response, 200, config.publicSummary());
  }

  if (target.startsWith(INTERNAL_PREFIX)) {
    if (!constantTimeBearerMatches(request.headers.authorization, config.internalToken)) {
      request.resume();
      return sendError(response, 401, "unauthorized");
    }

    if (target === TURN_PATH) {
      if (method !== "POST") return sendError(response, 405, "method_not_allowed");
      return handleTurn(executor, state, request, response);
    }

    const cancelTurnId = parseCancelTurnId(target);
    if (cancelTurnId !== undefined) {
      if (method !== "POST") return sendError(response, 405, "method_not_allowed");
      request.resume();
      const active = state.active.get(cancelTurnId);
      const accepted = active !== undefined && !active.controller.signal.aborted;
      if (accepted) active.controller.abort();
      return sendJson(response, 200, {
        status: accepted ? "accepted" : "not_active",
      });
    }

    return sendError(response, 404, "not_found");
  }

  return sendError(response, 404, "not_found");
}

async function handleTurn(
  executor: TurnRunner,
  state: ServerState,
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  if (state.closing) return sendError(response, 503, "shutting_down");

  let turn: AgentTurnRequest;
  try {
    turn = await readTurnRequest(request);
  } catch (error) {
    if (error instanceof HttpBoundaryError) {
      return sendError(response, error.status, error.code);
    }
    return sendError(response, 500, "internal_error");
  }

  if (state.closing) return sendError(response, 503, "shutting_down");

  if (state.active.has(turn.turn_id)) {
    return sendError(response, 409, "turn_active");
  }

  const active: ActiveTurn = { controller: new AbortController() };
  state.active.set(turn.turn_id, active);
  const abortForDisconnect = (): void => {
    if (!response.writableEnded) active.controller.abort();
  };
  request.once("aborted", abortForDisconnect);
  response.once("close", abortForDisconnect);

  let cumulativeBytes = 0;
  let streamStarted = false;
  let terminalWritten = false;
  const write: EventWrite = async (event) => {
    const bytes = serializeEvent(event);
    if (bytes.length > MAX_NDJSON_LINE_BYTES) {
      throw new ResponseBoundaryError("NDJSON event exceeds the line limit");
    }
    if (cumulativeBytes + bytes.length > MAX_NDJSON_RESPONSE_BYTES) {
      throw new ResponseBoundaryError("NDJSON stream exceeds the response limit");
    }
    if (terminalWritten) {
      throw new ResponseBoundaryError("NDJSON event follows the terminal event");
    }
    if (!streamStarted) {
      response.writeHead(200, {
        "content-type": "application/x-ndjson; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
      });
      streamStarted = true;
    }
    cumulativeBytes += bytes.length;
    await writeWithBackpressure(response, bytes);
    if (isTerminalEvent(event)) terminalWritten = true;
  };

  let execution: Promise<void>;
  try {
    execution = executor.execute(turn, write, active.controller.signal);
  } catch {
    request.off("aborted", abortForDisconnect);
    response.off("close", abortForDisconnect);
    if (state.active.get(turn.turn_id) === active) state.active.delete(turn.turn_id);
    return sendError(response, 500, "internal_error");
  }

  try {
    await execution;
    if (!terminalWritten) {
      active.controller.abort();
      if (!streamStarted) sendError(response, 500, "internal_error");
      else if (!response.destroyed) response.destroy();
      return;
    }
    if (!response.destroyed) response.end();
  } catch {
    active.controller.abort();
    if (!streamStarted) {
      sendError(response, 500, "internal_error");
    } else if (terminalWritten) {
      if (!response.destroyed) response.end();
    } else if (!response.destroyed) {
      response.destroy();
    }
  } finally {
    request.off("aborted", abortForDisconnect);
    response.off("close", abortForDisconnect);
    if (state.active.get(turn.turn_id) === active) state.active.delete(turn.turn_id);
  }
}

async function readTurnRequest(request: IncomingMessage): Promise<AgentTurnRequest> {
  if (!isJsonContentType(request.headers["content-type"])) {
    request.resume();
    throw new HttpBoundaryError(415, "unsupported_media_type");
  }

  const declaredLength = request.headers["content-length"];
  if (declaredLength !== undefined) {
    const bytes = Number(declaredLength);
    if (!Number.isSafeInteger(bytes) || bytes < 0) {
      request.resume();
      throw new HttpBoundaryError(400, "invalid_content_length");
    }
    if (bytes > MAX_REQUEST_BYTES) {
      request.resume();
      throw new HttpBoundaryError(413, "request_too_large");
    }
  }

  const chunks: Buffer[] = [];
  let total = 0;
  try {
    for await (const rawChunk of request) {
      const chunk = Buffer.isBuffer(rawChunk) ? rawChunk : Buffer.from(rawChunk);
      total += chunk.length;
      if (total > MAX_REQUEST_BYTES) {
        request.resume();
        throw new HttpBoundaryError(413, "request_too_large");
      }
      chunks.push(chunk);
    }
  } catch (error) {
    if (error instanceof HttpBoundaryError) throw error;
    throw new HttpBoundaryError(400, "invalid_body");
  }

  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks, total));
  } catch {
    throw new HttpBoundaryError(400, "invalid_utf8");
  }

  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new HttpBoundaryError(400, "invalid_json");
  }
  try {
    return parseTurnRequest(value);
  } catch {
    throw new HttpBoundaryError(422, "invalid_turn");
  }
}

function isJsonContentType(value: string | undefined): boolean {
  if (value === undefined) return false;
  const [mediaType, ...parameters] = value.split(";").map((part) => part.trim());
  if (mediaType?.toLowerCase() !== "application/json") return false;
  return parameters.every(
    (parameter) => parameter.toLowerCase() === "charset=utf-8",
  );
}

function parseCancelTurnId(target: string): string | undefined {
  if (target.includes("?") || target.includes("#")) return undefined;
  const match = /^\/internal\/v1\/turns\/([^/]+)\/cancel$/.exec(target);
  const encoded = match?.[1];
  if (encoded === undefined || /%2f/i.test(encoded)) return undefined;
  let decoded: string;
  try {
    decoded = decodeURIComponent(encoded);
  } catch {
    return undefined;
  }
  return TURN_ID_PATTERN.test(decoded) ? decoded : undefined;
}

function constantTimeBearerMatches(
  authorization: string | undefined,
  expectedToken: string,
): boolean {
  const match = /^Bearer ([^\s]+)$/.exec(authorization ?? "");
  const suppliedDigest = sha256(match?.[1] ?? "");
  const expectedDigest = sha256(expectedToken);
  return timingSafeEqual(suppliedDigest, expectedDigest);
}

function sha256(value: string): Buffer {
  return createHash("sha256").update(value, "utf8").digest();
}

function serializeEvent(event: AgentTurnEvent): Buffer {
  let serialized: string;
  try {
    serialized = `${JSON.stringify(event)}\n`;
  } catch {
    throw new ResponseBoundaryError("NDJSON event is not serializable");
  }
  return Buffer.from(serialized, "utf8");
}

async function writeWithBackpressure(
  response: ServerResponse,
  bytes: Buffer,
): Promise<void> {
  if (response.destroyed || !response.writable) {
    throw new ResponseBoundaryError("NDJSON response is no longer writable");
  }
  if (response.write(bytes)) return;

  await new Promise<void>((resolve, reject) => {
    const cleanup = (): void => {
      response.off("drain", onDrain);
      response.off("close", onClose);
      response.off("error", onError);
    };
    const onDrain = (): void => {
      cleanup();
      resolve();
    };
    const onClose = (): void => {
      cleanup();
      reject(new ResponseBoundaryError("NDJSON response closed during backpressure"));
    };
    const onError = (): void => {
      cleanup();
      reject(new ResponseBoundaryError("NDJSON response failed during backpressure"));
    };
    response.once("drain", onDrain);
    response.once("close", onClose);
    response.once("error", onError);
  });
}

function sendError(
  response: ServerResponse,
  status: number,
  code: string,
): void {
  sendJson(response, status, { error: { code } });
}

function sendJson(response: ServerResponse, status: number, value: unknown): void {
  if (response.destroyed || response.writableEnded) return;
  const body = Buffer.from(JSON.stringify(value), "utf8");
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": body.length,
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  response.end(body);
}
