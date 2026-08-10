import { Agent, type AgentEvent, type AgentMessage } from "@earendil-works/pi-agent-core";
import type {
  AssistantMessage,
  SimpleStreamOptions,
} from "@earendil-works/pi-ai";

import {
  checkpointFromState,
  computeCheckpointDigest,
  restoreMessages,
  type CheckpointableAgentState,
} from "./checkpoint.js";
import { AgentdTurnError, mapProviderError } from "./errors.js";
import { mapPiEvent, TurnEventSink, type EventWrite } from "./events.js";
import type { ModelRuntime } from "./models.js";
import type {
  AgentCheckpoint,
  AgentTurnEvent,
  AgentTurnRequest,
  JsonValue,
  TaskKind,
} from "./protocol.js";
import { prepareTask, type PreparedTask } from "./tasks.js";

const RETRY_DELAYS_MS = [100, 400] as const;
const MAX_PROVIDER_CALLS = 3;
const REPAIR_PROMPT =
  "The previous response did not satisfy the required output contract. " +
  "Call emit_result exactly once with arguments that match its schema. " +
  "Do not repeat or quote any prior input.";
const RESUME_PROMPT =
  "Continue the interrupted Turn from the sanitized checkpoint. " +
  "Do not repeat text already present in the checkpoint.";

export type RuntimeResolver = (profileId: string) => ModelRuntime | undefined;
export type Sleep = (milliseconds: number, signal: AbortSignal) => Promise<void>;

export interface RetryDecision {
  readonly taskKind: TaskKind;
  readonly error: AgentdTurnError;
  readonly publicOutputEmitted: boolean;
  readonly attempt: number;
  readonly maxAttempts: number;
}

export function shouldRetry(decision: RetryDecision): boolean {
  return (
    decision.error.retryable &&
    decision.attempt < decision.maxAttempts &&
    (decision.taskKind !== "interactive" || !decision.publicOutputEmitted)
  );
}

interface AttemptBudget {
  providerCalls: number;
  repairUsed: boolean;
}

interface TurnUsageAccumulator {
  readonly available: boolean;
  sawAssistant: boolean;
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
}

interface AttemptSuccess {
  readonly kind: "success";
  readonly result: JsonValue;
  readonly state: CheckpointableAgentState;
}

interface AttemptFailure {
  readonly kind: "failure";
  readonly error: AgentdTurnError;
  readonly state: CheckpointableAgentState;
}

type AttemptOutcome = AttemptSuccess | AttemptFailure;

interface AbortBinding {
  readonly reason: "external" | "timeout" | undefined;
  dispose(): void;
}

export class TurnExecutor {
  constructor(
    private readonly resolveRuntime: RuntimeResolver,
    private readonly sleep: Sleep = abortableSleep,
  ) {}

  async execute(
    request: AgentTurnRequest,
    write: EventWrite,
    outerSignal: AbortSignal,
  ): Promise<void> {
    let publicEventCount = 0;
    const sink = new TurnEventSink(request.turn_id, async (event) => {
      try {
        await write(event);
      } catch (cause) {
        throw new EventWriterError(cause);
      }
      if (isMeaningfulPublicEvent(event)) publicEventCount += 1;
    });

    try {
      await this.executeToSink(
        request,
        sink,
        outerSignal,
        () => publicEventCount,
      );
    } catch (error) {
      if (error instanceof EventWriterError) throw error.cause;
      throw error;
    }
  }

  private async executeToSink(
    request: AgentTurnRequest,
    sink: TurnEventSink,
    outerSignal: AbortSignal,
    publicEventCount: () => number,
  ): Promise<void> {
    const startedAt = Date.now();
    await sink.emit("turn_started", {
      model_profile_id: request.model_profile_id,
      task_kind: request.task_kind,
    });

    let runtime: ModelRuntime | undefined;
    try {
      runtime = this.resolveRuntime(request.model_profile_id);
    } catch (error) {
      await sink.fail(runtimeResolutionError(error));
      return;
    }
    if (runtime === undefined) {
      await sink.fail(unconfiguredRuntimeError());
      return;
    }
    if (runtime.profile.id !== request.model_profile_id) {
      await sink.fail(
        new AgentdTurnError(
          "internal_error",
          false,
          "The resolved model profile did not match the request.",
        ),
      );
      return;
    }

    let restored: AgentMessage[];
    try {
      restored = restoreMessages(request.checkpoint, request, {
        model: runtime.model,
      });
    } catch {
      await sink.fail(
        new AgentdTurnError(
          "internal_error",
          false,
          "The supplied checkpoint could not be restored.",
        ),
      );
      return;
    }

    const timeoutMs = Math.min(
      request.limits.timeout_ms,
      runtime.profile.timeoutMs,
    );
    const deadlineAt = startedAt + timeoutMs;
    const maxTokens = Math.min(
      request.limits.max_output_tokens,
      runtime.profile.maxOutputTokens,
      runtime.model.maxTokens,
    );
    const budget: AttemptBudget = { providerCalls: 0, repairUsed: false };
    const usage = createUsageAccumulator(runtime);
    let lastState: CheckpointableAgentState = { messages: restored };

    for (let attempt = 1; attempt <= runtime.profile.maxAttempts; attempt += 1) {
      if (outerSignal.aborted) {
        await sink.fail(
          abortedTurnError(),
          safeCheckpoint(request, lastState, usage),
        );
        return;
      }
      if (Date.now() >= deadlineAt) {
        await sink.fail(
          timeoutTurnError(),
          safeCheckpoint(request, lastState, usage),
        );
        return;
      }

      const publicBeforeAttempt = publicEventCount();
      let outcome: AttemptOutcome;
      try {
        outcome = await runAttempt({
          request,
          runtime,
          sink,
          outerSignal,
          deadlineAt,
          maxTokens,
          restored,
          budget,
        });
      } catch (error) {
        if (error instanceof EventWriterError) throw error;
        outcome = {
          kind: "failure",
          error: mapProviderError(error),
          state: lastState,
        };
      }
      lastState = outcome.state;
      recordAttemptUsage(
        usage,
        outcome.state.messages.slice(restored.length),
      );

      if (outcome.kind === "success") {
        const checkpoint = checkpointWithUsage(
          checkpointFromState(request, outcome.state),
          normalizedUsage(usage),
        );
        await sink.emit("checkpoint", { checkpoint });
        await sink.complete({
          result: outcome.result,
          provider: runtime.model.provider,
          model: runtime.model.id,
          model_profile_id: runtime.profile.id,
          usage: checkpoint.usage,
          provider_calls: budget.providerCalls,
          checkpoint_digest: checkpoint.digest,
          duration_ms: boundedDuration(startedAt),
          checkpoint,
        });
        return;
      }

      const retry =
        budget.providerCalls < MAX_PROVIDER_CALLS &&
        shouldRetry({
          taskKind: request.task_kind,
          error: outcome.error,
          publicOutputEmitted: publicEventCount() > publicBeforeAttempt,
          attempt,
          maxAttempts: runtime.profile.maxAttempts,
        });
      if (!retry) {
        await sink.fail(
          outcome.error,
          safeCheckpoint(request, outcome.state, usage),
        );
        return;
      }

      const delay = RETRY_DELAYS_MS[attempt - 1] ?? RETRY_DELAYS_MS.at(-1)!;
      if (Date.now() + delay >= deadlineAt) {
        await sink.fail(
          timeoutTurnError(),
          safeCheckpoint(request, outcome.state, usage),
        );
        return;
      }
      try {
        await this.sleep(delay, outerSignal);
      } catch (error) {
        const mapped = outerSignal.aborted ? abortedTurnError() : mapProviderError(error);
        await sink.fail(
          mapped,
          safeCheckpoint(request, outcome.state, usage),
        );
        return;
      }
    }

    await sink.fail(
      new AgentdTurnError(
        "internal_error",
        false,
        "The Turn exhausted its bounded execution attempts.",
      ),
      safeCheckpoint(request, lastState, usage),
    );
  }
}

async function runAttempt(options: {
  readonly request: AgentTurnRequest;
  readonly runtime: ModelRuntime;
  readonly sink: TurnEventSink;
  readonly outerSignal: AbortSignal;
  readonly deadlineAt: number;
  readonly maxTokens: number;
  readonly restored: AgentMessage[];
  readonly budget: AttemptBudget;
}): Promise<AttemptOutcome> {
  const task = prepareTask(options.request);
  const model = { ...options.runtime.model, maxTokens: options.maxTokens };
  const streamSimple = options.runtime.models.streamSimple.bind(
    options.runtime.models,
  );
  let providerStatus: number | undefined;
  let listenerError: unknown;
  const agent = new Agent({
    initialState: {
      systemPrompt: task.systemPrompt,
      model,
      thinkingLevel: "off",
      tools: task.tools,
      messages: options.restored,
    },
    streamFn: (requestModel, context, streamOptions) =>
      streamSimple(requestModel, context, {
        ...streamOptions,
        maxTokens: options.maxTokens,
        timeoutMs: Math.max(1, options.deadlineAt - Date.now()),
        maxRetries: 0,
        maxRetryDelayMs: 0,
        onResponse: async (response, responseModel) => {
          if (response.status >= 400) providerStatus = response.status;
          await streamOptions?.onResponse?.(response, responseModel);
        },
      } satisfies SimpleStreamOptions),
    sessionId: options.request.trace.correlation_id,
    toolExecution: "sequential",
    shouldStopAfterTurn: () => true,
  });
  const unsubscribe = agent.subscribe(async (event: AgentEvent) => {
    if (event.type === "turn_start") options.budget.providerCalls += 1;
    try {
      await mapPiEvent(event, options.sink);
    } catch (error) {
      listenerError ??= error;
      agent.abort();
      throw error;
    }
  });
  const abortBinding = bindAbortToAgent(
    agent,
    options.outerSignal,
    options.deadlineAt,
  );

  try {
    if (abortBinding.reason !== undefined) {
      return {
        kind: "failure",
        error:
          abortBinding.reason === "timeout"
            ? timeoutTurnError()
            : abortedTurnError(),
        state: agent.state,
      };
    }

    await agent.prompt(initialPrompt(options.request, task));
    let promptError = errorAfterPrompt(
      agent,
      listenerError,
      providerStatus,
      abortBinding.reason,
    );
    if (promptError !== undefined) {
      return { kind: "failure", error: promptError, state: agent.state };
    }

    if (
      task.constrained &&
      task.getStructuredResult() === undefined &&
      !options.budget.repairUsed &&
      options.budget.providerCalls < MAX_PROVIDER_CALLS
    ) {
      options.budget.repairUsed = true;
      providerStatus = undefined;
      await agent.prompt(REPAIR_PROMPT);
      promptError = errorAfterPrompt(
        agent,
        listenerError,
        providerStatus,
        abortBinding.reason,
      );
      if (promptError !== undefined) {
        return { kind: "failure", error: promptError, state: agent.state };
      }
    }

    const result = task.constrained
      ? task.getStructuredResult()
      : collectAssistantText(agent.state.messages.slice(options.restored.length));
    if (result === undefined) {
      return {
        kind: "failure",
        error: outputContractError(),
        state: agent.state,
      };
    }
    return {
      kind: "success",
      result: result as JsonValue,
      state: agent.state,
    };
  } finally {
    abortBinding.dispose();
    unsubscribe();
  }
}

function errorAfterPrompt(
  agent: Agent,
  listenerError: unknown,
  providerStatus: number | undefined,
  abortReason: AbortBinding["reason"],
): AgentdTurnError | undefined {
  if (listenerError !== undefined) {
    if (listenerError instanceof EventWriterError) throw listenerError;
    return mapProviderError(listenerError);
  }
  if (abortReason === "external") return abortedTurnError();
  if (abortReason === "timeout") return timeoutTurnError();
  const stopped = lastAssistant(agent.state.messages);
  if (stopped?.stopReason === "error" || stopped?.stopReason === "aborted") {
    return errorFromStoppedMessage(stopped, providerStatus);
  }
  return undefined;
}

function initialPrompt(
  request: AgentTurnRequest,
  task: PreparedTask,
): string {
  return request.checkpoint !== null && request.checkpoint.turn_id === request.turn_id
    ? RESUME_PROMPT
    : task.userMessage;
}

function collectAssistantText(messages: readonly AgentMessage[]): string {
  return messages
    .filter((message): message is AssistantMessage => message.role === "assistant")
    .flatMap((message) => message.content)
    .filter((content) => content.type === "text")
    .map((content) => content.text)
    .join("");
}

function lastAssistant(
  messages: readonly AgentMessage[],
): AssistantMessage | undefined {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role === "assistant") return message;
  }
  return undefined;
}

function errorFromStoppedMessage(
  message: AssistantMessage,
  providerStatus: number | undefined,
): AgentdTurnError {
  if (message.stopReason === "aborted") return abortedTurnError();
  if (providerStatus !== undefined) {
    return mapProviderError(Object.assign(new Error("provider request failed"), {
      status: providerStatus,
    }));
  }

  const detail = message.errorMessage ?? "";
  const status = providerStatusFromMessage(detail);
  if (status !== undefined) {
    return mapProviderError(
      Object.assign(new Error("provider request failed"), {
        status,
      }),
    );
  }
  const transportCode = /\b(E[A-Z]+|UND_ERR_[A-Z_]+)\b/.exec(detail)?.[1];
  if (transportCode !== undefined) {
    return mapProviderError(
      Object.assign(new Error("provider transport failed"), {
        code: transportCode,
      }),
    );
  }
  if (/timeout|timed out/i.test(detail)) return timeoutTurnError();
  if (/fetch|network|socket|connection|unavailable/i.test(detail)) {
    return new AgentdTurnError(
      "provider_unavailable",
      true,
      "The model provider is unavailable.",
    );
  }
  return new AgentdTurnError(
    "provider_invalid_response",
    false,
    "The model provider returned an invalid response.",
  );
}

function bindAbortToAgent(
  agent: Agent,
  outerSignal: AbortSignal,
  deadlineAt: number,
): AbortBinding {
  let reason: AbortBinding["reason"];
  const abort = (next: Exclude<AbortBinding["reason"], undefined>) => {
    if (reason !== undefined) return;
    reason = next;
    agent.abort();
  };
  const onOuterAbort = () => abort("external");
  outerSignal.addEventListener("abort", onOuterAbort, { once: true });
  if (outerSignal.aborted) abort("external");

  const remaining = deadlineAt - Date.now();
  let timer: ReturnType<typeof setTimeout> | undefined;
  if (reason === undefined) {
    if (remaining <= 0) {
      abort("timeout");
    } else {
      timer = setTimeout(() => abort("timeout"), remaining);
    }
  }

  return {
    get reason() {
      return reason;
    },
    dispose() {
      outerSignal.removeEventListener("abort", onOuterAbort);
      if (timer !== undefined) clearTimeout(timer);
    },
  };
}

function safeCheckpoint(
  request: AgentTurnRequest,
  state: CheckpointableAgentState,
  usage?: TurnUsageAccumulator,
): AgentCheckpoint | undefined {
  try {
    const checkpoint = checkpointFromState(request, state);
    return usage === undefined
      ? checkpoint
      : checkpointWithUsage(checkpoint, normalizedUsage(usage));
  } catch {
    return undefined;
  }
}

function providerStatusFromMessage(message: string): number | undefined {
  const leading = /^\s*([1-5][0-9]{2})(?=\s|:|-|$)/.exec(message)?.[1];
  if (leading !== undefined) return Number(leading);
  const fixedProvider =
    /^\s*(?:OpenAI|Azure OpenAI) API error\s*\(\s*([1-5][0-9]{2})\s*\)\s*:/i.exec(
      message,
    )?.[1];
  return fixedProvider === undefined ? undefined : Number(fixedProvider);
}

function createUsageAccumulator(runtime: ModelRuntime): TurnUsageAccumulator {
  const compat = runtime.model.compat;
  const streamingUsageDisabled =
    compat !== undefined &&
    "supportsUsageInStreaming" in compat &&
    compat.supportsUsageInStreaming === false;
  return {
    available:
      runtime.profile.provider === "faux" ||
      !streamingUsageDisabled,
    sawAssistant: false,
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
  };
}

function recordAttemptUsage(
  accumulator: TurnUsageAccumulator,
  messages: readonly AgentMessage[],
): void {
  if (!accumulator.available) return;
  for (const message of messages) {
    if (message.role !== "assistant") continue;
    accumulator.sawAssistant = true;
    accumulator.input += message.usage.input;
    accumulator.output += message.usage.output;
    accumulator.cacheRead += message.usage.cacheRead;
    accumulator.cacheWrite += message.usage.cacheWrite;
  }
}

function normalizedUsage(
  accumulator: TurnUsageAccumulator,
): AgentCheckpoint["usage"] {
  if (!accumulator.available || !accumulator.sawAssistant) {
    return {
      input_tokens: null,
      output_tokens: null,
      cache_read_tokens: null,
      cache_write_tokens: null,
    };
  }
  return {
    input_tokens: accumulator.input,
    output_tokens: accumulator.output,
    cache_read_tokens: accumulator.cacheRead,
    cache_write_tokens: accumulator.cacheWrite,
  };
}

function checkpointWithUsage(
  checkpoint: AgentCheckpoint,
  usage: AgentCheckpoint["usage"],
): AgentCheckpoint {
  const updated = { ...checkpoint, usage };
  return { ...updated, digest: computeCheckpointDigest(updated) };
}

function runtimeResolutionError(error: unknown): AgentdTurnError {
  if (error instanceof AgentdTurnError) return error;
  if (
    error instanceof Error &&
    /(?:profile|provider).*(?:unavailable|not configured|incomplete)/i.test(
      error.message,
    )
  ) {
    return unconfiguredRuntimeError();
  }
  return mapProviderError(error);
}

function unconfiguredRuntimeError(): AgentdTurnError {
  return new AgentdTurnError(
    "provider_unavailable",
    false,
    "The requested model profile is not configured.",
  );
}

function outputContractError(): AgentdTurnError {
  return new AgentdTurnError(
    "output_contract_violation",
    false,
    "The model did not emit the required result.",
  );
}

function abortedTurnError(): AgentdTurnError {
  return new AgentdTurnError("aborted", false, "The Turn was aborted.");
}

function timeoutTurnError(): AgentdTurnError {
  return new AgentdTurnError(
    "provider_timeout",
    true,
    "The model provider request timed out.",
  );
}

function isMeaningfulPublicEvent(event: AgentTurnEvent): boolean {
  return (
    event.type === "message_delta" ||
    event.type === "tool_call_requested" ||
    event.type === "tool_call_started" ||
    event.type === "tool_call_progress" ||
    event.type === "tool_call_completed"
  );
}

function boundedDuration(startedAt: number): number {
  return Math.min(3_600_000, Math.max(0, Date.now() - startedAt));
}

class EventWriterError extends Error {
  constructor(readonly cause: unknown) {
    super("turn event writer rejected");
    this.name = "EventWriterError";
  }
}

function abortableSleep(
  milliseconds: number,
  signal: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError());
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
      reject(abortError());
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function abortError(): Error {
  const error = new Error("aborted");
  error.name = "AbortError";
  return error;
}
