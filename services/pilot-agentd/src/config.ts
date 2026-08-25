import { AGENTD_VERSION } from "./version.js";

const DEFAULT_HOST = "0.0.0.0";
const DEFAULT_PORT = 8091;
const DEFAULT_TIMEOUT_SECONDS = 60;
const DEFAULT_MAX_OUTPUT_TOKENS = 1200;
const DEFAULT_MAX_ATTEMPTS = 2;
const DEFAULT_CONTEXT_WINDOW = 32_768;

export interface ModelProfile {
  readonly id: string;
  readonly provider: "campus-openai-compatible" | "faux";
  readonly baseUrl?: string;
  readonly model: string;
  readonly apiKey?: string;
  readonly timeoutMs: number;
  readonly maxOutputTokens: number;
  readonly maxAttempts: 1 | 2 | 3;
  readonly contextWindow: number;
  readonly fauxScenario?: "a0-smoke" | "a1-smoke";
}

export interface PublicAgentdConfig {
  readonly version: string;
  readonly model_profile_id: string;
  readonly configured: boolean;
}

export interface AgentdConfig {
  readonly host: string;
  readonly port: number;
  readonly internalToken: string;
  readonly toolGatewayUrl?: string;
  readonly modelProfile: ModelProfile;
  readonly configured: boolean;
  publicSummary(): PublicAgentdConfig;
}

function optionalNonEmpty(value: string | undefined): string | undefined {
  if (value === undefined || value.trim() === "") return undefined;
  return value;
}

function integerFromEnv(
  env: NodeJS.ProcessEnv,
  name: string,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const raw = optionalNonEmpty(env[name]);
  if (raw === undefined) return fallback;
  if (!/^[0-9]+$/.test(raw)) {
    throw new TypeError(`${name} must be an integer`);
  }
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new RangeError(`${name} is outside the supported range`);
  }
  return parsed;
}

function parseBaseUrl(value: string | undefined): string | undefined {
  const raw = optionalNonEmpty(value);
  if (raw === undefined) return undefined;
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new TypeError("PILOT107_LLM_BASE_URL must be a valid URL");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new TypeError("PILOT107_LLM_BASE_URL must use HTTP or HTTPS");
  }
  if (parsed.username !== "" || parsed.password !== "") {
    throw new TypeError("PILOT107_LLM_BASE_URL must not contain userinfo");
  }
  return raw;
}

function parseToolGatewayUrl(value: string | undefined): string | undefined {
  const raw = optionalNonEmpty(value);
  if (raw === undefined) return undefined;
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new TypeError("PILOT107_AGENTD_TOOL_GATEWAY_URL must be a valid URL");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new TypeError(
      "PILOT107_AGENTD_TOOL_GATEWAY_URL must use HTTP or HTTPS",
    );
  }
  if (parsed.username !== "" || parsed.password !== "") {
    throw new TypeError(
      "PILOT107_AGENTD_TOOL_GATEWAY_URL must not contain userinfo",
    );
  }
  if (parsed.hash !== "") {
    throw new TypeError(
      "PILOT107_AGENTD_TOOL_GATEWAY_URL must not contain a fragment",
    );
  }
  return raw;
}

function maxAttempts(value: number): 1 | 2 | 3 {
  if (value === 1 || value === 2 || value === 3) return value;
  throw new RangeError("PILOT107_LLM_MAX_ATTEMPTS is outside the supported range");
}

function campusProfile(env: NodeJS.ProcessEnv, id: string): ModelProfile {
  const baseUrl = parseBaseUrl(env.PILOT107_LLM_BASE_URL);
  const model = optionalNonEmpty(env.PILOT107_LLM_MODEL) ?? "";
  const apiKey = optionalNonEmpty(env.PILOT107_LLM_API_KEY);
  const timeoutSeconds = integerFromEnv(
    env,
    "PILOT107_LLM_TIMEOUT_SECONDS",
    DEFAULT_TIMEOUT_SECONDS,
    1,
    600,
  );
  const maxOutputTokens = integerFromEnv(
    env,
    "PILOT107_LLM_MAX_TOKENS",
    DEFAULT_MAX_OUTPUT_TOKENS,
    1,
    32_000,
  );
  const attempts = maxAttempts(
    integerFromEnv(
      env,
      "PILOT107_LLM_MAX_ATTEMPTS",
      DEFAULT_MAX_ATTEMPTS,
      1,
      3,
    ),
  );

  return Object.freeze({
    id,
    provider: "campus-openai-compatible" as const,
    ...(baseUrl === undefined ? {} : { baseUrl }),
    model,
    ...(apiKey === undefined ? {} : { apiKey }),
    timeoutMs: timeoutSeconds * 1000,
    maxOutputTokens,
    maxAttempts: attempts,
    contextWindow: DEFAULT_CONTEXT_WINDOW,
  });
}

function fauxProfile(env: NodeJS.ProcessEnv): ModelProfile {
  const rawScenario = optionalNonEmpty(env.PILOT107_AGENTD_FAUX_SCENARIO);
  if (
    rawScenario !== undefined &&
    rawScenario !== "a0-smoke" &&
    rawScenario !== "a1-smoke"
  ) {
    throw new TypeError(
      "PILOT107_AGENTD_FAUX_SCENARIO must name a supported server-side scenario",
    );
  }
  if (
    env.NODE_ENV !== "test" &&
    rawScenario !== "a0-smoke" &&
    rawScenario !== "a1-smoke"
  ) {
    throw new TypeError(
      "PILOT107_AGENTD_FAUX_SCENARIO is required for faux-default outside tests",
    );
  }

  return Object.freeze({
    id: "faux-default",
    provider: "faux" as const,
    model: "faux-1",
    timeoutMs: DEFAULT_TIMEOUT_SECONDS * 1000,
    maxOutputTokens: DEFAULT_MAX_OUTPUT_TOKENS,
    maxAttempts: DEFAULT_MAX_ATTEMPTS,
    contextWindow: 128_000,
    ...(rawScenario === "a0-smoke" || rawScenario === "a1-smoke"
      ? { fauxScenario: rawScenario }
      : {}),
  });
}

export function configFromEnv(env: NodeJS.ProcessEnv): AgentdConfig {
  const host = optionalNonEmpty(env.PILOT107_AGENTD_LISTEN_HOST) ?? DEFAULT_HOST;
  const port = integerFromEnv(
    env,
    "PILOT107_AGENTD_LISTEN_PORT",
    DEFAULT_PORT,
    1,
    65_535,
  );
  const internalToken = optionalNonEmpty(env.PILOT107_AGENTD_TOKEN) ?? "";
  if (env.NODE_ENV !== "test" && internalToken === "") {
    throw new TypeError("PILOT107_AGENTD_TOKEN must be non-empty outside tests");
  }
  const toolGatewayUrl = parseToolGatewayUrl(
    env.PILOT107_AGENTD_TOOL_GATEWAY_URL,
  );

  const profileId = optionalNonEmpty(env.PILOT107_AGENTD_MODEL_PROFILE) ?? "campus-default";
  let modelProfile: ModelProfile;
  if (profileId === "faux-default") {
    modelProfile = fauxProfile(env);
  } else if (profileId === "campus-default") {
    modelProfile = campusProfile(env, profileId);
  } else {
    throw new TypeError("PILOT107_AGENTD_MODEL_PROFILE is not supported");
  }

  const configured =
    modelProfile.provider === "faux" ||
    (modelProfile.baseUrl !== undefined && modelProfile.model !== "");
  const publicSummary = (): PublicAgentdConfig => ({
    version: AGENTD_VERSION,
    model_profile_id: modelProfile.id,
    configured,
  });

  return Object.freeze({
    host,
    port,
    internalToken,
    ...(toolGatewayUrl === undefined ? {} : { toolGatewayUrl }),
    modelProfile,
    configured,
    publicSummary,
  });
}
