import { describe, expect, it } from "vitest";

import { configFromEnv } from "../src/config.js";

const campusEnv: NodeJS.ProcessEnv = {
  NODE_ENV: "production",
  PILOT107_AGENTD_TOKEN: "internal-secret",
  PILOT107_AGENTD_MODEL_PROFILE: "campus-default",
  PILOT107_LLM_BASE_URL: "http://127.0.0.1:4111/v1",
  PILOT107_LLM_API_KEY: "llm-secret",
  PILOT107_LLM_MODEL: "campus-model",
  PILOT107_LLM_TIMEOUT_SECONDS: "60",
  PILOT107_LLM_MAX_TOKENS: "1200",
  PILOT107_LLM_MAX_ATTEMPTS: "2",
};

describe("pilot-agentd configuration", () => {
  it("parses the phase-aware Builder flag closed and defaults it off", () => {
    expect(configFromEnv(campusEnv).phaseAwareBuilder).toBe(false);
    expect(configFromEnv({
      ...campusEnv,
      PILOT107_PHASE_AWARE_BUILDER: "1",
    }).phaseAwareBuilder).toBe(true);
    expect(() => configFromEnv({
      ...campusEnv,
      PILOT107_PHASE_AWARE_BUILDER: "true",
    })).toThrow("PILOT107_PHASE_AWARE_BUILDER");
  });

  it("builds a bounded campus profile and exposes only a redacted summary", () => {
    const config = configFromEnv(campusEnv);

    expect(config).toMatchObject({
      host: "0.0.0.0",
      port: 8091,
      internalToken: "internal-secret",
      configured: true,
      modelProfile: {
        id: "campus-default",
        provider: "campus-openai-compatible",
        baseUrl: "http://127.0.0.1:4111/v1",
        model: "campus-model",
        apiKey: "llm-secret",
        timeoutMs: 60_000,
        maxOutputTokens: 1200,
        maxAttempts: 2,
        contextWindow: 32_768,
      },
    });
    expect(config.publicSummary()).toEqual({
      version: "0.1.0",
      model_profile_id: "campus-default",
      configured: true,
    });

    const publicJson = JSON.stringify(config.publicSummary());
    expect(publicJson).not.toContain("llm-secret");
    expect(publicJson).not.toContain("internal-secret");
    expect(publicJson).not.toContain("127.0.0.1");
  });

  it("accepts the bounded ten-minute reasoner window", () => {
    const config = configFromEnv({
      ...campusEnv,
      PILOT107_LLM_TIMEOUT_SECONDS: "600",
    });

    expect(config.modelProfile.timeoutMs).toBe(600_000);
  });

  it.each([
    { PILOT107_LLM_BASE_URL: undefined },
    { PILOT107_LLM_MODEL: undefined },
  ])("starts with a degraded campus capability when %s is missing", (override) => {
    const config = configFromEnv({ ...campusEnv, ...override });

    expect(config.configured).toBe(false);
    expect(config.publicSummary().configured).toBe(false);
  });

  it("allows a keyless campus gateway and preserves configured readiness", () => {
    const config = configFromEnv({ ...campusEnv, PILOT107_LLM_API_KEY: undefined });

    expect(config.configured).toBe(true);
    expect(config.modelProfile.apiKey).toBeUndefined();
  });

  it("accepts an optional private Tool Gateway URL without exposing it publicly", () => {
    const config = configFromEnv({
      ...campusEnv,
      PILOT107_AGENTD_TOOL_GATEWAY_URL:
        "http://pilot107-api:8080/internal/v1/agent-tools/invoke",
    });

    expect(config.toolGatewayUrl).toBe(
      "http://pilot107-api:8080/internal/v1/agent-tools/invoke",
    );
    expect(config.publicSummary()).toEqual({
      version: "0.1.0",
      model_profile_id: "campus-default",
      configured: true,
    });
  });

  it.each([
    "ftp://pilot107-api/internal/v1/agent-tools/invoke",
    "http://student:secret@pilot107-api/internal/v1/agent-tools/invoke",
    "http://pilot107-api/internal/v1/agent-tools/invoke#secret",
    "not-a-url",
  ])("rejects unsafe Tool Gateway URL without echoing %s", (toolGatewayUrl) => {
    const invalid = {
      ...campusEnv,
      PILOT107_AGENTD_TOOL_GATEWAY_URL: toolGatewayUrl,
    };

    expect(() => configFromEnv(invalid)).toThrow(
      "PILOT107_AGENTD_TOOL_GATEWAY_URL",
    );
    try {
      configFromEnv(invalid);
    } catch (error) {
      expect(String(error)).not.toContain(toolGatewayUrl);
      expect(String(error)).not.toContain("student:secret");
    }
  });

  it.each([
    ["PILOT107_AGENTD_LISTEN_PORT", "0"],
    ["PILOT107_AGENTD_LISTEN_PORT", "8091.5"],
    ["PILOT107_LLM_TIMEOUT_SECONDS", "601"],
    ["PILOT107_LLM_MAX_TOKENS", "32001"],
    ["PILOT107_LLM_MAX_ATTEMPTS", "4"],
  ])("rejects an out-of-range %s without echoing its value", (name, value) => {
    const invalid = { ...campusEnv, [name]: value };

    expect(() => configFromEnv(invalid)).toThrow(name);
    try {
      configFromEnv(invalid);
    } catch (error) {
      expect((error as Error).message).toMatch(
        new RegExp(
          `^${name} (?:must be an integer|is outside the supported range)$`,
        ),
      );
    }
  });

  it.each([
    "ftp://gateway.example.edu/v1",
    "https://student:password@gateway.example.edu/v1",
  ])("rejects unsafe campus URL %s without disclosing it", (baseUrl) => {
    const invalid = { ...campusEnv, PILOT107_LLM_BASE_URL: baseUrl };

    expect(() => configFromEnv(invalid)).toThrow("PILOT107_LLM_BASE_URL");
    try {
      configFromEnv(invalid);
    } catch (error) {
      expect(String(error)).not.toContain(baseUrl);
      expect(String(error)).not.toContain("password");
    }
  });

  it("requires the internal token outside tests without exposing other config", () => {
    const invalid = { ...campusEnv, PILOT107_AGENTD_TOKEN: "" };

    expect(() => configFromEnv(invalid)).toThrow("PILOT107_AGENTD_TOKEN");
    try {
      configFromEnv(invalid);
    } catch (error) {
      expect(String(error)).not.toContain("llm-secret");
      expect(String(error)).not.toContain("127.0.0.1");
    }
  });

  it("allows faux-default only for tests or the fixed a0-smoke scenario", () => {
    const testConfig = configFromEnv({
      NODE_ENV: "test",
      PILOT107_AGENTD_MODEL_PROFILE: "faux-default",
    });
    expect(testConfig).toMatchObject({
      configured: true,
      modelProfile: { provider: "faux" },
    });
    expect(testConfig.modelProfile.fauxScenario).toBeUndefined();

    const smokeConfig = configFromEnv({
      NODE_ENV: "production",
      PILOT107_AGENTD_TOKEN: "smoke-token",
      PILOT107_AGENTD_MODEL_PROFILE: "faux-default",
      PILOT107_AGENTD_FAUX_SCENARIO: "a0-smoke",
    });
    expect(smokeConfig.modelProfile.fauxScenario).toBe("a0-smoke");

    const a1SmokeConfig = configFromEnv({
      NODE_ENV: "production",
      PILOT107_AGENTD_TOKEN: "smoke-token",
      PILOT107_AGENTD_MODEL_PROFILE: "faux-default",
      PILOT107_AGENTD_FAUX_SCENARIO: "a1-smoke",
    });
    expect(a1SmokeConfig.modelProfile.fauxScenario).toBe("a1-smoke");

    expect(() =>
      configFromEnv({
        NODE_ENV: "production",
        PILOT107_AGENTD_TOKEN: "smoke-token",
        PILOT107_AGENTD_MODEL_PROFILE: "faux-default",
      }),
    ).toThrow("PILOT107_AGENTD_FAUX_SCENARIO");
    expect(() =>
      configFromEnv({
        NODE_ENV: "test",
        PILOT107_AGENTD_MODEL_PROFILE: "faux-default",
        PILOT107_AGENTD_FAUX_SCENARIO: "arbitrary-json",
      }),
    ).toThrow("PILOT107_AGENTD_FAUX_SCENARIO");
  });
});
