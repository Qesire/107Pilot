import {
  createModels,
  createProvider,
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  fauxToolCall,
  type FetchFunction,
  type Model,
  type ProviderStreams,
  type SimpleStreamOptions,
  type StreamOptions,
} from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";

import type { ModelProfile } from "./config.js";

export interface ModelRuntime {
  readonly profile: ModelProfile;
  readonly models: ReturnType<typeof createModels>;
  readonly model: Model<any>;
}

export interface FauxModelRuntime extends ModelRuntime {
  readonly faux: ReturnType<typeof fauxProvider>;
}

export interface FauxModelRuntimeOptions {
  readonly tokensPerSecond?: number;
  readonly scenario?: "a0-smoke";
}

function smokeResponses() {
  return [
    fauxAssistantMessage([fauxText("107Pilot faux assistant is ready.")], {
      timestamp: 1,
    }),
    fauxAssistantMessage(
      [
        fauxToolCall(
          "emit_result",
          {
            summary: "检测到可复现的作业问题。",
            narrative: "该结论来自本地 faux 场景中的证据。",
            recommendations: ["检查作业日志与资源请求。"],
            warnings: [],
            citations: [],
          },
          { id: "a0-smoke-explain" },
        ),
      ],
      { stopReason: "toolUse", timestamp: 2 },
    ),
    fauxAssistantMessage(
      [
        fauxToolCall(
          "emit_result",
          {
            suggested_patch: { "resources.cpus_per_task": 2 },
            explanation_zh: "建议在确认后调整 CPU 请求。",
          },
          { id: "a0-smoke-contract-patch" },
        ),
      ],
      { stopReason: "toolUse", timestamp: 3 },
    ),
    fauxAssistantMessage(
      [
        fauxToolCall(
          "emit_result",
          {
            schema_version: "pilot107.remediation-plan/v1",
            summary: "使用受限的本地诊断步骤。",
            fact_ids: [],
            required_inputs: [],
            proposals: [],
            stop_conditions: ["证据不足时停止。"],
          },
          { id: "a0-smoke-remediation" },
        ),
      ],
      { stopReason: "toolUse", timestamp: 4 },
    ),
    fauxAssistantMessage(
      [fauxText("This response is intentionally long enough for cancellation testing.")],
      { timestamp: 5 },
    ),
    fauxAssistantMessage(
      [fauxText("The faux turn resumed from its safe checkpoint.")],
      { timestamp: 6 },
    ),
  ];
}

export function createFauxModelRuntime(
  options: FauxModelRuntimeOptions = {},
): FauxModelRuntime {
  if (
    options.tokensPerSecond !== undefined &&
    (!Number.isFinite(options.tokensPerSecond) || options.tokensPerSecond <= 0)
  ) {
    throw new RangeError("tokensPerSecond must be a positive finite number");
  }

  const faux = fauxProvider({
    api: "faux",
    provider: "faux-default",
    models: [
      {
        id: "faux-1",
        name: "107Pilot Faux Model",
        reasoning: false,
        input: ["text"],
        contextWindow: 128_000,
        maxTokens: 1200,
      },
    ],
    ...(options.tokensPerSecond === undefined
      ? {}
      : { tokensPerSecond: options.tokensPerSecond }),
  });
  const models = createModels();
  models.setProvider(faux.provider);
  if (options.scenario === "a0-smoke") {
    faux.setResponses(smokeResponses());
  }
  const model = faux.getModel();
  const profile: ModelProfile = Object.freeze({
    id: "faux-default",
    provider: "faux",
    model: model.id,
    timeoutMs: 60_000,
    maxOutputTokens: model.maxTokens,
    maxAttempts: 2,
    contextWindow: model.contextWindow,
    ...(options.scenario === undefined ? {} : { fauxScenario: options.scenario }),
  });
  return Object.freeze({ profile, models, model, faux });
}

export function createCampusModelRuntime(profile: ModelProfile): ModelRuntime {
  if (
    profile.provider !== "campus-openai-compatible" ||
    profile.baseUrl === undefined ||
    profile.model === ""
  ) {
    throw new TypeError("campus model profile is incomplete");
  }

  const model: Model<"openai-completions"> = {
    id: profile.model,
    name: profile.model,
    api: "openai-completions",
    provider: profile.id,
    baseUrl: profile.baseUrl,
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: profile.contextWindow,
    maxTokens: profile.maxOutputTokens,
    compat: {
      supportsStore: false,
      supportsDeveloperRole: false,
      supportsReasoningEffort: false,
      supportsUsageInStreaming: false,
      supportsStrictMode: false,
      maxTokensField: "max_tokens",
    },
  };
  const provider = createProvider({
    id: profile.id,
    name: "107Pilot campus gateway",
    baseUrl: profile.baseUrl,
    auth: {
      apiKey: {
        name: "107Pilot campus gateway key",
        resolve: async () => ({
          auth: profile.apiKey === undefined ? {} : { apiKey: profile.apiKey },
        }),
      },
    },
    models: [model],
    api: requireOpenAISseDone(openAICompletionsApi()),
  });
  const models = createModels();
  models.setProvider(provider);
  return Object.freeze({ profile, models, model });
}

function requireOpenAISseDone(api: ProviderStreams): ProviderStreams {
  return {
    ...api,
    stream: (model, context, options) =>
      api.stream(model, context, withValidatedSse(options)),
    streamSimple: (model, context, options) =>
      api.streamSimple(model, context, withValidatedSse(options)),
  };
}

function withValidatedSse<T extends StreamOptions | SimpleStreamOptions>(
  options: T | undefined,
): T {
  const baseFetch = options?.fetch ?? globalThis.fetch.bind(globalThis);
  return {
    ...options,
    fetch: validatingOpenAISseFetch(baseFetch),
  } as T;
}

function validatingOpenAISseFetch(baseFetch: FetchFunction): FetchFunction {
  return async (input, init) => {
    const response = await baseFetch(input, init);
    if (
      !response.ok ||
      response.body === null ||
      !response.headers.get("content-type")?.toLowerCase().startsWith("text/event-stream")
    ) {
      return response;
    }

    const detector = new OpenAISseDoneDetector();
    const body = response.body.pipeThrough(
      new TransformStream<Uint8Array, Uint8Array>({
        transform(chunk, controller) {
          detector.push(chunk);
          controller.enqueue(chunk);
        },
        flush() {
          detector.finish();
        },
      }),
    );
    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  };
}

class OpenAISseDoneDetector {
  readonly #decoder = new TextDecoder();
  #pending = "";
  #dataLines: string[] = [];
  #sawDone = false;

  push(chunk: Uint8Array): void {
    this.#pending += this.#decoder.decode(chunk, { stream: true });
    this.#consumeCompleteLines();
  }

  finish(): void {
    this.#pending += this.#decoder.decode();
    this.#consumeCompleteLines();
    if (this.#pending !== "") {
      this.#consumeLine(this.#pending);
      this.#pending = "";
    }
    if (!this.#sawDone) {
      throw new SyntaxError("OpenAI SSE stream ended without a [DONE] event.");
    }
  }

  #consumeCompleteLines(): void {
    while (true) {
      const newline = this.#pending.indexOf("\n");
      if (newline < 0) return;
      const rawLine = this.#pending.slice(0, newline);
      this.#pending = this.#pending.slice(newline + 1);
      this.#consumeLine(rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine);
    }
  }

  #consumeLine(line: string): void {
    if (line === "") {
      if (this.#dataLines.join("\n") === "[DONE]") this.#sawDone = true;
      this.#dataLines = [];
      return;
    }
    if (line.startsWith(":")) return;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    if (field !== "data") return;
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    this.#dataLines.push(value);
  }
}
