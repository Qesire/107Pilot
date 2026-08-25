# Pi Turn Contract Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make embedded Pi Turns context-aware, bounded, readable, and terminal: no guessed Workspace tools, no empty `{}` tool failures, no standalone whitespace messages, no mislabeled task kind, and no unbounded inner tool loop ending only at timeout.

**Architecture:** Keep `@earendil-works/pi-agent-core` as a short-lived in-process Agent created per Turn. Build the model-visible tool catalog from durable request bindings, normalize gateway failures into a safe structured error envelope, and enforce a per-task Pi step budget independently from provider retry attempts. Durable events retain their real task kind; the web view projects coalesced human messages from the raw event log without deleting audit events.

**Tech Stack:** TypeScript, `@earendil-works/pi-agent-core` 0.84.1, TypeBox, Vitest, Python worker/protocol mirror, React 18, TanStack Query.

## Global Constraints

- Do not introduce a persistent OS-level Pi process. The intended unit is one embedded `Agent` instance per durable Turn attempt.
- Provider retries remain capped at three provider calls. Pi step budgets are separate: four steps for `interactive_readonly`, twelve steps for Project profiles.
- Generic read-only Turns must never expose `workspace_list`, `workspace_search`, or `workspace_read`. Those operations belong to Project tools with durable `project_id` and `workspace_id` bindings.
- Run/Evidence tools are visible only when `context_refs` binds the corresponding resource. Platform tools remain visible for a platform-only Session.
- Tool error payloads may include only safe `code`, `message`, and `retryable`; never forward stack traces, credentials, request headers, provider bodies, or arbitrary internal exception text.
- Persist raw durable events for audit. Coalescing is a presentation projection, not mutation of history.
- Each task ends with an isolated commit.

---

### Task 1: Build a context-aware read-only tool catalog

**Files:**
- Modify: `services/pilot-agentd/src/read-tools.ts`
- Modify: `services/pilot-agentd/src/tasks.ts`
- Modify: `services/pilot-agentd/src/protocol.ts`
- Modify: `services/pilot-agentd/tests/readonly-turn.integration.test.ts`
- Modify: `services/pilot-agentd/tests/tasks.test.ts`
- Modify: `services/pilot-agentd/tests/protocol.test.ts`
- Modify: `src/pilot107/worker/agent_turn_worker.py`
- Modify: `src/pilot107/agent/protocol.py`
- Modify: `src/pilot107/agent/capabilities.py`
- Modify: `tests/test_agent_turn_worker.py`
- Modify: `tests/agent/test_protocol.py`
- Modify: `tests/agent/test_capabilities.py`

**Interfaces:**

```ts
export function visibleReadToolNames(
  request: DurableAgentTurnRequest,
): readonly A1ToolName[];
```

Binding rules:

```text
always: platform_get_snapshot, platform_observation_get, account_observation_get
run:<id>: run_get, run_log_read, run_resources_get
evidence:<run_id>:<object_id>: evidence_read
never in A1: workspace_list, workspace_search, workspace_read
```

- [ ] **Step 1: Write failing TypeScript catalog tests**

Add table-driven cases for an empty-source platform Session, a Run-bound Session, and an Evidence-bound Session.

```ts
expect(visibleReadToolNames(platformRequest)).toEqual([
  "platform_get_snapshot",
  "platform_observation_get",
  "account_observation_get",
]);
expect(visibleReadToolNames(runRequest)).toContain("run_get");
expect(visibleReadToolNames(platformRequest)).not.toContain("workspace_list");
```

In the readonly integration test, inspect the tools passed to the fake model and assert that `workspace_list` is absent for the request shape that reproduced Session `session-c60e21fb-c580-4c98-ad58-f9e3356e1b8e`.

- [ ] **Step 2: Write the matching Python worker failure test**

Assert that `_build_durable_turn_request` does not claim Workspace A1 tools for a Session with `source={}` and does include the Run tools when `_context_refs` produces a `run:` binding. Add capability/protocol tests proving Workspace names are no longer valid A1 claims but remain valid A2 Project claims.

- [ ] **Step 3: Run the focused tests and observe the broad catalog**

Run:

```bash
npm --prefix services/pilot-agentd test -- --run \
  tests/tasks.test.ts tests/readonly-turn.integration.test.ts tests/protocol.test.ts
python -m pytest \
  tests/test_agent_turn_worker.py \
  tests/agent/test_protocol.py \
  tests/agent/test_capabilities.py -q
```

Expected: the empty-source request still exposes Workspace and Run/Evidence tools.

- [ ] **Step 4: Implement catalog filtering**

Remove Workspace names from both TS/Python A1 protocol constants and the read-only capability allowlist; they remain in A2. Compute bound categories from `request.context_refs` and map only the visible names in `createReadOnlyTools`. Build the Python capability's `tools` set from the same categories so Run/Evidence authority is not granted to an unbound Session.

```ts
export function visibleReadToolNames(request: DurableAgentTurnRequest): readonly A1ToolName[] {
  const refs = new Set(request.context_refs);
  const names: A1ToolName[] = [...PLATFORM_READ_TOOL_NAMES];
  if ([...refs].some((ref) => ref.startsWith("run:"))) names.push(...RUN_READ_TOOL_NAMES);
  if ([...refs].some((ref) => ref.startsWith("evidence:"))) names.push("evidence_read");
  return names;
}
```

Keep the Python capability declaration synchronized so the durable request cannot claim tools that the Session has no source binding for. Project Workspace operations continue through `A2_PROJECT_TOOL_NAMES` and `createProjectTools`.

- [ ] **Step 5: Run TS, Python, and protocol tests**

```bash
npm --prefix services/pilot-agentd test -- --run \
  tests/tasks.test.ts tests/readonly-turn.integration.test.ts tests/protocol.test.ts
python -m pytest \
  tests/test_agent_turn_worker.py \
  tests/agent/test_protocol.py \
  tests/agent/test_capabilities.py -q
```

Expected: all pass and the platform-only fake model sees no unbound resource tools.

- [ ] **Step 6: Commit context-aware tools**

```bash
git add \
  services/pilot-agentd/src/read-tools.ts \
  services/pilot-agentd/src/tasks.ts \
  services/pilot-agentd/src/protocol.ts \
  services/pilot-agentd/tests/readonly-turn.integration.test.ts \
  services/pilot-agentd/tests/tasks.test.ts \
  services/pilot-agentd/tests/protocol.test.ts \
  src/pilot107/worker/agent_turn_worker.py \
  src/pilot107/agent/protocol.py \
  src/pilot107/agent/capabilities.py \
  tests/test_agent_turn_worker.py \
  tests/agent/test_protocol.py \
  tests/agent/test_capabilities.py
git commit -m "fix: bind Pi read tools to session context"
```

---

### Task 2: Preserve safe structured tool failures end-to-end

**Files:**
- Modify: `services/pilot-agentd/src/read-tools.ts`
- Modify: `services/pilot-agentd/src/project-tools.ts`
- Modify: `services/pilot-agentd/src/events.ts`
- Modify: `services/pilot-agentd/tests/read-tools.test.ts`
- Modify: `services/pilot-agentd/tests/project-tools.test.ts`
- Modify: `services/pilot-agentd/tests/events.test.ts`

**Interfaces:**

```ts
interface PublicToolError {
  readonly code: string;
  readonly message: string;
  readonly retryable: boolean;
}

export function toolFailureResult(error: ToolResult["error"]): {
  content: Array<{ type: "text"; text: string }>;
  details: { error: PublicToolError };
};
```

- [ ] **Step 1: Write failing tool-wrapper tests**

Have the fake gateway return:

```ts
{
  result: null,
  error: { code: "workspace_not_bound", message: "No Workspace is bound.", retryable: false },
  evidence_refs: [],
  bytes_returned: 0,
}
```

Assert that the model-visible result contains the readable message and event details equal:

```ts
{
  error: {
    code: "workspace_not_bound",
    message: "No Workspace is bound.",
    retryable: false,
  },
}
```

Add a sanitization test proving extra fields such as `stack`, `authorization`, and `body` are discarded.

- [ ] **Step 2: Write the `{}` event regression test**

In `events.test.ts`, pass a Pi tool result whose `details` is `{}` and whose text content is `"No Workspace is bound."`. Assert `mapPiEvent` emits the text content rather than `{}`.

- [ ] **Step 3: Run the focused tests**

```bash
npm --prefix services/pilot-agentd test -- --run \
  tests/read-tools.test.ts tests/project-tools.test.ts tests/events.test.ts
```

Expected: failures show generic `The read tool request failed.` and `result: {}`.

- [ ] **Step 4: Implement one shared safe normalization path**

Place the pure normalization helper in `read-tools.ts` and reuse it from `project-tools.ts`. Return a Pi tool result for known gateway failures instead of throwing a generic exception. Continue throwing only for transport/programming failures that do not carry a `ToolResult.error`.

Update `events.ts::resultValue()` so an empty object is not considered meaningful:

```ts
function hasObjectFields(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && Object.keys(value).length > 0;
}

if (hasObjectFields(result.details)) return structuredClone(result.details);
return textContent(result.content);
```

- [ ] **Step 5: Run tool/event tests**

```bash
npm --prefix services/pilot-agentd test -- --run \
  tests/read-tools.test.ts \
  tests/project-tools.test.ts \
  tests/events.test.ts \
  tests/readonly-turn.integration.test.ts
```

Expected: all pass and the captured durable tool completion has a non-empty safe error.

- [ ] **Step 6: Commit structured failures**

```bash
git add \
  services/pilot-agentd/src/read-tools.ts \
  services/pilot-agentd/src/project-tools.ts \
  services/pilot-agentd/src/events.ts \
  services/pilot-agentd/tests/read-tools.test.ts \
  services/pilot-agentd/tests/project-tools.test.ts \
  services/pilot-agentd/tests/events.test.ts
git commit -m "fix: preserve safe Pi tool failures"
```

---

### Task 3: Enforce independent Pi step budgets and terminal text

**Files:**
- Modify: `services/pilot-agentd/src/turn-executor.ts`
- Modify: `services/pilot-agentd/src/errors.ts`
- Modify: `services/pilot-agentd/tests/turn-executor.test.ts`
- Modify: `services/pilot-agentd/tests/readonly-turn.integration.test.ts`

**Interfaces:**

```ts
const PI_STEP_LIMITS: Readonly<Record<ExecutableTaskKind, number>> = {
  interactive_readonly: 4,
  experiment_builder: 12,
  run_diagnosis_repair: 12,
  market_application: 12,
  template_publication: 12,
  // constrained/no-tool kinds terminate after their existing one-shot contract
};

interface AttemptBudget {
  providerCalls: number;
  repairUsed: boolean;
  piSteps: number;
  stepLimitExceeded: boolean;
}
```

Public terminal error:

```json
{
  "code": "tool_step_budget_exhausted",
  "retryable": false,
  "message": "The Turn reached its bounded tool-step limit before producing a final answer."
}
```

- [ ] **Step 1: Write a failing four-step read-only loop test**

Configure the fake model to request one tool forever. Assert exactly four provider turns/tool steps occur, then one `checkpoint` and one `turn_failed` with `tool_step_budget_exhausted`; assert the test completes without advancing the clock to the total timeout.

```ts
expect(provider.calls).toHaveLength(4);
expect(events.at(-1)).toMatchObject({
  event_type: "turn_failed",
  payload: { error: { code: "tool_step_budget_exhausted", retryable: false } },
});
```

- [ ] **Step 2: Write Project and whitespace terminal tests**

Add one Project test that permits twelve tool steps and stops at `validation_schedule` before the limit. Add one read-only test where the model calls a tool and ends with only `"\n\n"`; assert `empty_provider_response`, not successful completion.

- [ ] **Step 3: Run the executor tests**

```bash
npm --prefix services/pilot-agentd test -- --run \
  tests/turn-executor.test.ts tests/readonly-turn.integration.test.ts
```

Expected: the loop continues until timeout and the whitespace-after-tool case incorrectly succeeds.

- [ ] **Step 4: Implement bounded `shouldStopAfterTurn` behavior**

Increment `piSteps` on each Pi `turn_start`. Stop the inner loop when the task is naturally terminal, a terminating tool returns, or `piSteps >= taskStepLimit(request.task_kind)`. After `agent.prompt`, return `toolStepBudgetError()` if the budget stopped a tool-producing turn without final assistant text.

```ts
shouldStopAfterTurn: ({ toolResults }) => {
  if (!isLoopingTask(options.request.task_kind)) return true;
  if (toolResults.length === 0) return true;
  if (options.budget.piSteps >= taskStepLimit(options.request.task_kind)) {
    options.budget.stepLimitExceeded = true;
    return true;
  }
  return false;
},
```

Change the unconstrained output contract so `result.trim().length === 0` is always an error unless a terminating Project tool already produced the durable AgentTask handoff.

- [ ] **Step 5: Prove retry and step budgets do not interfere**

Run:

```bash
npm --prefix services/pilot-agentd test -- --run \
  tests/turn-executor.test.ts \
  tests/readonly-turn.integration.test.ts \
  tests/server.test.ts
```

Expected: existing retry tests still cap provider retry attempts at three; the inner tool-loop tests use 4/12.

- [ ] **Step 6: Commit bounded Turns**

```bash
git add \
  services/pilot-agentd/src/turn-executor.ts \
  services/pilot-agentd/src/errors.ts \
  services/pilot-agentd/tests/turn-executor.test.ts \
  services/pilot-agentd/tests/readonly-turn.integration.test.ts
git commit -m "fix: bound Pi tool loops and terminal output"
```

---

### Task 4: Preserve the real durable task kind

**Files:**
- Modify: `services/pilot-agentd/src/turn-executor.ts`
- Modify: `services/pilot-agentd/src/protocol.ts`
- Modify: `services/pilot-agentd/tests/turn-executor.test.ts`
- Modify: `services/pilot-agentd/tests/protocol.test.ts`
- Modify: `src/pilot107/agent/protocol.py`
- Modify: `tests/agent/test_protocol.py`

**Interfaces:**

`turn_started.payload.task_kind` must use the `ExecutableTaskKind` union without remapping Project/read-only kinds to `interactive`.

- [ ] **Step 1: Write failing event and cross-language tests**

```ts
expect(started.payload.task_kind).toBe("interactive_readonly");
```

Add table cases for `experiment_builder` and `run_diagnosis_repair`. Update the golden protocol fixture only after both Python and TypeScript schemas accept the real values.

- [ ] **Step 2: Run protocol tests**

```bash
npm --prefix services/pilot-agentd test -- --run \
  tests/turn-executor.test.ts tests/protocol.test.ts
python -m pytest \
  tests/agent/test_protocol.py -q
```

Expected: the executor test receives `interactive` instead of `interactive_readonly`.

- [ ] **Step 3: Remove the lossy task-kind mapping**

Emit the request value directly:

```ts
await sink.emit("turn_started", {
  model_profile_id: request.model_profile_id,
  task_kind: request.task_kind,
});
```

If the event payload schema currently narrows task kind, expand the TS and Python mirrors together and regenerate/update the checked-in golden payload through the repository's existing protocol test helper.

- [ ] **Step 4: Run protocol and event suites**

```bash
npm --prefix services/pilot-agentd test -- --run \
  tests/turn-executor.test.ts tests/protocol.test.ts tests/events.test.ts
python -m pytest \
  tests/agent/test_protocol.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit task-kind fidelity**

```bash
git add \
  services/pilot-agentd/src/turn-executor.ts \
  services/pilot-agentd/src/protocol.ts \
  services/pilot-agentd/tests/turn-executor.test.ts \
  services/pilot-agentd/tests/protocol.test.ts \
  src/pilot107/agent/protocol.py \
  tests/agent/test_protocol.py
git commit -m "fix: preserve durable Pi task kinds"
```

---

### Task 5: Project raw events into readable conversation rows

**Files:**
- Modify: `apps/web/src/AgentSessionPanel.tsx`
- Modify: `apps/web/src/AgentSessionPanel.test.tsx`
- Modify: `apps/web/src/styles.css`

**Interfaces:**

```ts
export interface AgentEventGroup {
  readonly key: string;
  readonly kind: "assistant" | "tool" | "lifecycle" | "failure";
  readonly events: readonly AgentTurnEvent[];
  readonly text: string | null;
}

export function groupAgentEvents(events: AgentTurnEvent[]): AgentEventGroup[];
```

- [ ] **Step 1: Write failing projection tests**

Cover these exact sequences:

1. Adjacent deltas `"集群"`, `"共有"`, `" 6 CPU"` become one assistant group with `"集群共有 6 CPU"`.
2. `"\n\n"` produces no assistant group.
3. tool requested/started/completed with the same `tool_call_id` becomes one expandable tool group.
4. a structured tool error displays `error.message`.
5. `turn_started` with `interactive_readonly` labels the Turn `平台只读 Turn`.

```ts
expect(groupAgentEvents(events).filter((group) => group.kind === "assistant"))
  .toHaveLength(1);
expect(groupAgentEvents(events)[0]?.text).toBe("集群共有 6 CPU");
```

- [ ] **Step 2: Run the component unit test**

```bash
npm test -- apps/web/src/AgentSessionPanel.test.tsx
```

Expected: `groupAgentEvents` is missing and current rendering would create one card per delta.

- [ ] **Step 3: Implement the pure grouping projection**

Keep `mergeAgentEvents` for replay/deduplication. Add `groupAgentEvents` and render its result. Whitespace-only deltas stay in `events` but are omitted from visual groups. Concatenate adjacent message deltas for the same Turn without trimming meaningful spaces; use `.trim()` only to decide whether the final group is empty.

Group tool lifecycle events by `(turn_id, tool_call_id)` and retain a `<details>` view of all raw payloads so audit data remains inspectable.

- [ ] **Step 4: Update labels and boundary copy**

Change the generic note so it no longer claims unbound Workspace access. Label actual task kinds from the `turn_started` payload, including:

```ts
{
  interactive_readonly: "平台只读 Turn",
  experiment_builder: "实验构建 Turn",
  run_diagnosis_repair: "诊断修复 Turn",
}
```

- [ ] **Step 5: Run web tests and typecheck**

```bash
npm test -- \
  apps/web/src/AgentSessionPanel.test.tsx apps/web/src/AgentPage.test.ts
npm run typecheck
```

Expected: all pass.

- [ ] **Step 6: Commit the readable event projection**

```bash
git add \
  apps/web/src/AgentSessionPanel.tsx \
  apps/web/src/AgentSessionPanel.test.tsx \
  apps/web/src/styles.css
git commit -m "fix: render Pi events as readable turns"
```

---

## Final Verification

- [ ] Run the complete agentd suite and build:

```bash
npm --prefix services/pilot-agentd test -- --run
npm --prefix services/pilot-agentd run build
```

- [ ] Run Python protocol/worker/gateway tests:

```bash
python -m pytest \
  tests/test_agent_turn_worker.py \
  tests/agent/test_protocol.py \
  tests/agent/test_tool_gateway.py -q
```

- [ ] Run web tests and production build:

```bash
npm test
npm run build
```

- [ ] Reproduce the original platform-only prompt against the deployed reasoner profile. Acceptance requires: no Workspace call, a non-empty final answer, no standalone whitespace reply cards, the real task kind in `turn_started`, and either completion within four steps or `tool_step_budget_exhausted` with a checkpoint.

- [ ] Inspect the durable raw event stream and the UI projection. Confirm raw events remain complete while the rendered view is coalesced.
