export type TurnErrorCode =
  | "provider_auth"
  | "provider_rate_limited"
  | "provider_timeout"
  | "provider_unavailable"
  | "provider_invalid_response"
  | "output_contract_violation"
  | "empty_provider_response"
  | "tool_step_budget_exhausted"
  | "aborted"
  | "internal_error";

export interface TurnErrorPayload {
  readonly code: TurnErrorCode;
  readonly retryable: boolean;
  readonly message: string;
  readonly provider_status?: number;
}

export class AgentdTurnError extends Error {
  constructor(
    readonly code: TurnErrorCode,
    readonly retryable: boolean,
    message: string,
    readonly providerStatus?: number,
  ) {
    super(sanitizeErrorMessage(message));
    this.name = "AgentdTurnError";
  }

  toPayload(): TurnErrorPayload {
    return {
      code: this.code,
      retryable: this.retryable,
      message: this.message,
      ...(this.providerStatus === undefined
        ? {}
        : { provider_status: this.providerStatus }),
    };
  }
}

function sanitizeErrorMessage(message: string): string {
  return message
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [REDACTED]")
    .replace(/https?:\/\/[^\s<>'"\])}]+/gi, (raw) => {
      try {
        const url = new URL(raw);
        url.search = "";
        url.hash = "";
        return url.toString();
      } catch {
        return "[REDACTED_URL]";
      }
    })
    .replace(
      /\b((?:api[-_]?key|authorization|(?:access[-_]?|refresh[-_]?)?token|password)\s*[:=]\s*)[^\s,;]+/gi,
      "$1[REDACTED]",
    )
    .slice(0, 4_096);
}

const MESSAGE_BY_CODE: Record<TurnErrorCode, string> = {
  provider_auth: "The model provider rejected authentication.",
  provider_rate_limited: "The model provider rate limit was reached.",
  provider_timeout: "The model provider request timed out.",
  provider_unavailable: "The model provider is unavailable.",
  provider_invalid_response: "The model provider returned an invalid response.",
  output_contract_violation: "The model did not emit the required result.",
  empty_provider_response: "The model provider returned an empty response.",
  tool_step_budget_exhausted:
    "The Turn reached its bounded tool-step limit before producing a final answer.",
  aborted: "The Turn was aborted.",
  internal_error: "The Turn failed because of an internal error.",
};

export function mapProviderError(error: unknown): AgentdTurnError {
  if (error instanceof AgentdTurnError) return error;

  if (isAbortError(error)) return turnError("aborted", false);

  const status = httpStatus(error);
  if (status !== undefined) {
    if (status === 401 || status === 403) {
      return turnError("provider_auth", false, status);
    }
    if (status === 408) return turnError("provider_timeout", true, status);
    if (status === 429) return turnError("provider_rate_limited", true, status);
    if (status >= 500) return turnError("provider_unavailable", true, status);
    return turnError("provider_invalid_response", false, status);
  }

  const code = errorCode(error);
  if (code === "ETIMEDOUT" || code === "ESOCKETTIMEDOUT") {
    return turnError("provider_timeout", true);
  }
  if (
    code === "ECONNABORTED" ||
    code === "ECONNREFUSED" ||
    code === "ECONNRESET" ||
    code === "EHOSTUNREACH" ||
    code === "ENETUNREACH" ||
    code === "ENOTFOUND" ||
    code === "EAI_AGAIN" ||
    code.startsWith("UND_ERR_")
  ) {
    return turnError("provider_unavailable", true);
  }

  if (isTimeoutError(error)) return turnError("provider_timeout", true);
  if (error instanceof SyntaxError) {
    return turnError("provider_invalid_response", false);
  }
  if (error instanceof TypeError && error.message.toLowerCase().includes("fetch")) {
    return turnError("provider_unavailable", true);
  }
  return turnError("internal_error", false);
}

function turnError(
  code: TurnErrorCode,
  retryable: boolean,
  status?: number,
): AgentdTurnError {
  return new AgentdTurnError(code, retryable, MESSAGE_BY_CODE[code], status);
}

function isAbortError(error: unknown): boolean {
  return isRecord(error) && error.name === "AbortError";
}

function isTimeoutError(error: unknown): boolean {
  return isRecord(error) && error.name === "TimeoutError";
}

function httpStatus(error: unknown): number | undefined {
  for (const candidate of nestedErrorRecords(error)) {
    const value = candidate.status ?? candidate.statusCode;
    if (
      typeof value === "number" &&
      Number.isInteger(value) &&
      value >= 100 &&
      value <= 599
    ) {
      return value;
    }
  }
  return undefined;
}

function errorCode(error: unknown): string {
  for (const candidate of nestedErrorRecords(error)) {
    if (typeof candidate.code === "string") return candidate.code.toUpperCase();
  }
  return "";
}

function nestedErrorRecords(error: unknown): Record<string, unknown>[] {
  if (!isRecord(error)) return [];
  const records = [error];
  if (isRecord(error.cause)) records.push(error.cause);
  if (isRecord(error.response)) records.push(error.response);
  return records;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
