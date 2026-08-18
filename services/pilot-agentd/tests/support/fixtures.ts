import type {
  AgentCheckpoint,
  AgentTurnEvent,
  AgentTurnRequest,
  ConstrainedTaskKind,
  DurableAgentTurnRequest,
} from "../../src/protocol.js";

const BASE = {
  schema_version: "pilot107.agent-turn-request/v1" as const,
  turn_id: "turn-1",
  model_profile_id: "faux-default",
  checkpoint: null,
  limits: { timeout_ms: 1_000, max_output_tokens: 128 },
  trace: { correlation_id: "test-1" },
};

export function interactiveRequest(
  options: { checkpoint?: AgentCheckpoint | null } = {},
): AgentTurnRequest {
  return {
    ...BASE,
    task_kind: "interactive",
    prompt_profile_id: "hpc-assistant-v1",
    toolset_id: "a0-none",
    input: {
      message: "hello",
      context_blocks: [],
    },
    checkpoint: options.checkpoint ?? null,
  } as AgentTurnRequest;
}

export function durableRequest(
  options: Partial<DurableAgentTurnRequest> = {},
): DurableAgentTurnRequest {
  return {
    schema_version: "pilot107.agent-turn-request/v2",
    session_id: "session-1",
    turn_id: "turn-1",
    owner: "alice",
    state_version: 7,
    task_kind: "interactive_readonly",
    model_profile_id: "faux-default",
    prompt_profile_id: "hpc-readonly-v1",
    toolset_id: "a1-readonly",
    input: {
      message: "inspect run-1",
      context_refs: ["run:run-1"],
    },
    capability_token: "opaque.capability.token",
    checkpoint: null,
    limits: { timeout_ms: 30_000, max_output_tokens: 256 },
    trace: { correlation_id: "turn-1" },
    ...options,
  };
}

export function explainRequest(fact: Record<string, unknown> = {}): AgentTurnRequest {
  return {
    ...BASE,
    task_kind: "explain",
    prompt_profile_id: "agent-explain-v1",
    toolset_id: "emit-explanation-v1",
    input: {
      run_id: "run-1",
      status: "FAILED",
      deterministic_summary: "The job failed.",
      facts: [
        {
          fact_id: "fact-1",
          statement: "The process exited with code 1.",
          evidence_refs: ["evidence://run-1/stderr"],
          evidence_object_ids: ["object-1"],
          confidence: "high",
          ...fact,
        },
      ],
      bound_evidence: [
        {
          object_id: "object-1",
          evidence_ref: "evidence://run-1/stderr",
          logical_path: "stderr.txt",
          sha256: "a".repeat(64),
          mime_type: "text/plain",
          trust: "untrusted",
          snippet: "exit code 1",
          truncated: false,
          redactions: [],
        },
      ],
      code_context: null,
      diagnoses: [],
      required_output: {
        summary: "one sentence, grounded in facts",
        narrative: "short Chinese explanation for the user",
        recommendations: "array of concrete next actions",
        warnings: "array of uncertainty notes",
        citations: "one item per fact_id",
      },
    },
  } as AgentTurnRequest;
}

export function contractPatchRequest(): AgentTurnRequest {
  return {
    ...BASE,
    task_kind: "contract_patch",
    prompt_profile_id: "contract-patch-v1",
    toolset_id: "emit-contract-patch-v1",
    input: {
      recipe_version_id: "recipe-v1",
      user_intent: "use four CPUs",
      current_contract: { resources: { cpus_per_task: 2 } },
      required_output: {
        suggested_patch: "Contract dot-paths mapped to new values",
        explanation_zh: "brief Chinese explanation",
      },
    },
  } as AgentTurnRequest;
}

export function remediationRequest(): AgentTurnRequest {
  return {
    ...BASE,
    task_kind: "remediation_plan",
    prompt_profile_id: "remediation-plan-v1",
    toolset_id: "emit-remediation-plan-v1",
    input: {
      run_id: "run-1",
      facts: [
        {
          fact_id: "fact-1",
          statement: "The process exceeded memory.",
          evidence_object_ids: ["object-1"],
          confidence: "high",
        },
      ],
      policy: {
        allowed_action_types: ["contract_patch"],
        allowed_contract_patch_fields: ["resources.memory"],
        arbitrary_shell: false,
        proposal_is_execution_authority: false,
      },
    },
  } as AgentTurnRequest;
}

export function requestFor(kind: ConstrainedTaskKind): AgentTurnRequest {
  switch (kind) {
    case "explain":
      return explainRequest();
    case "contract_patch":
      return contractPatchRequest();
    case "remediation_plan":
      return remediationRequest();
  }
}

export function terminal(events: AgentTurnEvent[]): AgentTurnEvent {
  const terminalEvents = events.filter(
    (event) => event.type === "turn_completed" || event.type === "turn_failed",
  );
  if (terminalEvents.length !== 1) {
    throw new Error(`expected exactly one terminal event, got ${terminalEvents.length}`);
  }
  return terminalEvents[0] as AgentTurnEvent;
}

export function errorCode(event: AgentTurnEvent): string | undefined {
  if (event.type !== "turn_failed") return undefined;
  return event.payload.error.code;
}

export function deltaText(event: AgentTurnEvent): string {
  return event.type === "message_delta" ? event.payload.delta : "";
}

export const neverAbort = new AbortController().signal;
