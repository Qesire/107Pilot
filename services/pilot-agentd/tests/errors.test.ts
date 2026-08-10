import { describe, expect, it } from "vitest";

import { AgentdTurnError, mapProviderError } from "../src/errors.js";

describe("provider error normalization", () => {
  it.each([
    [401, "provider_auth", false],
    [403, "provider_auth", false],
    [408, "provider_timeout", true],
    [429, "provider_rate_limited", true],
    [500, "provider_unavailable", true],
    [503, "provider_unavailable", true],
    [400, "provider_invalid_response", false],
  ] as const)("maps HTTP %i to %s", (status, code, retryable) => {
    const mapped = mapProviderError({
      status,
      body: `provider body secret-${status}`,
      headers: { authorization: "Bearer header-secret" },
    });

    expect(mapped).toMatchObject({ code, retryable, providerStatus: status });
    expect(JSON.stringify(mapped.toPayload())).not.toContain("secret");
  });

  it("maps AbortError without exposing its message", () => {
    const mapped = mapProviderError(
      new DOMException("Bearer abort-secret", "AbortError"),
    );

    expect(mapped).toMatchObject({ code: "aborted", retryable: false });
    expect(mapped.message).not.toContain("abort-secret");
  });

  it("maps fetch transport and timeout failures separately", () => {
    const transport = new TypeError("fetch failed with token=transport-secret");
    Object.assign(transport, { cause: { code: "ECONNREFUSED" } });
    const timeout = Object.assign(new Error("password=timeout-secret"), {
      code: "ETIMEDOUT",
    });

    expect(mapProviderError(transport)).toMatchObject({
      code: "provider_unavailable",
      retryable: true,
    });
    expect(mapProviderError(timeout)).toMatchObject({
      code: "provider_timeout",
      retryable: true,
    });
  });

  it("maps malformed and unknown errors to fixed sanitized messages", () => {
    const malformed = mapProviderError(
      new SyntaxError('Unexpected token in {"api_key":"syntax-secret"}'),
    );
    const unknown = mapProviderError(
      new Error("https://gateway.example/v1?api_key=internal-secret"),
    );

    expect(malformed).toMatchObject({
      code: "provider_invalid_response",
      retryable: false,
    });
    expect(unknown).toMatchObject({ code: "internal_error", retryable: false });
    expect(JSON.stringify(malformed.toPayload())).not.toContain("syntax-secret");
    expect(JSON.stringify(unknown.toPayload())).not.toContain("internal-secret");
  });

  it("omits an absent provider_status from the closed wire payload", () => {
    const payload = new AgentdTurnError(
      "output_contract_violation",
      false,
      "The model did not emit the required result.",
    ).toPayload();

    expect(payload).toEqual({
      code: "output_contract_violation",
      retryable: false,
      message: "The model did not emit the required result.",
    });
    expect(Object.values(payload)).not.toContain(undefined);
  });

  it("sanitizes messages supplied to an explicit turn error", () => {
    const error = new AgentdTurnError(
      "output_contract_violation",
      false,
      "Authorization: Bearer explicit-secret at https://gateway.example/v1?token=url-secret",
    );

    expect(JSON.stringify(error.toPayload())).not.toContain("explicit-secret");
    expect(JSON.stringify(error.toPayload())).not.toContain("url-secret");
  });
});
