import { createHash } from "node:crypto";

import {
  parseToolInvocation,
  parseToolResult,
  type DurableAgentTurnRequest,
  type JsonObject,
  type ReadToolName,
  type ToolInvocation,
  type ToolResult,
} from "./protocol.js";

const TOOL_CALL_TIMEOUT_MS = 10_000;
const MAX_RESPONSE_BYTES = 1024 * 1024;

export interface ToolGatewayClientOptions {
  readonly url: string;
  readonly fetch?: typeof fetch;
  readonly now?: () => number;
}

export class ToolGatewayClient {
  readonly #url: string;
  readonly #fetch: typeof fetch;
  readonly #now: () => number;

  constructor(options: ToolGatewayClientOptions) {
    this.#url = options.url;
    this.#fetch = options.fetch ?? globalThis.fetch;
    this.#now = options.now ?? Date.now;
  }

  async invoke(
    request: DurableAgentTurnRequest,
    toolCallId: string,
    toolName: ReadToolName,
    arguments_: JsonObject,
    signal: AbortSignal,
  ): Promise<ToolResult> {
    const now = this.#now();
    const timeoutMs = Math.min(TOOL_CALL_TIMEOUT_MS, request.limits.timeout_ms);
    const callDigest = createHash("sha256")
      .update(request.turn_id)
      .update("\0")
      .update(toolCallId)
      .digest("hex");
    const invocation = parseToolInvocation({
      schema_version: "pilot107.agent-tool-invocation/v1",
      invocation_id: `inv-${callDigest}`,
      idempotency_key: `idem-${callDigest}`,
      owner: request.owner,
      session_id: request.session_id,
      turn_id: request.turn_id,
      state_version: request.state_version,
      profile_id: request.prompt_profile_id,
      tool_name: toolName,
      arguments: arguments_,
      deadline: new Date(now + timeoutMs).toISOString(),
    });
    const callAbort = new AbortController();
    let timedOut = false;
    const abortFromTurn = (): void => callAbort.abort(abortReason(signal));
    if (signal.aborted) abortFromTurn();
    else signal.addEventListener("abort", abortFromTurn, { once: true });
    const timer = setTimeout(() => {
      timedOut = true;
      callAbort.abort(new DOMException("Agent tool gateway timeout", "TimeoutError"));
    }, timeoutMs);
    timer.unref();
    let response: Response;
    try {
      response = await this.#fetch(this.#url, {
        method: "POST",
        headers: {
          authorization: `Bearer ${request.capability_token}`,
          "content-type": "application/json; charset=utf-8",
        },
        body: JSON.stringify(invocation),
        redirect: "manual",
        signal: callAbort.signal,
      });
    } catch {
      if (timedOut && !signal.aborted) {
        throw new ToolGatewayClientError("The Agent tool gateway timed out.");
      }
      if (signal.aborted) throw abortReason(signal);
      throw new ToolGatewayClientError("The Agent tool gateway was unavailable.");
    } finally {
      clearTimeout(timer);
      signal.removeEventListener("abort", abortFromTurn);
    }
    if (response.headers.get("content-type")?.toLowerCase() !== (
      "application/json; charset=utf-8"
    )) {
      throw response.ok
        ? new ToolGatewayClientError("The Agent tool gateway returned invalid content.")
        : new ToolGatewayClientError("The Agent tool gateway rejected the request.");
    }
    const body = await readBoundedBody(response, MAX_RESPONSE_BYTES);
    let value: unknown;
    try {
      value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
    } catch {
      throw response.ok
        ? new ToolGatewayClientError("The Agent tool gateway returned invalid content.")
        : new ToolGatewayClientError("The Agent tool gateway rejected the request.");
    }
    let result: ToolResult;
    try {
      result = parseToolResult(value);
    } catch {
      throw response.ok
        ? new ToolGatewayClientError("The Agent tool gateway returned invalid content.")
        : new ToolGatewayClientError("The Agent tool gateway rejected the request.");
    }
    if (result.invocation_id !== invocation.invocation_id) {
      throw response.ok
        ? new ToolGatewayClientError("The Agent tool gateway returned a mismatched result.")
        : new ToolGatewayClientError("The Agent tool gateway rejected the request.");
    }
    if (!response.ok && result.error === null) {
      throw new ToolGatewayClientError("The Agent tool gateway rejected the request.");
    }
    return result;
  }
}

export class ToolGatewayClientError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ToolGatewayClientError";
  }
}

function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("The Turn was aborted", "AbortError");
}

async function readBoundedBody(
  response: Response,
  maximumBytes: number,
): Promise<Uint8Array> {
  if (response.body === null) return new Uint8Array();
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maximumBytes) {
        await reader.cancel();
        throw new ToolGatewayClientError(
          "The Agent tool gateway response was too large.",
        );
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}
