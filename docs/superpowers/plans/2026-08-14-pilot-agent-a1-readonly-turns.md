# Pilot Agent A1 Read-Only Turns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build durable, owner-scoped, read-only Agent sessions and Turns that can call a minimal set of typed 107Pilot read tools through `pilot-agentd`, survive API/Worker/Agentd restarts, and replay persisted events to a reconnecting client.

**Architecture:** Python remains the identity, persistence, authorization, budget, and tool-execution authority. A short-lived Worker lease invokes `pilot-agentd`; Agentd receives a per-Turn capability token and can call only the private Python Tool Gateway with the seven A1 read tools. Every Agentd event is persisted before client publication, and interrupted read-only Turns are recoverable because A1 has no mutating tools.

**Tech Stack:** Python 3.12, SQLite and PostgreSQL repository parity, stdlib HTTP/NDJSON, HMAC-SHA256 capability tokens, TypeScript 5.9, Node 22.19.0, `@earendil-works/pi-agent-core` 0.84.1, TypeBox, Vitest, Docker Compose, pytest.

## Global Constraints

- Keep Node exactly `22.19.0` and Pi packages exactly `0.84.1`; version changes require a separate compatibility task.
- Keep the existing A0 v1 request/event/checkpoint contracts valid for explain, Contract patch, remediation, and faux/campus tests.
- `pilot-agentd` receives no Slurm token, SSH key, MFA material, workspace mount, `/public` mount, database DSN, or provider secret beyond its own model configuration.
- The Tool Gateway revalidates owner, session, Turn, state version, profile, tool allowlist, arguments, expiry, invocation idempotency, and byte/query budgets; Pi-side schema validation is not authorization.
- A capability is also bound to the current Turn fencing token, so an Agentd request from a reclaimed lease fails before any Store read.
- A1 tools are read-only: `platform_get_snapshot`, `workspace_list`, `workspace_search`, `workspace_read`, `run_get`, `run_log_read`, and `evidence_read`.
- The browser reads only durable Python events and never connects directly to Agentd.
- API/Worker/Agentd restart recovery, fencing, owner isolation, and event replay are completion gates.
- Local D0/D1 evidence is authoritative for this slice; remote VM, real 107, and a live campus model are not prerequisites.
- Begin every behavior change with a failing test, make the minimum implementation pass, and commit each task independently.

## Re-Baselined Roadmap

The completed A0 slice established the Pi service, model compatibility, faux provider, strict Python↔TypeScript boundary, and migrated explain/remediation calls. The next dependency order is:

1. **A1 — this plan:** durable read-only Sessions/Turns, typed read tools, event replay, and restart recovery.
2. **A2 — separate plan after A1 review:** `ExperimentProjectSession` with `blank | existing`, `WorkspaceSnapshot`, isolated `AgentWorkspace`, patch/diff/ChangeSet, and network-disabled `sandbox_exec`.
3. **A3 — separate plan after A2 review:** durable `AgentTask`, Slurm validation scheduling, Turn release/resume, Evidence reinjection, and `AgentResourceEnvelope`.
4. **A4 — separate plan after A3 review:** conflict-checked ChangeSet publication, explicit approval, Contract materialization, formal Run, and Runtime Watch handoff.
5. **A5 — separate plans by subsystem:** failed-Run repair profile first, then market application/publication profiles using the existing remediation and market domain services.

Resource-observability collection and Runtime Watch remain independent provider projects. A1 consumes their existing stored facts only; it does not add new Slurm polling. Full business-store PostgreSQL parity, real identity, and real 107 admission remain production gates and must not be mixed into A1 beyond new Agent Session tables having SQLite/PostgreSQL parity from day one.

---

### Task 1: Add the A1 versioned Turn and tool wire contracts

**Files:**
- Create: `schemas/agent/v2/README.md`
- Create: `schemas/agent/v2/turn-request.schema.json`
- Create: `schemas/agent/v2/tool-invocation.schema.json`
- Create: `schemas/agent/v2/tool-result.schema.json`
- Modify: `services/pilot-agentd/src/protocol.ts`
- Modify: `services/pilot-agentd/tests/protocol.test.ts`
- Modify: `src/pilot107/agent/protocol.py`
- Modify: `src/pilot107/agent/client.py`
- Modify: `tests/agent/test_protocol.py`
- Modify: `tests/agent/test_client.py`

**Interfaces:**
- Consumes: the existing v1 event and checkpoint formats.
- Produces: `DurableAgentTurnRequest`, `ToolInvocation`, `ToolResult`, `AgentdClient.stream_durable_turn()`, and the `a1-readonly` task/profile/toolset pairing.

- [x] **Step 1: Write failing cross-language contract tests**

Add a Python fixture and the equivalent TypeScript fixture:

```python
A1_REQUEST = {
    "schema_version": "pilot107.agent-turn-request/v2",
    "session_id": "session-1",
    "turn_id": "turn-1",
    "owner": "alice",
    "state_version": 3,
    "task_kind": "interactive_readonly",
    "model_profile_id": "faux-default",
    "prompt_profile_id": "hpc-readonly-v1",
    "toolset_id": "a1-readonly",
    "input": {"message": "why is run-1 pending?", "context_refs": ["run:run-1"]},
    "capability_token": "opaque.test.token",
    "checkpoint": None,
    "limits": {"timeout_ms": 60000, "max_output_tokens": 1200},
    "trace": {"correlation_id": "turn-1"},
}
```

Assert that both runtimes accept this closed object, reject an added field, reject the v2 fields on a v1 request, and reject any pairing other than `interactive_readonly / hpc-readonly-v1 / a1-readonly`.

- [x] **Step 2: Run the contract tests and verify RED**

Run:

```bash
PYTHONPATH=src uv run --extra dev pytest tests/agent/test_protocol.py tests/agent/test_client.py -q
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/workspace" \
  -w /workspace/services/pilot-agentd node:22.19.0-bookworm-slim \
  npm test -- tests/protocol.test.ts
```

Expected: the v2 fixtures fail because no v2 parser or client method exists; all existing v1 cases remain green.

- [x] **Step 3: Implement the closed v2 request and tool schemas**

Add these Python types and matching TypeBox schemas:

```python
@dataclass(frozen=True)
class DurableAgentTurnRequest:
    session_id: str
    turn_id: str
    owner: str
    state_version: int
    model_profile_id: str
    message: str
    context_refs: tuple[str, ...]
    capability_token: str = field(repr=False)
    checkpoint: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolInvocation:
    schema_version: str
    invocation_id: str
    idempotency_key: str
    owner: str
    session_id: str
    turn_id: str
    state_version: int
    profile_id: str
    tool_name: str
    arguments: dict[str, Any]
    deadline: str


@dataclass(frozen=True)
class ToolResult:
    schema_version: str
    invocation_id: str
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    evidence_refs: tuple[str, ...]
    bytes_returned: int
```

`AgentdClient.stream_durable_turn(request, on_event=None)` must serialize only the defined fields and reuse the existing bounded NDJSON parser for the response.

- [x] **Step 4: Generate and compare checked-in schemas**

Extend the existing schema-generation test so TypeBox output is semantically equal to all JSON files under `schemas/agent/v2/`. Keep v1 golden files unchanged.

- [x] **Step 5: Run GREEN and commit**

```bash
PYTHONPATH=src uv run --extra dev pytest tests/agent -q
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/workspace" \
  -w /workspace/services/pilot-agentd node:22.19.0-bookworm-slim \
  npm test -- tests/protocol.test.ts
git add schemas/agent/v2 services/pilot-agentd/src/protocol.ts \
  services/pilot-agentd/tests/protocol.test.ts src/pilot107/agent/protocol.py \
  src/pilot107/agent/client.py tests/agent
git commit -m "feat: define durable agent turn contracts"
```

---

### Task 2: Persist Agent Sessions, Turns, events, and invocation records in SQLite

**Files:**
- Create: `src/pilot107/agent/session.py`
- Create: `src/pilot107/agent/migrations.py`
- Create: `src/pilot107/agent/store.py`
- Create: `tests/agent/test_store.py`

**Interfaces:**
- Consumes: `SchemaMigration` and the existing checksum migration runner.
- Produces: `AgentSessionStore`, `AgentSessionRecord`, `AgentTurnRecord`, `AgentTurnEventRecord`, and `AgentToolInvocationRecord`.

- [x] **Step 1: Write failing state, idempotency, owner, and fencing tests**

Cover these exact transitions:

```python
session, created = store.create_session(
    owner="alice",
    request_key="create-1",
    profile_id="hpc-readonly-v1",
    model_profile_id="faux-default",
    source={"run_id": "run-1"},
)
turn, created = store.create_turn(
    session_id=session.session_id,
    owner="alice",
    request_key="message-1",
    message="why pending?",
    expected_state_version=session.state_version,
)
claim = store.claim_turn(turn.turn_id, worker_id="worker-a", lease_seconds=30)
store.append_event(turn.turn_id, claim=claim, sequence=1, event_type="turn_started", payload={})
```

Assert stable request-key replay, rejection of same key/different content, cross-owner 404 semantics, contiguous event sequence, lease reclaim incrementing `fencing_token`, stale writer rejection, cancel-request persistence, and terminal Session state updates.

- [x] **Step 2: Run the Store tests and verify RED**

```bash
PYTHONPATH=src uv run --extra dev pytest tests/agent/test_store.py -q
```

Expected: import failure for `pilot107.agent.store`.

- [x] **Step 3: Add migration `006a.001.agent_sessions`**

Create four tables with closed state checks and owner-scoped indexes:

```text
agent_sessions(session_id, owner, request_key, profile_id, model_profile_id,
               source_json, state, state_version, context_checkpoint_json,
               resource_usage_json, outcome_json, created_at, updated_at)
agent_turns(turn_id, session_id, owner, request_key, input_digest, message,
            state_version, state, lease_owner, lease_expires_at, fencing_token,
            event_sequence, final_checkpoint_json, error_json,
            created_at, started_at, finished_at)
agent_turn_events(event_id, turn_id, session_id, owner, sequence,
                  event_type, payload_json, created_at)
agent_tool_invocations(invocation_id, idempotency_key, turn_id, session_id,
                       owner, tool_name, arguments_digest, state,
                       result_json, error_json, bytes_returned, created_at, updated_at)
```

Use unique constraints `(owner, request_key)` for Sessions, `(session_id,
request_key)` for Turns, `(turn_id, sequence)` for events, and `(turn_id,
idempotency_key)` for tool invocations. Add a partial unique index on `owner` for
Turns in `running` state so two Workers cannot run concurrent Turns for the
same owner; a claim that loses this constraint race remains queued.

- [x] **Step 4: Implement CAS and lease-aware Store methods**

Define `AgentSessionStore` as a `Protocol`, implement it as
`SQLiteAgentSessionStore`, and expose these exact methods:

```python
class AgentSessionStore(Protocol):
    def create_session(
        self,
        *,
        owner: str,
        request_key: str,
        profile_id: str,
        model_profile_id: str,
        source: Mapping[str, object],
    ) -> tuple[AgentSessionRecord, bool]:
        raise NotImplementedError

    def get_session(self, session_id: str, *, owner: str) -> AgentSessionRecord:
        raise NotImplementedError

    def list_sessions_page(
        self,
        *,
        owner: str,
        states: frozenset[AgentSessionState] | None,
        before: str | None,
        limit: int,
    ) -> tuple[list[AgentSessionRecord], str | None]:
        raise NotImplementedError

    def create_turn(
        self,
        *,
        session_id: str,
        owner: str,
        request_key: str,
        message: str,
        expected_state_version: int,
    ) -> tuple[AgentTurnRecord, bool]:
        raise NotImplementedError

    def claim_turn(
        self, turn_id: str, *, worker_id: str, lease_seconds: int
    ) -> AgentTurnLease | None:
        raise NotImplementedError

    def renew_turn(
        self, claim: AgentTurnLease, *, lease_seconds: int
    ) -> AgentTurnLease:
        raise NotImplementedError

    def append_event(
        self,
        turn_id: str,
        *,
        claim: AgentTurnLease,
        sequence: int,
        event_type: str,
        payload: Mapping[str, object],
    ) -> AgentTurnEventRecord:
        raise NotImplementedError

    def request_cancel(
        self, turn_id: str, *, owner: str, expected_state_version: int
    ) -> AgentTurnRecord:
        raise NotImplementedError

    def complete_turn(
        self,
        turn_id: str,
        *,
        claim: AgentTurnLease,
        final_checkpoint: Mapping[str, object] | None,
        resource_usage: Mapping[str, object],
        outcome: Mapping[str, object],
    ) -> AgentTurnRecord:
        raise NotImplementedError

    def interrupt_turn(
        self,
        turn_id: str,
        *,
        claim: AgentTurnLease,
        checkpoint: Mapping[str, object] | None,
        error: Mapping[str, object],
    ) -> AgentTurnRecord:
        raise NotImplementedError

    def list_events_page(
        self,
        *,
        session_id: str,
        owner: str,
        after_event_id: int,
        limit: int,
    ) -> tuple[list[AgentTurnEventRecord], int | None]:
        raise NotImplementedError

    def list_recoverable_turns(self, *, limit: int) -> list[AgentTurnRecord]:
        raise NotImplementedError

    def reserve_tool_invocation(
        self,
        *,
        invocation_id: str,
        idempotency_key: str,
        owner: str,
        session_id: str,
        turn_id: str,
        expected_state_version: int,
        expected_fencing_token: int,
        tool_name: str,
        arguments_digest: str,
    ) -> tuple[AgentToolInvocationRecord, bool]:
        raise NotImplementedError

    def finish_tool_invocation(
        self,
        *,
        invocation_id: str,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        result: Mapping[str, object] | None,
        error: Mapping[str, object] | None,
        bytes_returned: int,
    ) -> AgentToolInvocationRecord:
        raise NotImplementedError

    def get_turn_tool_usage(
        self,
        *,
        turn_id: str,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
    ) -> AgentTurnToolUsage:
        raise NotImplementedError
```

Every mutating statement must include the current fencing token or expected state version in its `WHERE` clause and raise `AgentSessionConflict` when `rowcount != 1`.

- [x] **Step 5: Run GREEN and commit**

```bash
PYTHONPATH=src uv run --extra dev pytest tests/agent/test_store.py -q
uv run --extra dev ruff check src/pilot107/agent tests/agent
uv run --extra dev mypy src/pilot107/agent
git add src/pilot107/agent/session.py src/pilot107/agent/migrations.py \
  src/pilot107/agent/store.py tests/agent/test_store.py
git commit -m "feat: persist agent sessions and turns"
```

---

### Task 3: Add PostgreSQL parity and repository selection for Agent Sessions

**Files:**
- Modify: `src/pilot107/core/postgres_domain_schema.py`
- Create: `src/pilot107/agent/postgres_store.py`
- Create: `src/pilot107/agent/store_factory.py`
- Modify: `src/pilot107/core/postgres_domain_stores.py`
- Modify: `tests/test_postgres_domain_migration.py`
- Create: `tests/agent/test_postgres_store.py`
- Create: `tests/agent/test_store_contract.py`

**Interfaces:**
- Consumes: the Task 2 Store contract.
- Produces: `PostgresAgentSessionStore` and `build_agent_session_store(sqlite_path, postgres_dsn)`.

- [x] **Step 1: Extract a backend-neutral Store contract suite**

Make `tests/agent/test_store_contract.py` expose a concrete backend-neutral
scenario (continue the same function with cancellation, interruption, and
recovery assertions):

```python
def exercise_agent_store_contract(
    store: AgentSessionStore,
    *,
    advance_clock: Callable[[timedelta], None],
) -> None:
    session, created = store.create_session(
        owner="alice",
        request_key="session-1",
        profile_id="hpc-readonly-v1",
        model_profile_id="faux-default",
        source={"run_id": "run-1"},
    )
    assert created is True
    replay, replay_created = store.create_session(
        owner="alice",
        request_key="session-1",
        profile_id="hpc-readonly-v1",
        model_profile_id="faux-default",
        source={"run_id": "run-1"},
    )
    assert replay_created is False
    assert replay.session_id == session.session_id

    turn, turn_created = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-1",
        message="why pending?",
        expected_state_version=session.state_version,
    )
    assert turn_created is True
    first = store.claim_turn(turn.turn_id, worker_id="worker-a", lease_seconds=1)
    assert first is not None
    event = store.append_event(
        turn.turn_id,
        claim=first,
        sequence=1,
        event_type="turn_started",
        payload={},
    )
    assert event.sequence == 1

    advance_clock(timedelta(seconds=2))
    second = store.claim_turn(turn.turn_id, worker_id="worker-b", lease_seconds=30)
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(AgentSessionConflict):
        store.append_event(
            turn.turn_id,
            claim=first,
            sequence=2,
            event_type="message_delta",
            payload={"delta": "stale"},
        )
```

The SQLite test must call this suite before the PostgreSQL adapter exists. The
same suite must also cover invocation reservation/replay, same-key/different-
digest conflict, byte aggregation, and rejection after the Turn fence changes.

- [x] **Step 2: Run the PostgreSQL tests and verify RED**

```bash
PYTHONPATH=src uv run --extra dev pytest \
  tests/test_postgres_domain_migration.py tests/agent/test_postgres_store.py -q
```

Expected with the integration DSN configured: missing Agent tables and adapter failures. Without the DSN, only the marked integration cases skip; migration-shape tests still fail.

- [x] **Step 3: Add migration `004a.005.agent_sessions` and the PostgreSQL Store**

Use native `JSONB`, `BIGSERIAL` event IDs, `TIMESTAMPTZ`, row locks, and an
atomic `UPDATE` whose equality predicate includes `fencing_token` and whose
`RETURNING` clause yields the updated record. Do not inherit SQLite SQL or
translate `?` placeholders.

- [x] **Step 4: Wire runtime selection**

```python
def build_agent_session_store(
    *, sqlite_path: Path, postgres_dsn: str | None
) -> AgentSessionStore:
    if postgres_dsn:
        return PostgresAgentSessionStore(postgres_dsn)
    return SQLiteAgentSessionStore(sqlite_path)
```

Add Agent tables to `expected_postgres_domain_tables()` and the checksum migration test.

- [x] **Step 5: Run both backends and commit**

```bash
PYTHONPATH=src uv run --extra dev pytest tests/agent/test_store.py \
  tests/test_postgres_domain_migration.py tests/agent/test_postgres_store.py -q
git add src/pilot107/core/postgres_domain_schema.py \
  src/pilot107/core/postgres_domain_stores.py src/pilot107/agent/postgres_store.py \
  src/pilot107/agent/store_factory.py tests/agent/test_store_contract.py \
  tests/agent/test_postgres_store.py tests/test_postgres_domain_migration.py
git commit -m "feat: add postgres agent session parity"
```

---

### Task 4: Implement capability tokens and the authoritative read Tool Gateway

**Files:**
- Create: `src/pilot107/agent/capabilities.py`
- Create: `src/pilot107/agent/tool_gateway.py`
- Create: `src/pilot107/agent/read_tools.py`
- Create: `src/pilot107/api/agent_tool_routes.py`
- Modify: `src/pilot107/api/http_app.py`
- Modify: `src/pilot107/api/asgi_app.py`
- Modify: `src/pilot107/api/service.py`
- Create: `tests/agent/test_capabilities.py`
- Create: `tests/agent/test_tool_gateway.py`
- Create: `tests/test_agent_tool_gateway_api.py`
- Modify: `tests/test_api_service.py`

**Interfaces:**
- Consumes: Agent Session Store, RunStore, PlatformSnapshotStore, EvidenceStore, and bounded WorkspaceReader abstractions.
- Produces: `AgentCapabilitySigner`, `AgentToolGateway.invoke()`,
  `AgentReadContext`, seven A1 handlers, and private endpoint
  `POST /internal/v1/agent-tools/invoke`.

- [x] **Step 1: Write failing token and Tool Gateway tests**

Test a capability bound to:

```python
claims = AgentCapabilityClaims(
    owner="alice",
    session_id="session-1",
    turn_id="turn-1",
    state_version=3,
    fencing_token=7,
    profile_id="hpc-readonly-v1",
    tools=frozenset({"run_get", "evidence_read"}),
    max_invocations=8,
    max_bytes=262_144,
    expires_at=clock() + 60,
)
```

Assert signature tampering, expiry, wrong owner/session/Turn/profile/state
version/fencing token, an unlisted tool, excess invocation count, excess
cumulative bytes, path traversal, foreign-owner run/evidence/workspace access,
and changed idempotency content all fail closed without returning private data.

- [x] **Step 2: Run the tests and verify RED**

```bash
PYTHONPATH=src uv run --extra dev pytest \
  tests/agent/test_capabilities.py tests/agent/test_tool_gateway.py \
  tests/test_agent_tool_gateway_api.py -q
```

Expected: missing capability and Tool Gateway modules.

- [x] **Step 3: Implement an opaque HMAC-SHA256 capability**

Encode canonical JSON claims with unpadded base64url and sign
`b"pilot107-agent-capability-v1." + payload`. Claims include `owner`, Session,
Turn, state version, fencing token, profile, tool allowlist, invocation/byte
budgets, issue time, and expiry. Verification must use `hmac.compare_digest`,
accept at most 120 seconds of lifetime, allow at most 5 seconds of clock skew,
and never include the signing secret in exceptions or reprs.

- [x] **Step 4: Implement the seven read handlers with hard bounds**

Use these request maxima:

| Tool | Bound |
| --- | --- |
| `platform_get_snapshot` | latest owner snapshot, 128 KiB serialized |
| `workspace_list` | 500 paths, metadata only, no `.git` or symlink targets |
| `workspace_search` | 100 matches, 200-character snippets, 256 KiB total |
| `workspace_read` | one regular text file, 64 KiB |
| `run_get` | one owner Run plus safe state/resource summary, 64 KiB |
| `run_log_read` | one stream, explicit cursor, 64 KiB |
| `evidence_read` | one owner-bound evidence object/snippet, 64 KiB |

Return `ToolResult` with stable evidence references and `bytes_returned`; never return raw command argv, provider keys, secrets, or unbounded log/file content.

- [x] **Step 5: Persist invocation idempotency and budgets**

`AgentToolGateway.invoke(token, invocation)` must reserve the invocation in `agent_tool_invocations` before reading. A retry with the same `(turn_id, idempotency_key, arguments_digest)` returns the stored result; different arguments raise `AGENT.TOOL.IDEMPOTENCY_CONFLICT`.

- [x] **Step 6: Expose only the private Tool Gateway endpoint**

`AgentToolRoutes` accepts exactly one JSON `ToolInvocation`, reads the
capability only from `Authorization: Bearer`, invokes `AgentToolGateway`, and
returns exactly one `ToolResult`. It must not accept `UserIdentity` headers as
authorization, must cap the request body at 1 MiB, and must map malformed,
expired, fenced, unauthorized, budget, conflict, and internal failures to
closed redacted error bodies. Add ASGI tests proving the public route table does
not advertise this endpoint and an unsigned request cannot reach any reader.
Add `PILOT107_AGENT_CAPABILITY_HMAC_SECRET_FILE` to `ApiServiceConfig`; the
service builder reads exactly that configured file, requires at least 32 random
bytes when A1 is enabled, rejects simultaneous inline/file sources, and passes
the secret only to `AgentCapabilitySigner`. Compose deployments use the file
source only.

- [x] **Step 7: Run GREEN and commit**

```bash
PYTHONPATH=src uv run --extra dev pytest \
  tests/agent/test_capabilities.py tests/agent/test_tool_gateway.py \
  tests/test_agent_tool_gateway_api.py tests/test_api_service.py -q
git add src/pilot107/agent/capabilities.py src/pilot107/agent/tool_gateway.py \
  src/pilot107/agent/read_tools.py src/pilot107/api/agent_tool_routes.py \
  src/pilot107/api/http_app.py src/pilot107/api/asgi_app.py src/pilot107/api/service.py \
  tests/agent/test_capabilities.py tests/agent/test_tool_gateway.py \
  tests/test_agent_tool_gateway_api.py tests/test_api_service.py
git commit -m "feat: authorize readonly agent tools"
```

---

### Task 5: Register A1 read tools in Agentd and call the private Tool Gateway

**Files:**
- Create: `services/pilot-agentd/src/tool-gateway.ts`
- Create: `services/pilot-agentd/src/read-tools.ts`
- Modify: `services/pilot-agentd/src/config.ts`
- Modify: `services/pilot-agentd/src/tasks.ts`
- Modify: `services/pilot-agentd/src/turn-executor.ts`
- Modify: `services/pilot-agentd/src/main.ts`
- Create: `services/pilot-agentd/tests/tool-gateway.test.ts`
- Create: `services/pilot-agentd/tests/readonly-turn.integration.test.ts`
- Modify: `services/pilot-agentd/tests/config.test.ts`
- Modify: `services/pilot-agentd/tests/tasks.test.ts`

**Interfaces:**
- Consumes: v2 Turn requests, `ToolInvocation`, `ToolResult`, and `PILOT107_AGENTD_TOOL_GATEWAY_URL`.
- Produces: `ToolGatewayClient.invoke()`, `createReadOnlyTools()`, and the `interactive_readonly` Pi task.

- [ ] **Step 1: Write failing tool trajectory and hostile-gateway tests**

Use a local mock Tool Gateway and a Pi faux response containing:

```typescript
fauxToolCall("run_get", { run_id: "run-1" }, { id: "call-run-1" })
```

Assert exact invocation fields, bearer capability forwarding, deterministic idempotency key, ToolResult schema validation, one terminal event, and public answer continuation after the tool result. Cover timeout, 401, 403, 409, 429, 5xx, wrong content type, oversized body, malformed JSON, unknown fields, invocation ID mismatch, and response text containing secrets.

- [ ] **Step 2: Run RED**

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/workspace" \
  -w /workspace/services/pilot-agentd node:22.19.0-bookworm-slim \
  npm test -- tests/tool-gateway.test.ts tests/readonly-turn.integration.test.ts
```

Expected: missing Tool Gateway client and A1 task registration.

- [ ] **Step 3: Implement the bounded Tool Gateway client**

Use `fetch` with the Turn abort signal, a 10-second per-call maximum,
`application/json; charset=utf-8`, a 1 MiB body ceiling, no redirects, and no
proxy/provider fallback. Construct the `Authorization` value as
`"Bearer " + capabilityToken`; redact it from all errors.

- [ ] **Step 4: Register only the seven tools for the A1 pairing**

`prepareTask()` must return no read tools for A0 interactive/explain/patch/remediation requests. For `interactive_readonly`, construct seven `AgentTool` objects with closed TypeBox argument schemas and `executionMode: "sequential"`.

- [ ] **Step 5: Prevent authority from entering events or checkpoints**

Extend sanitization tests so capability tokens, Tool Gateway URLs, authorization headers, and full invocation envelopes never appear in `message_delta`, checkpoint messages, terminal results, or public error messages.

- [ ] **Step 6: Run GREEN and commit**

```bash
cd services/pilot-agentd
npm run check
cd ../..
git add services/pilot-agentd/src services/pilot-agentd/tests
git commit -m "feat: execute readonly agent tools"
```

---

### Task 6: Add durable orchestration, Worker dispatch, cancellation, and recovery

**Files:**
- Create: `src/pilot107/services/agent_session_service.py`
- Create: `src/pilot107/worker/agent_turn_worker.py`
- Modify: `src/pilot107/worker/runtime_worker.py`
- Modify: `src/pilot107/worker/service.py`
- Create: `tests/test_agent_session_service.py`
- Create: `tests/test_agent_turn_worker.py`
- Modify: `tests/test_runtime_worker.py`
- Modify: `tests/test_worker_service.py`

**Interfaces:**
- Consumes: AgentSessionStore, ControlRepository, AgentdClient, AgentCapabilitySigner, and AgentToolGateway configuration.
- Produces: `AgentSessionService`, `AgentTurnWorker.dispatch_due()`, outbox topic `agent.turn.execute.v1`, and durable retry/cancel recovery.

- [ ] **Step 1: Write failing orchestration and crash-window tests**

Cover these failure windows:

1. Session/Turn committed before outbox enqueue.
2. Outbox claimed before Turn lease claim.
3. Agentd emits an event and Worker crashes before the next event.
4. Agentd completes and Worker crashes before outbox acknowledge.
5. API requests cancellation while Agentd is active.
6. Worker lease expires and a second Worker reclaims the Turn.
7. Old Worker attempts an event/terminal write with a stale fence.
8. Agentd transport fails with and without a checkpoint.
9. Two Workers claim queued Turns for Alice while Bob has an independent Turn.

Assert exactly one durable event sequence, no duplicate tool invocation, at
most one running Turn for Alice, and no head-of-line blocking for Bob.

- [ ] **Step 2: Run RED**

```bash
PYTHONPATH=src uv run --extra dev pytest \
  tests/test_agent_session_service.py tests/test_agent_turn_worker.py \
  tests/test_runtime_worker.py tests/test_worker_service.py -q
```

Expected: missing service and worker modules.

- [ ] **Step 3: Implement create/enqueue/recovery semantics**

`AgentSessionService.submit_message()` must persist the Turn first, then idempotently enqueue:

```python
message_id = f"agent-turn:{turn.turn_id}"
control_repository.enqueue(
    message_id=message_id,
    topic="agent.turn.execute.v1",
    aggregate_id=turn.turn_id,
    payload={"turn_id": turn.turn_id},
)
```

`recover_pending_turns()` scans pending/interrupted read-only Turns and recreates
the same outbox message ID. After claiming the Turn, the Worker signs a fresh
capability containing the claim's fencing token; it never persists the encoded
token or publishes it as an event. Worker configuration reads the same
`PILOT107_AGENT_CAPABILITY_HMAC_SECRET_FILE`; startup fails closed when A1 is
enabled and the file is absent or too short.

- [ ] **Step 4: Persist each Agentd event before progress publication**

The callback order must be:

```python
store.append_event(turn_id, claim=claim, sequence=event.sequence,
                   event_type=event.type, payload=event.payload)
publish_event_hint(session_id, event.sequence)
```

Hints may be lost; Store events may not. On replay after interruption, pass only the last validated checkpoint. A1 read tools make the Turn retry-safe, but invocation idempotency still prevents duplicate reads from consuming budget twice.

- [ ] **Step 5: Implement durable cancellation**

The API sets `cancel_requested`. The active Worker checks before Agentd invocation and after every event; it calls `AgentdClient.cancel_turn(turn_id)` once, persists the terminal `aborted` event/checkpoint, and maps the Session back to `idle` unless the user cancelled the whole Session.

- [ ] **Step 6: Run GREEN and commit**

```bash
PYTHONPATH=src uv run --extra dev pytest \
  tests/test_agent_session_service.py tests/test_agent_turn_worker.py \
  tests/test_runtime_worker.py tests/test_worker_service.py -q
git add src/pilot107/services/agent_session_service.py \
  src/pilot107/worker/agent_turn_worker.py src/pilot107/worker/runtime_worker.py \
  src/pilot107/worker/service.py tests/test_agent_session_service.py \
  tests/test_agent_turn_worker.py tests/test_runtime_worker.py tests/test_worker_service.py
git commit -m "feat: dispatch recoverable agent turns"
```

---

### Task 7: Expose owner-scoped Session, Turn, cancel, and event replay APIs

**Files:**
- Create: `src/pilot107/api/agent_session_routes.py`
- Modify: `src/pilot107/api/http_app.py`
- Modify: `src/pilot107/api/service.py`
- Modify: `src/pilot107/api/asgi_app.py`
- Modify: `tests/test_api_service.py`
- Create: `tests/test_agent_session_api.py`
- Modify: `tests/test_asgi_app.py`
- Modify: `tests/snapshots/openapi_phase3b.json`

**Interfaces:**
- Consumes: AgentSessionService and durable Store pages.
- Produces: authenticated HTTP routes and opaque-cursor event replay.

- [ ] **Step 1: Write failing API, permission, idempotency, and replay tests**

Add these routes:

```text
POST /api/v1/agent-sessions
GET  /api/v1/agent-sessions
GET  /api/v1/agent-sessions/{session_id}
POST /api/v1/agent-sessions/{session_id}/turns
POST /api/v1/agent-sessions/{session_id}/turns/{turn_id}/cancel
GET  /api/v1/agent-sessions/{session_id}/events?after_event_id=N&limit=100
```

Assert Alice/Bob isolation, server-derived owner, unknown-field rejection, stable request-key replay, stale state-version conflict, bounded message length, invalid cursor rejection, reconnect after event N, and cancellation idempotency.

- [ ] **Step 2: Run RED**

```bash
PYTHONPATH=src uv run --extra dev pytest \
  tests/test_agent_session_api.py tests/test_api_service.py tests/test_asgi_app.py -q
```

Expected: 404 or missing route class.

- [ ] **Step 3: Implement the route module**

Follow `RemediationRoutes`: all owners come from `UserIdentity`, list cursors bind owner/state scope, errors use stable `AGENT.SESSION.*` and `AGENT.TURN.*` codes, and response payloads exclude capability tokens, leases, provider credentials, and raw checkpoint messages.

- [ ] **Step 4: Add durable event streaming compatibility**

The existing event transport may send a lightweight `agent.session_event_available` notification, but clients must resume through the paged durable endpoint. Add `Last-Event-ID` coverage proving a browser/API restart does not lose or duplicate events.

- [ ] **Step 5: Update OpenAPI and run GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest \
  tests/test_agent_session_api.py tests/test_api_service.py tests/test_asgi_app.py -q
git add src/pilot107/api/agent_session_routes.py src/pilot107/api/http_app.py \
  src/pilot107/api/service.py src/pilot107/api/asgi_app.py \
  tests/test_agent_session_api.py tests/test_api_service.py tests/test_asgi_app.py \
  tests/snapshots/openapi_phase3b.json
git commit -m "feat: expose durable agent sessions"
```

---

### Task 8: Wire the private Tool Gateway and secret boundaries in Compose

**Files:**
- Modify: `simulator/compose/compose.yml`
- Modify: `simulator/compose/compose.competition.yml`
- Modify: `simulator/compose/compose.competition-app-node.yml`
- Modify: `simulator/compose/.env.example`
- Modify: `simulator/compose/.env.competition.example`
- Modify: `simulator/compose/.env.cpu-rc.example`
- Modify: `tests/test_agentd_compose.py`
- Modify: `scripts/check-pilot-agentd.sh`

**Interfaces:**
- Consumes: Python Tool Gateway endpoint and Agentd `PILOT107_AGENTD_TOOL_GATEWAY_URL`.
- Produces: private-network A1 deployment with signing authority retained by Python.

- [ ] **Step 1: Write failing Compose isolation tests**

Assert:

```python
assert agentd_env["PILOT107_AGENTD_TOOL_GATEWAY_URL"] == "http://pilot107-api:8080/internal/v1/agent-tools/invoke"
assert "PILOT107_AGENT_CAPABILITY_HMAC_SECRET_FILE" not in agentd_env
assert "PILOT107_POSTGRES_DSN" not in agentd_env
assert "PILOT107_SLURM_TOKEN" not in agentd_env
assert agentd.get("ports", []) == []
```

Also assert API and Worker receive
`PILOT107_AGENT_CAPABILITY_HMAC_SECRET_FILE=/run/secrets/pilot107-agent-capability-hmac`
through deployment secret injection, not a committed value.

- [ ] **Step 2: Run RED**

```bash
PYTHONPATH=src uv run --extra dev pytest tests/test_agentd_compose.py -q
```

Expected: missing Tool Gateway and capability-secret configuration.

- [ ] **Step 3: Wire the private URL and capability secret**

Use a separate `pilot107-agent-capability-hmac` Compose secret file. Mount it read-only into API and Worker; pass only the Tool Gateway URL to Agentd. Do not publish the API internal route separately or add a host port to Agentd.

- [ ] **Step 4: Render all profiles and commit**

```bash
PYTHONPATH=src uv run --extra dev pytest tests/test_agentd_compose.py -q
sh simulator/compose/scripts/check-compose-config.sh
git add simulator/compose tests/test_agentd_compose.py scripts/check-pilot-agentd.sh
git commit -m "feat: isolate the agent tool gateway"
```

---

### Task 9: Prove the A1 vertical slice, restart recovery, and resource bounds

**Files:**
- Create: `scripts/smoke-pilot-agent-a1.py`
- Create: `scripts/smoke-pilot-agent-a1.sh`
- Create: `scripts/fault-pilot-agent-a1.sh`
- Create: `tests/test_pilot_agent_a1_vertical.py`
- Modify: `scripts/check-pilot-agentd.sh`
- Modify: `scripts/check-ci-local.sh`
- Modify: `apps/api/README.md`
- Modify: `simulator/compose/README.md`
- Modify: `docs/phase-3/current_status_index.md`

**Interfaces:**
- Consumes: the complete Python API→outbox Worker→Agentd→Tool Gateway→Store path.
- Produces: one-command D1 evidence for A1 and an updated authoritative status baseline.

- [ ] **Step 1: Write failing vertical tests**

The test must seed owner-scoped platform/run/log/evidence/workspace fixtures, create an Alice Session over HTTP, submit a message, and assert the faux trajectory calls at least `run_get`, `run_log_read`, and `evidence_read`. A Bob read of the Session and any Alice fixture must return 404/forbidden without content leakage.

- [ ] **Step 2: Add restart and fault injection**

`fault-pilot-agent-a1.sh` must stop and restart each component at a deterministic barrier:

```text
API after Turn commit
Worker after outbox claim
Agentd after one persisted tool result
browser connection after event N
```

For every barrier, assert one Turn, contiguous durable events, one logical tool invocation per idempotency key, no stale-fence write, and a terminal or explicitly interrupted recoverable state.

- [ ] **Step 3: Add performance and budget assertions**

With 100 persisted idle Sessions and 10 concurrent faux Turns, record Agentd
RSS/CPU, queue wait, event lag, tool invocation count, and returned bytes.
Assert idle Sessions create no per-session process or retained Pi Turn, all Pi
Turns are released after the terminal event, each Turn stays within 32 tool
invocations and 1 MiB cumulative tool output, and no log/file read exceeds its
per-tool bound. Record the measured shared-Agentd RSS delta as the baseline for
later capacity policy rather than inventing a production threshold in D1.

- [ ] **Step 4: Add the one-command smoke and docs**

```bash
bash scripts/build-app-images.sh
bash scripts/smoke-pilot-agent-a1.sh
bash scripts/fault-pilot-agent-a1.sh
```

Document Session states, retry/cancel semantics, Tool Gateway security, capability-secret placement, durable event replay, and the explicit absence of write/submit capabilities in A1.

- [ ] **Step 5: Run the full completion gate**

```bash
bash scripts/check-pilot-agentd.sh
bash scripts/smoke-pilot-agent-a1.sh
bash scripts/fault-pilot-agent-a1.sh
PYTHONPATH=src uv run --extra dev pytest -q
uv run --extra dev ruff check src tests scripts
uv run --extra dev mypy src
sh simulator/compose/scripts/check-compose-config.sh
git diff --check
git status --short
```

Expected: all commands pass; only explicitly preserved user-owned untracked files may remain.

- [ ] **Step 6: Commit A1 evidence and status**

```bash
git add scripts/smoke-pilot-agent-a1.py scripts/smoke-pilot-agent-a1.sh \
  scripts/fault-pilot-agent-a1.sh tests/test_pilot_agent_a1_vertical.py \
  scripts/check-pilot-agentd.sh scripts/check-ci-local.sh apps/api/README.md \
  simulator/compose/README.md docs/phase-3/current_status_index.md
git commit -m "test: verify durable readonly agent turns"
```

## A1 Completion Audit

Before marking A1 complete, require evidence for every row:

| Requirement | Evidence |
| --- | --- |
| v1 compatibility | all existing Agentd/Python v1 suites unchanged and green |
| versioned A1 contract | v2 schema golden comparison in Python and TypeScript |
| durable Sessions/Turns | SQLite and PostgreSQL Store contract suites |
| owner isolation | Tool Gateway and HTTP Alice/Bob negative tests |
| minimum toolset | seven registered tools and rejection of every unknown tool |
| no execution authority | no write, shell, SSH, submit, cancel-Run, or publish tool registered |
| capability enforcement | tamper/expiry/scope/state/tool/budget tests |
| event durability | persist-before-publish tests and reconnect replay |
| idempotency/fencing | crash-window, reclaim, and stale-writer tests |
| cancellation/recovery | active cancellation plus API/Worker/Agentd restart matrix |
| deployment isolation | rendered Compose inspection and architecture gate |
| local vertical evidence | faux D1 smoke and deterministic fault script |
| resource bounds | 100-idle/10-active benchmark report |
| repository quality | full pytest, Ruff, mypy, Agentd check, Compose, and diff check |

Do not start A2 until every A1 row has objective local evidence and all blocking review findings are closed.
