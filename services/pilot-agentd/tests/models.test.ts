import { describe, expect, it } from "vitest";

import { configFromEnv } from "../src/config.js";
import {
  createCampusModelRuntime,
  createFauxModelRuntime,
} from "../src/models.js";

const campusEnv: NodeJS.ProcessEnv = {
  NODE_ENV: "production",
  PILOT107_AGENTD_TOKEN: "internal-secret",
  PILOT107_AGENTD_MODEL_PROFILE: "campus-default",
  PILOT107_LLM_BASE_URL: "http://127.0.0.1:4111/v1",
  PILOT107_LLM_API_KEY: "llm-secret",
  PILOT107_LLM_MODEL: "campus-model",
};

describe("Pi model registry", () => {
  it("registers a conservative campus OpenAI completions model", () => {
    const profile = configFromEnv(campusEnv).modelProfile;
    const runtime = createCampusModelRuntime(profile);

    expect(runtime.model).toMatchObject({
      id: "campus-model",
      api: "openai-completions",
      provider: "campus-default",
      baseUrl: "http://127.0.0.1:4111/v1",
      reasoning: false,
      input: ["text"],
      contextWindow: 32_768,
      maxTokens: 1200,
      compat: {
        supportsStore: false,
        supportsDeveloperRole: false,
        supportsReasoningEffort: false,
        supportsUsageInStreaming: false,
        supportsStrictMode: false,
        maxTokensField: "max_tokens",
      },
    });
    expect(runtime.models.getModel("campus-default", "campus-model")).toBe(
      runtime.model,
    );
  });

  it("resolves configured campus auth without leaking or inventing a key", async () => {
    const keyed = createCampusModelRuntime(configFromEnv(campusEnv).modelProfile);
    await expect(keyed.models.getAuth(keyed.model)).resolves.toMatchObject({
      auth: { apiKey: "llm-secret" },
    });

    const keyless = createCampusModelRuntime(
      configFromEnv({ ...campusEnv, PILOT107_LLM_API_KEY: undefined }).modelProfile,
    );
    await expect(keyless.models.getAuth(keyless.model)).resolves.toEqual({ auth: {} });
    await expect(keyless.models.getAvailable("campus-default")).resolves.toEqual([
      keyless.model,
    ]);
  });

  it("rejects incomplete or wrong provider profiles", () => {
    const incomplete = configFromEnv({
      ...campusEnv,
      PILOT107_LLM_BASE_URL: undefined,
    }).modelProfile;
    expect(() => createCampusModelRuntime(incomplete)).toThrow(
      "campus model profile is incomplete",
    );

    const faux = configFromEnv({
      NODE_ENV: "test",
      PILOT107_AGENTD_MODEL_PROFILE: "faux-default",
    }).modelProfile;
    expect(() => createCampusModelRuntime(faux)).toThrow(
      "campus model profile is incomplete",
    );
  });

  it("returns a real registered Pi faux handle that tests can script", async () => {
    const runtime = createFauxModelRuntime();

    expect(runtime.models.getProvider("faux-default")).toBe(runtime.faux.provider);
    expect(runtime.models.getModel("faux-default", runtime.model.id)).toBe(runtime.model);
    expect(runtime.faux.getModel()).toBe(runtime.model);
    expect(runtime.faux.getPendingResponseCount()).toBe(0);
    await expect(runtime.models.getAuth(runtime.model)).resolves.toEqual({ auth: {} });
  });

  it("queues only the fixed server-side a0-smoke sequence", async () => {
    const injected = JSON.stringify({ content: "attacker supplied" });
    const config = configFromEnv({
      NODE_ENV: "production",
      PILOT107_AGENTD_TOKEN: "smoke-token",
      PILOT107_AGENTD_MODEL_PROFILE: "faux-default",
      PILOT107_AGENTD_FAUX_SCENARIO: "a0-smoke",
      PILOT107_AGENTD_FAUX_RESPONSES: injected,
    });
    const runtime = createFauxModelRuntime({
      scenario: config.modelProfile.fauxScenario,
    });

    expect(runtime.faux.getPendingResponseCount()).toBe(6);
    const answer = await runtime.models.completeSimple(runtime.model, {
      messages: [{ role: "user", content: "hello", timestamp: 1 }],
    });
    expect(answer.content).toEqual([
      { type: "text", text: "107Pilot faux assistant is ready." },
    ]);
    expect(JSON.stringify(answer)).not.toContain("attacker supplied");
    expect(runtime.faux.getPendingResponseCount()).toBe(5);

    const explain = await runtime.models.completeSimple(runtime.model, {
      messages: [{ role: "user", content: "explain", timestamp: 2 }],
    });
    expect(explain.content).toContainEqual(
      expect.objectContaining({
        type: "toolCall",
        name: "emit_result",
        arguments: expect.objectContaining({
          citations: [
            {
              fact_id: "fact-smoke",
              evidence_object_ids: ["object-smoke"],
            },
          ],
        }),
      }),
    );

    await runtime.models.completeSimple(runtime.model, {
      messages: [{ role: "user", content: "patch", timestamp: 3 }],
    });
    const remediation = await runtime.models.completeSimple(runtime.model, {
      messages: [{ role: "user", content: "remediate", timestamp: 4 }],
    });
    expect(remediation.content).toContainEqual(
      expect.objectContaining({
        type: "toolCall",
        name: "emit_result",
        arguments: expect.objectContaining({ fact_ids: ["fact-smoke"] }),
      }),
    );
  });

  it("rejects invalid faux throughput", () => {
    expect(() => createFauxModelRuntime({ tokensPerSecond: 0 })).toThrow(
      "tokensPerSecond",
    );
  });
});
