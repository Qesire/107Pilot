import { Type, type Static, type TSchema } from "typebox";
import { Value } from "typebox/value";

import {
  CHECKPOINT_PROTOCOL_VERSION,
  DURABLE_TURN_PROTOCOL_VERSION,
  EVENT_PROTOCOL_VERSION,
  TOOL_INVOCATION_PROTOCOL_VERSION,
  TOOL_RESULT_PROTOCOL_VERSION,
  TURN_PROTOCOL_VERSION,
} from "./version.js";

const JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema";
const SCHEMA_BASE = "https://107pilot.local/schemas/agent/v1/";
const V2_SCHEMA_BASE = "https://107pilot.local/schemas/agent/v2/";
const FORBIDDEN_INPUT_PATTERN =
  "^(?:[aA][pP][iI]_[kK][eE][yY]|[aA][uU][tT][hH][oO][rR][iI][zZ][aA][tT][iI][oO][nN]|[bB][aA][sS][eE]_[uU][rR][lL]|[sS][yY][sS][tT][eE][mM]_[pP][rR][oO][mM][pP][tT]|[sS][cC][hH][eE][mM][aA]|[tT][oO][oO][lL][sS])$";
const FORBIDDEN_INPUT_FIELDS = new Set([
  "api_key",
  "authorization",
  "base_url",
  "system_prompt",
  "schema",
  "tools",
]);

const JsonDefinitions = {
  jsonValue: {
    anyOf: [
      { type: "null" },
      { type: "boolean" },
      { type: "number" },
      { type: "string", maxLength: 256_000 },
      {
        type: "array",
        maxItems: 4_096,
        items: { $ref: "#/$defs/jsonValue" },
      },
      {
        type: "object",
        maxProperties: 4_096,
        propertyNames: {
          not: {
            pattern: FORBIDDEN_INPUT_PATTERN,
          },
        },
        additionalProperties: { $ref: "#/$defs/jsonValue" },
      },
    ],
  },
} as const;

type JsonPrimitive = null | boolean | number | string;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

const JsonValueSchema = Type.Unsafe<JsonValue>({ $ref: "#/$defs/jsonValue" });
const JsonObjectSchema = Type.Unsafe<JsonObject>({
  type: "object",
  maxProperties: 4_096,
  propertyNames: {
    not: {
      pattern: FORBIDDEN_INPUT_PATTERN,
    },
  },
  additionalProperties: { $ref: "#/$defs/jsonValue" },
});

const Id = Type.String({
  minLength: 1,
  maxLength: 128,
  pattern: "^[A-Za-z0-9._:-]+$",
});
const Text = Type.String({ maxLength: 256_000 });
const NonEmptyText = Type.String({ minLength: 1, maxLength: 64_000 });
const StringList = Type.Array(Type.String({ maxLength: 4_096 }), { maxItems: 4_096 });
const NullableCount = Type.Union([
  Type.Null(),
  Type.Integer({ minimum: 0, maximum: Number.MAX_SAFE_INTEGER }),
]);

const LimitsSchema = Type.Object(
  {
    timeout_ms: Type.Integer({ minimum: 100, maximum: 300_000 }),
    max_output_tokens: Type.Integer({ minimum: 1, maximum: 32_000 }),
  },
  { additionalProperties: false },
);

const TraceSchema = Type.Object(
  { correlation_id: Id },
  { additionalProperties: false },
);

const CheckpointMessageSchema = Type.Object(
  {
    role: Type.Union([
      Type.Literal("user"),
      Type.Literal("assistant"),
      Type.Literal("tool_result"),
    ]),
    content: Text,
    tool_call_id: Type.Union([Type.Null(), Id]),
    tool_name: Type.Union([Type.Null(), Id]),
    is_error: Type.Union([Type.Null(), Type.Boolean()]),
  },
  { additionalProperties: false },
);

const CompletedToolSchema = Type.Object(
  {
    tool_call_id: Id,
    tool_name: Id,
    arguments: JsonObjectSchema,
    result: JsonValueSchema,
    is_error: Type.Boolean(),
  },
  { additionalProperties: false },
);

const UsageSchema = Type.Object(
  {
    input_tokens: NullableCount,
    output_tokens: NullableCount,
    cache_read_tokens: NullableCount,
    cache_write_tokens: NullableCount,
  },
  { additionalProperties: false },
);

const AgentCheckpointBodySchema = Type.Object(
  {
    schema_version: Type.Literal(CHECKPOINT_PROTOCOL_VERSION),
    turn_id: Id,
    lineage: Type.Array(Id, { maxItems: 256 }),
    model_profile_id: Id,
    prompt_profile_id: Id,
    messages: Type.Array(CheckpointMessageSchema, { maxItems: 4_096 }),
    completed_tools: Type.Array(CompletedToolSchema, { maxItems: 4_096 }),
    usage: UsageSchema,
    digest: Type.String({ pattern: "^[a-f0-9]{64}$" }),
  },
  { additionalProperties: false },
);

export const AgentCheckpointSchema = Type.Object(
  AgentCheckpointBodySchema.properties,
  {
    $schema: JSON_SCHEMA_DRAFT,
    $id: `${SCHEMA_BASE}checkpoint.schema.json`,
    $defs: JsonDefinitions,
    additionalProperties: false,
  },
);

export type AgentCheckpoint = Static<typeof AgentCheckpointSchema>;

const ContextBlockSchema = Type.Object(
  {
    source: Id,
    trust: Type.Union([Type.Literal("trusted"), Type.Literal("untrusted")]),
    content: Text,
  },
  { additionalProperties: false },
);

const InteractiveInputSchema = Type.Object(
  {
    message: NonEmptyText,
    context_blocks: Type.Array(ContextBlockSchema, { maxItems: 64 }),
  },
  { additionalProperties: false },
);

const ExplainFactSchema = Type.Object(
  {
    fact_id: Id,
    statement: NonEmptyText,
    evidence_refs: StringList,
    evidence_object_ids: Type.Array(Id, { maxItems: 4_096 }),
    confidence: Id,
  },
  { additionalProperties: false },
);

const BoundEvidenceSchema = Type.Object(
  {
    object_id: Id,
    evidence_ref: Text,
    logical_path: Text,
    sha256: Type.String({ pattern: "^[a-f0-9]{64}$" }),
    mime_type: Type.String({ minLength: 1, maxLength: 256 }),
    trust: Id,
    snippet: Text,
    truncated: Type.Boolean(),
    redactions: StringList,
  },
  { additionalProperties: false },
);

const CodeContextChunkSchema = Type.Object(
  {
    chunk_id: Id,
    source_ref: Text,
    path: Text,
    start_line: Type.Integer({ minimum: 1 }),
    end_line: Type.Integer({ minimum: 1 }),
    content: Text,
    sha256: Type.String({ pattern: "^[a-f0-9]{64}$" }),
    redactions: StringList,
  },
  { additionalProperties: false },
);

const CodeContextSchema = Type.Object(
  {
    run_id: Id,
    snapshot_id: Id,
    workspace: Text,
    revision: Text,
    dirty: Type.Boolean(),
    worktree_fingerprint: Text,
    chunks: Type.Array(CodeContextChunkSchema, { maxItems: 256 }),
    evidence_snippets: StringList,
    warnings: StringList,
  },
  { additionalProperties: false },
);

const DiagnosisSchema = Type.Object(
  {
    diagnosis_id: Id,
    rule_id: Id,
    severity: Id,
    summary: Text,
    evidence_refs: StringList,
    suggested_patch: JsonObjectSchema,
    retryable: Type.Boolean(),
    confidence: Id,
    category: Type.Union([Type.Null(), Type.String({ maxLength: 256 })]),
    stage: Type.Union([Type.Null(), Type.String({ maxLength: 256 })]),
    fix_guide: JsonObjectSchema,
  },
  { additionalProperties: false },
);

const ExplainInputSchema = Type.Object(
  {
    run_id: Id,
    status: Id,
    deterministic_summary: Text,
    facts: Type.Array(ExplainFactSchema, { maxItems: 4_096 }),
    bound_evidence: Type.Array(BoundEvidenceSchema, { maxItems: 4_096 }),
    code_context: Type.Union([Type.Null(), CodeContextSchema]),
    diagnoses: Type.Array(DiagnosisSchema, { maxItems: 4_096 }),
    required_output: Type.Object(
      {
        summary: Text,
        narrative: Text,
        recommendations: Text,
        warnings: Text,
        citations: Text,
      },
      { additionalProperties: false },
    ),
  },
  { additionalProperties: false },
);

const ContractPatchInputSchema = Type.Object(
  {
    recipe_version_id: Id,
    user_intent: NonEmptyText,
    current_contract: JsonObjectSchema,
    required_output: Type.Object(
      {
        suggested_patch: Text,
        explanation_zh: Text,
      },
      { additionalProperties: false },
    ),
  },
  { additionalProperties: false },
);

const RemediationFactSchema = Type.Object(
  {
    fact_id: Id,
    statement: NonEmptyText,
    evidence_object_ids: Type.Array(Id, { maxItems: 4_096 }),
    confidence: Id,
  },
  { additionalProperties: false },
);

const RemediationInputSchema = Type.Object(
  {
    run_id: Id,
    facts: Type.Array(RemediationFactSchema, { maxItems: 4_096 }),
    policy: Type.Object(
      {
        allowed_action_types: Type.Array(Id, { maxItems: 256 }),
        allowed_contract_patch_fields: Type.Array(Text, { maxItems: 4_096 }),
        arbitrary_shell: Type.Literal(false),
        proposal_is_execution_authority: Type.Literal(false),
      },
      { additionalProperties: false },
    ),
  },
  { additionalProperties: false },
);

function requestVariant<
  const Kind extends TaskKind,
  const PromptProfile extends string,
  const Toolset extends string,
  const Input extends TSchema,
>(kind: Kind, promptProfile: PromptProfile, toolset: Toolset, input: Input) {
  return Type.Object(
    {
      schema_version: Type.Literal(TURN_PROTOCOL_VERSION),
      turn_id: Id,
      task_kind: Type.Literal(kind),
      model_profile_id: Id,
      prompt_profile_id: Type.Literal(promptProfile),
      toolset_id: Type.Literal(toolset),
      input,
      checkpoint: Type.Union([Type.Null(), AgentCheckpointBodySchema]),
      limits: LimitsSchema,
      trace: TraceSchema,
    },
    { additionalProperties: false },
  );
}

export type TaskKind =
  | "interactive"
  | "explain"
  | "contract_patch"
  | "remediation_plan";
export type ConstrainedTaskKind = Exclude<TaskKind, "interactive">;

const RequestVariants = [
  requestVariant("interactive", "hpc-assistant-v1", "a0-none", InteractiveInputSchema),
  requestVariant(
    "explain",
    "agent-explain-v1",
    "emit-explanation-v1",
    ExplainInputSchema,
  ),
  requestVariant(
    "contract_patch",
    "contract-patch-v1",
    "emit-contract-patch-v1",
    ContractPatchInputSchema,
  ),
  requestVariant(
    "remediation_plan",
    "remediation-plan-v1",
    "emit-remediation-plan-v1",
    RemediationInputSchema,
  ),
] as const;

export const AgentTurnRequestSchema = Type.Union([...RequestVariants], {
  $schema: JSON_SCHEMA_DRAFT,
  $id: `${SCHEMA_BASE}turn-request.schema.json`,
  $defs: JsonDefinitions,
});

export type AgentTurnRequest = Static<typeof AgentTurnRequestSchema>;

const DurableTurnInputSchema = Type.Object(
  {
    message: NonEmptyText,
    context_refs: Type.Array(Type.String({ minLength: 1, maxLength: 4_096 }), {
      maxItems: 256,
    }),
  },
  { additionalProperties: false },
);

export const DurableAgentTurnRequestSchema = Type.Object(
  {
    schema_version: Type.Literal(DURABLE_TURN_PROTOCOL_VERSION),
    session_id: Id,
    turn_id: Id,
    owner: Id,
    state_version: Type.Integer({ minimum: 0, maximum: Number.MAX_SAFE_INTEGER }),
    task_kind: Type.Union([
      Type.Literal("interactive_readonly"),
      Type.Literal("experiment_builder"),
      Type.Literal("run_diagnosis_repair"),
      Type.Literal("market_application"),
      Type.Literal("template_publication"),
    ]),
    model_profile_id: Id,
    prompt_profile_id: Type.Union([
      Type.Literal("hpc-readonly-v1"),
      Type.Literal("platform_coach"),
      Type.Literal("experiment_builder"),
      Type.Literal("run_diagnosis_repair"),
      Type.Literal("market_application"),
      Type.Literal("template_publication"),
    ]),
    toolset_id: Type.Union([
      Type.Literal("a1-readonly"),
      Type.Literal("a2-project"),
    ]),
    input: DurableTurnInputSchema,
    capability_token: Type.String({ minLength: 1, maxLength: 8_192 }),
    checkpoint: Type.Union([Type.Null(), AgentCheckpointBodySchema]),
    limits: LimitsSchema,
    trace: TraceSchema,
  },
  {
    $schema: JSON_SCHEMA_DRAFT,
    $id: `${V2_SCHEMA_BASE}turn-request.schema.json`,
    $defs: JsonDefinitions,
    additionalProperties: false,
  },
);

export type DurableAgentTurnRequest = Static<typeof DurableAgentTurnRequestSchema>;

export type ExecutableAgentTurnRequest =
  | AgentTurnRequest
  | DurableAgentTurnRequest;
export type ExecutableTaskKind =
  | TaskKind
  | "interactive_readonly"
  | "experiment_builder"
  | "run_diagnosis_repair"
  | "market_application"
  | "template_publication";

export const A1_READ_TOOL_NAMES = [
  "platform_get_snapshot",
  "platform_observation_get",
  "account_observation_get",
  "run_get",
  "run_log_read",
  "evidence_read",
  "run_resources_get",
] as const;

export const A2_PROJECT_TOOL_NAMES = [
  "project_get",
  "workspace_list",
  "workspace_read",
  "workspace_patch",
  "workspace_diff",
  "sandbox_exec",
  "validation_schedule",
] as const;

export type ReadToolName =
  | (typeof A1_READ_TOOL_NAMES)[number]
  | (typeof A2_PROJECT_TOOL_NAMES)[number];

const ReadToolNameSchema = Type.Union(
  [...A1_READ_TOOL_NAMES, ...A2_PROJECT_TOOL_NAMES].map((name) => Type.Literal(name)),
);

export const ToolInvocationSchema = Type.Object(
  {
    schema_version: Type.Literal(TOOL_INVOCATION_PROTOCOL_VERSION),
    invocation_id: Id,
    idempotency_key: Id,
    owner: Id,
    session_id: Id,
    turn_id: Id,
    state_version: Type.Integer({ minimum: 0, maximum: Number.MAX_SAFE_INTEGER }),
    profile_id: Type.Union([
      Type.Literal("hpc-readonly-v1"),
      Type.Literal("platform_coach"),
      Type.Literal("experiment_builder"),
      Type.Literal("run_diagnosis_repair"),
      Type.Literal("market_application"),
      Type.Literal("template_publication"),
    ]),
    tool_name: ReadToolNameSchema,
    arguments: JsonObjectSchema,
    deadline: Type.String({
      minLength: 20,
      maxLength: 32,
      pattern: "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,6})?Z$",
    }),
  },
  {
    $schema: JSON_SCHEMA_DRAFT,
    $id: `${V2_SCHEMA_BASE}tool-invocation.schema.json`,
    $defs: JsonDefinitions,
    additionalProperties: false,
  },
);

export type ToolInvocation = Static<typeof ToolInvocationSchema>;

const ToolResultBase = {
  schema_version: Type.Literal(TOOL_RESULT_PROTOCOL_VERSION),
  invocation_id: Id,
  evidence_refs: Type.Array(Type.String({ minLength: 1, maxLength: 4_096 }), {
    maxItems: 256,
  }),
  bytes_returned: Type.Integer({ minimum: 0, maximum: 1_048_576 }),
};

const ToolErrorSchema = Type.Object(
  {
    code: Id,
    message: Type.String({ minLength: 1, maxLength: 4_096 }),
    retryable: Type.Boolean(),
  },
  { additionalProperties: false },
);

export const ToolResultSchema = Type.Union(
  [
    Type.Object(
      { ...ToolResultBase, result: JsonObjectSchema, error: Type.Null() },
      { additionalProperties: false },
    ),
    Type.Object(
      { ...ToolResultBase, result: Type.Null(), error: ToolErrorSchema },
      { additionalProperties: false },
    ),
  ],
  {
    $schema: JSON_SCHEMA_DRAFT,
    $id: `${V2_SCHEMA_BASE}tool-result.schema.json`,
    $defs: JsonDefinitions,
  },
);

export type ToolResult = Static<typeof ToolResultSchema>;

const ErrorCodeSchema = Type.Union([
  Type.Literal("provider_auth"),
  Type.Literal("provider_rate_limited"),
  Type.Literal("provider_timeout"),
  Type.Literal("provider_unavailable"),
  Type.Literal("provider_invalid_response"),
  Type.Literal("output_contract_violation"),
  Type.Literal("aborted"),
  Type.Literal("internal_error"),
]);

const TurnErrorSchema = Type.Object(
  {
    code: ErrorCodeSchema,
    retryable: Type.Boolean(),
    message: Type.String({ minLength: 1, maxLength: 4_096 }),
    provider_status: Type.Optional(
      Type.Integer({ minimum: 100, maximum: 599 }),
    ),
  },
  { additionalProperties: false },
);

const ToolBaseProperties = {
  tool_call_id: Id,
  tool_name: Id,
};

function eventVariant<const EventType extends TurnEventType, const Payload extends TSchema>(
  type: EventType,
  payload: Payload,
) {
  return Type.Object(
    {
      schema_version: Type.Literal(EVENT_PROTOCOL_VERSION),
      turn_id: Id,
      sequence: Type.Integer({ minimum: 1 }),
      timestamp: Type.String({ minLength: 1, maxLength: 64 }),
      type: Type.Literal(type),
      payload,
    },
    { additionalProperties: false },
  );
}

export type TurnEventType =
  | "turn_started"
  | "message_delta"
  | "tool_call_requested"
  | "tool_call_started"
  | "tool_call_progress"
  | "tool_call_completed"
  | "checkpoint"
  | "turn_completed"
  | "turn_failed";

const EventVariants = [
  eventVariant(
    "turn_started",
    Type.Object(
      {
        model_profile_id: Id,
        task_kind: Type.Union([
          Type.Literal("interactive"),
          Type.Literal("explain"),
          Type.Literal("contract_patch"),
          Type.Literal("remediation_plan"),
        ]),
      },
      { additionalProperties: false },
    ),
  ),
  eventVariant(
    "message_delta",
    Type.Object({ delta: Text }, { additionalProperties: false }),
  ),
  eventVariant(
    "tool_call_requested",
    Type.Object(
      { ...ToolBaseProperties, arguments: JsonObjectSchema },
      { additionalProperties: false },
    ),
  ),
  eventVariant(
    "tool_call_started",
    Type.Object(ToolBaseProperties, { additionalProperties: false }),
  ),
  eventVariant(
    "tool_call_progress",
    Type.Object(
      { ...ToolBaseProperties, progress: Text },
      { additionalProperties: false },
    ),
  ),
  eventVariant(
    "tool_call_completed",
    Type.Object(
      { ...ToolBaseProperties, result: JsonValueSchema, is_error: Type.Boolean() },
      { additionalProperties: false },
    ),
  ),
  eventVariant(
    "checkpoint",
    Type.Object(
      { checkpoint: AgentCheckpointBodySchema },
      { additionalProperties: false },
    ),
  ),
  eventVariant(
    "turn_completed",
    Type.Object(
      {
        result: JsonValueSchema,
        provider: Id,
        model: Type.String({ minLength: 1, maxLength: 512 }),
        model_profile_id: Id,
        usage: UsageSchema,
        provider_calls: Type.Integer({ minimum: 1, maximum: 100 }),
        checkpoint_digest: Type.String({ pattern: "^[a-f0-9]{64}$" }),
        duration_ms: Type.Integer({ minimum: 0, maximum: 3_600_000 }),
        checkpoint: Type.Optional(AgentCheckpointBodySchema),
      },
      { additionalProperties: false },
    ),
  ),
  eventVariant(
    "turn_failed",
    Type.Object(
      {
        error: TurnErrorSchema,
        checkpoint: Type.Optional(AgentCheckpointBodySchema),
      },
      { additionalProperties: false },
    ),
  ),
] as const;

export const AgentTurnEventSchema = Type.Union([...EventVariants], {
  $schema: JSON_SCHEMA_DRAFT,
  $id: `${SCHEMA_BASE}turn-event.schema.json`,
  $defs: JsonDefinitions,
});

export type AgentTurnEvent = Static<typeof AgentTurnEventSchema>;

const TASK_PAIRINGS: Record<TaskKind, readonly [string, string]> = {
  interactive: ["hpc-assistant-v1", "a0-none"],
  explain: ["agent-explain-v1", "emit-explanation-v1"],
  contract_patch: ["contract-patch-v1", "emit-contract-patch-v1"],
  remediation_plan: ["remediation-plan-v1", "emit-remediation-plan-v1"],
};

export function parseTurnRequest(value: unknown): AgentTurnRequest {
  rejectInputInjection(value);
  rejectInvalidTaskPairing(value);
  if (!Value.Check(AgentTurnRequestSchema, value)) {
    throw new TypeError(validationMessage("turn request", AgentTurnRequestSchema, value));
  }
  return value;
}

export function parseDurableTurnRequest(value: unknown): DurableAgentTurnRequest {
  rejectInputInjection(value);
  if (!Value.Check(DurableAgentTurnRequestSchema, value)) {
    throw new TypeError(
      validationMessage("durable turn request", DurableAgentTurnRequestSchema, value),
    );
  }
  const readonlyPair = value.task_kind === "interactive_readonly"
    && ["hpc-readonly-v1", "platform_coach"].includes(value.prompt_profile_id)
    && value.toolset_id === "a1-readonly";
  const builderPair = [
    "experiment_builder",
    "run_diagnosis_repair",
    "market_application",
    "template_publication",
  ]
    .includes(value.task_kind)
    && value.prompt_profile_id === value.task_kind
    && value.toolset_id === "a2-project";
  if (!readonlyPair && !builderPair) {
    throw new TypeError("durable turn request profile/toolset pairing is invalid");
  }
  return value;
}

export function parseExecutableTurnRequest(
  value: unknown,
): ExecutableAgentTurnRequest {
  if (
    isObject(value) &&
    value.schema_version === DURABLE_TURN_PROTOCOL_VERSION
  ) {
    return parseDurableTurnRequest(value);
  }
  return parseTurnRequest(value);
}

export function parseToolInvocation(value: unknown): ToolInvocation {
  if (isObject(value) && "arguments" in value) {
    visitInput(value.arguments, "arguments", new Set<object>());
  }
  if (!Value.Check(ToolInvocationSchema, value)) {
    throw new TypeError(validationMessage("tool invocation", ToolInvocationSchema, value));
  }
  const allowed = [
    "experiment_builder",
    "run_diagnosis_repair",
    "market_application",
    "template_publication",
  ]
    .includes(value.profile_id)
    ? A2_PROJECT_TOOL_NAMES
    : A1_READ_TOOL_NAMES;
  if (!(allowed as readonly string[]).includes(value.tool_name)) {
    throw new TypeError("tool invocation profile/tool pairing is invalid");
  }
  return value;
}

export function parseToolResult(value: unknown): ToolResult {
  if (!Value.Check(ToolResultSchema, value)) {
    throw new TypeError(validationMessage("tool result", ToolResultSchema, value));
  }
  return value;
}

export function parseCheckpoint(value: unknown): AgentCheckpoint {
  if (!Value.Check(AgentCheckpointSchema, value)) {
    throw new TypeError(validationMessage("checkpoint", AgentCheckpointSchema, value));
  }
  return value;
}

export function isTerminalEvent(event: Pick<AgentTurnEvent, "type">): boolean {
  return event.type === "turn_completed" || event.type === "turn_failed";
}

export function assertTerminalInvariant(
  events: readonly AgentTurnEvent[],
): AgentTurnEvent {
  for (const event of events) {
    if (!Value.Check(AgentTurnEventSchema, event)) {
      throw new TypeError(validationMessage("turn event", AgentTurnEventSchema, event));
    }
  }
  const terminalEvents = events.filter(isTerminalEvent);
  if (terminalEvents.length !== 1) {
    throw new TypeError(
      `event stream must contain exactly one terminal event; got ${terminalEvents.length}`,
    );
  }
  const terminal = terminalEvents[0] as AgentTurnEvent;
  if (events.at(-1) !== terminal) {
    throw new TypeError("terminal event must be last");
  }
  const turnId = events[0]?.turn_id;
  for (const [index, event] of events.entries()) {
    if (event.sequence !== index + 1) {
      throw new TypeError("event sequence must be contiguous and start at 1");
    }
    if (event.turn_id !== turnId) {
      throw new TypeError("event stream must use one turn_id");
    }
  }
  return terminal;
}

function rejectInvalidTaskPairing(value: unknown): void {
  if (!isObject(value) || typeof value.task_kind !== "string") return;
  if (!(value.task_kind in TASK_PAIRINGS)) return;
  const kind = value.task_kind as TaskKind;
  const [profile, toolset] = TASK_PAIRINGS[kind];
  if (value.prompt_profile_id !== profile || value.toolset_id !== toolset) {
    throw new TypeError(
      `invalid task/profile/toolset pairing for ${kind}: expected ${profile}/${toolset}`,
    );
  }
}

function rejectInputInjection(value: unknown): void {
  if (!isObject(value) || !("input" in value)) return;
  visitInput(value.input, "input", new Set<object>());
}

function visitInput(value: unknown, path: string, seen: Set<object>): void {
  if (value === null || typeof value !== "object") return;
  if (seen.has(value)) {
    throw new TypeError(`invalid turn request: cyclic value at ${path}`);
  }
  seen.add(value);
  if (Array.isArray(value)) {
    value.forEach((item, index) => visitInput(item, `${path}[${index}]`, seen));
    seen.delete(value);
    return;
  }
  for (const [key, nested] of Object.entries(value)) {
    if (FORBIDDEN_INPUT_FIELDS.has(key.toLowerCase())) {
      throw new TypeError(`forbidden input field ${path}.${key}`);
    }
    visitInput(nested, `${path}.${key}`, seen);
  }
  seen.delete(value);
}

function validationMessage(label: string, schema: TSchema, value: unknown): string {
  const errors = Value.Errors(schema, value);
  const unknown = errors.find((error) => error.keyword === "additionalProperties");
  if (unknown?.keyword === "additionalProperties") {
    const fields = unknown.params.additionalProperties.join(", ");
    return `invalid ${label}: unknown field ${fields} at ${unknown.instancePath || "/"}`;
  }
  const first = errors[0];
  return `invalid ${label}: ${first?.message ?? "schema mismatch"}${
    first?.instancePath ? ` at ${first.instancePath}` : ""
  }`;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
