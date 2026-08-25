# 107Pilot VM Demo Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the CPU VM's local Slurm stack into the authoritative demo scheduler, expose a stable 6-CPU/10-GiB job envelope, restore containerized Sandbox execution, add authorized path entry and bounded file search, and close the AgentTask-to-Slurm lifecycle on the deployed VM.

**Architecture:** Keep the existing command-gateway as the typed transport to the VM-local Slurm and shared `/public` filesystem. Add one bounded file-search projection to `FileOpsExecutor`, expose it through `FileRoutes`, and build small path/search components around the existing multi-pane manager. Reuse the durable AgentTask service and add owner-scoped read/cancel routes plus a UI projection; do not introduce arbitrary remote shell or claim external real-107 connectivity.

**Tech Stack:** Python 3.12, stdlib HTTP/SQLite, Docker Compose, Slurm 23.11 simulator image, bubblewrap, React 18, TypeScript 5.7, TanStack Query, Vitest, pytest/unittest, Bash release tooling.

## Global Constraints

- The deployment is labelled `VM 本机 Slurm（演示）`; it is not external real 107 or campus multi-user production.
- Slurm advertises exactly 6 CPUs, 10240 MiB, zero GPUs, partition `CPU-RC`, QoS `qos_cpu_rc`, and four-hour maximum wall time.
- Roughly 2 CPUs and 5 GiB remain for MariaDB, Slurm control services, API, Worker, Agentd, Web, and proxy.
- Sandbox remains fail closed, argv-only, non-root, no-network, clear-environment, resource-bounded, and Workspace/ChangeSet owner-bound.
- File search matches names and relative paths only, never file contents; it does not follow symlinks or return special files.
- Search pages contain at most 100 results and enforce both scan-count and elapsed-time budgets.
- Browser input never expands owner roots and never supplies arbitrary shell or remote program source.
- AgentTask requests cannot exceed 6 CPU, 10240 MiB, zero GPU, four hours, or their approved resource envelope.
- Deployment preserves database/Evidence/Capsule volumes, remote secrets, and old release directories; never use `down -v`.

---

### Task 1: Align the VM Slurm and capability envelopes

**Files:**
- Modify: `tests/test_cpu_rc_profile.py`
- Modify: `simulator/compose/slurm-cpu-rc/slurm.conf`
- Modify: `simulator/compose/compose.cpu-rc.yml`
- Modify: `config/platform_profiles/cpu-only-8c16g.json`
- Modify: `scripts/apply-cpu-rc-profile.sh`

**Interfaces:**
- Consumes: existing `CPU-RC` partition and `qos_cpu_rc` identifiers.
- Produces: one consistent 6-CPU/10240-MiB scheduler, compose, accounting, and API capability contract.

- [ ] **Step 1: Change the profile contract test first**

```python
def test_capability_profile_is_cpu_only_and_matches_vm_demo_capacity(self) -> None:
    profile = load_capability_profile(PROFILE)
    self.assertEqual(max(qos.max_cpus or 0 for qos in profile.qos), 6)
    self.assertEqual(max(qos.max_memory_gb or 0 for qos in profile.qos), 10)

def test_slurm_and_compose_envelopes_do_not_expose_gpu_resources(self) -> None:
    slurm = (ROOT / "simulator/compose/slurm-cpu-rc/slurm.conf").read_text()
    compose = (ROOT / "simulator/compose/compose.cpu-rc.yml").read_text()
    self.assertIn("NodeName=anode16 CPUs=6", slurm)
    self.assertIn("RealMemory=10240", slurm)
    self.assertIn("cpus: 6.0", compose)
    self.assertIn("mem_limit: 10g", compose)

def test_fresh_accounting_profile_is_seeded_before_controller_validation(self) -> None:
    script = (ROOT / "scripts/apply-cpu-rc-profile.sh").read_text()
    self.assertIn("MaxTRESPerJob=cpu=6,mem=10G", script)
    self.assertIn("GrpTRES=cpu=6,mem=10G", script)
```

- [ ] **Step 2: Run the focused test and observe the old 8/15 assertions fail**

Run: `uv run pytest -q tests/test_cpu_rc_profile.py`

Expected: failures show `8`, `15360`, and `15G` where the test requires `6`, `10240`, and `10G`.

- [ ] **Step 3: Update all capacity authorities atomically**

Set the Slurm node to `CPUs=6 RealMemory=10240`; set `worker-1` to `cpus: 6.0` and `mem_limit: 10g`; set capability `max_cpus` to `6`, `max_memory_gb` to `10`, source/notes to `vm-demo-6c10g`; and set accounting to:

```bash
run_sacctmgr modify qos qos_cpu_rc set \
  MaxWall=04:00:00 MaxTRESPerJob=cpu=6,mem=10G GrpTRES=cpu=6,mem=10G || true
```

- [ ] **Step 4: Run profile and compose validation**

Run: `uv run pytest -q tests/test_cpu_rc_profile.py && docker compose --env-file simulator/compose/.env.cpu-rc.example -f simulator/compose/compose.yml -f simulator/compose/compose.competition.yml -f simulator/compose/compose.cpu-rc.yml --profile competition config >/dev/null`

Expected: all profile tests pass and compose exits 0.

- [ ] **Step 5: Commit the resource contract**

```bash
git add tests/test_cpu_rc_profile.py simulator/compose/slurm-cpu-rc/slurm.conf \
  simulator/compose/compose.cpu-rc.yml config/platform_profiles/cpu-only-8c16g.json \
  scripts/apply-cpu-rc-profile.sh
git commit -m "fix: reserve VM resources from Slurm jobs"
```

### Task 2: Put bubblewrap in the final application image

**Files:**
- Create: `scripts/check-app-sandbox-image.sh`
- Modify: `apps/Dockerfile`
- Modify: `scripts/check-app-images.sh`
- Test: `tests/test_app_sandbox_image_contract.py`

**Interfaces:**
- Consumes: `SandboxExecutor` default `/usr/bin/bwrap` and compose security options.
- Produces: `check-app-sandbox-image.sh IMAGE_REF` returning 0 only when a UID 10700 container executes a real isolated Python validation.

- [ ] **Step 1: Add a failing final-image contract test**

```python
def test_application_dockerfile_installs_bubblewrap() -> None:
    dockerfile = (ROOT / "apps/Dockerfile").read_text()
    install = dockerfile[dockerfile.index("RUN apt-get update"):]
    assert "bubblewrap" in install

def test_app_image_check_invokes_real_bwrap_as_runtime_uid() -> None:
    script = (ROOT / "scripts/check-app-sandbox-image.sh").read_text()
    assert "--user 10700:10700" in script
    assert "--cap-drop ALL" in script
    assert "no-new-privileges:true" in script
    assert "SandboxExecutor" in script
```

- [ ] **Step 2: Verify the test fails because the package and script are absent**

Run: `uv run pytest -q tests/test_app_sandbox_image_contract.py`

Expected: the Dockerfile assertion fails and the script path is missing.

- [ ] **Step 3: Install bubblewrap and implement the image smoke**

Add `bubblewrap` to the existing apt transaction in `apps/Dockerfile`. The smoke script must run the supplied image with the production security boundary and execute:

```python
workspace = AgentWorkspaceRecord(
    workspace_id="image-smoke", project_id="image-smoke", owner="alice",
    local_root="/tmp/workspace", snapshot=WorkspaceSnapshot(
        source_ref="/public/home/alice", digest="a" * 64, entries=(),
        captured_at="2026-08-25T00:00:00Z",
    ), created_at="2026-08-25T00:00:00Z", updated_at="2026-08-25T00:00:00Z",
)
result = SandboxExecutor().execute(
    workspace, argv=("python", "-c", "import socket; print('sandbox-ok')"), timeout=5,
)
assert result.status == "succeeded" and result.stdout == "sandbox-ok\n"
```

The `docker run` command uses `--read-only`, `--tmpfs /tmp:rw,nosuid,nodev`, `--cap-drop ALL`, `--security-opt no-new-privileges:true`, and `--user 10700:10700`.

- [ ] **Step 4: Rebuild and run the real image smoke**

Run: `PILOT107_API_IMAGE=pilot107/api:sandbox-check PILOT107_WORKER_IMAGE=pilot107/worker:sandbox-check PILOT107_WEB_IMAGE=pilot107/web:sandbox-check bash scripts/build-app-images.sh && bash scripts/check-app-sandbox-image.sh pilot107/api:sandbox-check`

Expected: output contains `sandbox-image=PASS image=pilot107/api:sandbox-check` and exits 0.

- [ ] **Step 5: Run Sandbox unit tests and image checks**

Run: `uv run pytest -q tests/agent/test_workspace_sandbox.py tests/test_app_sandbox_image_contract.py && bash scripts/check-app-images.sh`

Expected: all tests and checks pass.

- [ ] **Step 6: Commit the runtime dependency fix**

```bash
git add apps/Dockerfile scripts/check-app-sandbox-image.sh scripts/check-app-images.sh \
  tests/test_app_sandbox_image_contract.py
git commit -m "fix: ship bubblewrap in app images"
```

### Task 3: Add a bounded file-search projection to the gateway

**Files:**
- Modify: `src/pilot107/adapters/slurm.py`
- Modify: `simulator/compose/scripts/command-gateway.py`
- Modify: `tests/test_command_gateway.py`
- Modify: `tests/test_file_ops_executor.py`

**Interfaces:**
- Produces: `FileSearchRequest`, `FileSearchEntry`, `FileSearchPage`, and `FileOpsExecutor.search_files(...) -> FileSearchPage`.
- Gateway endpoint: `POST /search_files` with an owner-bound structured JSON request.

- [ ] **Step 1: Write failing gateway tests for matching and safety**

```python
def test_search_files_matches_name_and_relative_path_without_following_symlinks(self) -> None:
    root = Path(self.temp.name) / "public/home/alice"
    (root / "models/v1").mkdir(parents=True)
    (root / "models/v1/weights.bin").write_bytes(b"123")
    (root / "model-link").symlink_to(root / "models", target_is_directory=True)
    config = gateway.GatewayConfig(token=None, allowed_roots=[str(root)])
    page = gateway._search_files({
        "root": str(root), "owner": "alice", "q": "MODEL",
        "kind": "all", "limit": 100, "cursor": None,
        "scan_limit": 1000, "time_limit_ms": 1000,
    }, config)
    assert [item["relative_path"] for item in page["items"]] == [
        "models", "models/v1", "models/v1/weights.bin"
    ]
```

Add separate tests asserting an outside root is rejected, `limit=101` is rejected, special files are omitted, a low `scan_limit` returns `incomplete=True`, and a cursor cannot be replayed with another owner/root/query.

- [ ] **Step 2: Run the gateway tests and verify `_search_files` is missing**

Run: `uv run pytest -q tests/test_command_gateway.py -k search_files`

Expected: failures identify the missing `_search_files` projection.

- [ ] **Step 3: Define typed search records and protocol method**

Add immutable records to `src/pilot107/adapters/slurm.py`:

```python
@dataclass(frozen=True)
class FileSearchEntry:
    path: str
    relative_path: str
    type: str
    size: int
    mtime: int

@dataclass(frozen=True)
class FileSearchPage:
    items: tuple[FileSearchEntry, ...]
    incomplete: bool
    next_cursor: str | None
    warnings: tuple[str, ...]
```

Add `search_files` to `FileOpsExecutor`, `HttpCommandGatewayExecutor`, and `LocalFilesystemExecutor` with explicit `root`, filters, `limit`, `cursor`, `scan_limit`, `time_limit_ms`, `owner`, and timeout arguments.

- [ ] **Step 4: Implement the fixed gateway projection**

Use `os.scandir` with an explicit stack, `follow_symlinks=False`, `time.monotonic()`, and a scan counter. Encode cursor state as canonical JSON plus an HMAC derived from the configured gateway token; when no token exists, derive an in-process random cursor key at server start. Bind owner, canonical root, normalized query, filters, and remaining traversal stack into the signed cursor.

- [ ] **Step 5: Run projection and executor tests**

Run: `uv run pytest -q tests/test_command_gateway.py tests/test_file_ops_executor.py`

Expected: matching, filtering, budget, cursor binding, symlink, and root authorization tests pass.

- [ ] **Step 6: Commit the search transport**

```bash
git add src/pilot107/adapters/slurm.py simulator/compose/scripts/command-gateway.py \
  tests/test_command_gateway.py tests/test_file_ops_executor.py
git commit -m "feat: add bounded file search projection"
```

### Task 4: Expose owner-scoped file search over HTTP

**Files:**
- Modify: `src/pilot107/api/file_routes.py`
- Modify: `tests/test_file_upload_api.py`
- Modify: `apps/web/src/types.ts`
- Modify: `apps/web/src/api.ts`
- Modify: `apps/web/src/api.test.ts`
- Modify: `src/pilot107/api/asgi_app.py`
- Modify: `tests/snapshots/openapi_phase3b.json`

**Interfaces:**
- Produces: `GET /api/v1/files/search` and TypeScript `FileSearchResponse`.
- Error contract: invalid query 400, path forbidden 403, missing root 404, transport failure 502.

- [ ] **Step 1: Add failing route tests**

```python
def test_search_route_returns_bounded_owner_scoped_page(self) -> None:
    response = self.api.handle_get(
        "/api/v1/files/search?root=/public/home/alice&q=model&kind=file&limit=20",
        headers=self._headers("alice"),
    )
    self.assertEqual(response.status, 200)
    self.assertEqual(response.payload["root"], "/public/home/alice")
    self.assertLessEqual(len(response.payload["items"]), 20)

def test_search_route_rejects_limit_over_one_hundred(self) -> None:
    response = self.api.handle_get(
        "/api/v1/files/search?root=/public/home/alice&q=x&limit=101",
        headers=self._headers("alice"),
    )
    self.assertEqual(response.status, 400)
```

- [ ] **Step 2: Run route tests and see the current 404**

Run: `uv run pytest -q tests/test_file_upload_api.py -k search_route`

Expected: the search request returns 404 before implementation.

- [ ] **Step 3: Implement strict query parsing and error mapping**

Add the `search` branch before `content` in `FileRoutes.handle_get`. Parse timestamps as timezone-aware ISO 8601, sizes as non-negative integers, `kind` from `file|directory|all`, and `limit` within `1..100`. Pass server-owned defaults `scan_limit=10000` and `time_limit_ms=750` to the executor; never accept these budgets from the browser.

- [ ] **Step 4: Add frontend wire types and API client**

```typescript
export interface FileSearchResponse {
  root: string;
  items: FileEntry[];
  incomplete: boolean;
  next_cursor: string | null;
  warnings: string[];
}

fileSearch: (user, input, signal) => getJson<FileSearchResponse>(
  queryPath("/api/v1/files/search", {
    root: input.root, q: input.q, kind: input.kind,
    limit: String(input.limit), ...(input.cursor ? { cursor: input.cursor } : {}),
  }), user, signal,
)
```

- [ ] **Step 5: Register the OpenAPI route and refresh the checked snapshot**

Register `/api/v1/files/search` as GET with root/q/kind/size/mtime/limit/cursor query parameters, then regenerate the project snapshot using the existing snapshot test command.

- [ ] **Step 6: Run backend and web API tests**

Run: `uv run pytest -q tests/test_file_upload_api.py tests/test_asgi_app.py && npm test -- --run apps/web/src/api.test.ts`

Expected: route, OpenAPI snapshot, query encoding, and response typing pass.

- [ ] **Step 7: Commit the HTTP search contract**

```bash
git add src/pilot107/api/file_routes.py src/pilot107/api/asgi_app.py \
  tests/test_file_upload_api.py tests/snapshots/openapi_phase3b.json \
  apps/web/src/types.ts apps/web/src/api.ts apps/web/src/api.test.ts
git commit -m "feat: expose authorized file search"
```

### Task 5: Add an editable path bar to each file pane

**Files:**
- Create: `apps/web/src/files/PathBar.tsx`
- Create: `apps/web/src/files/PathBar.test.tsx`
- Modify: `apps/web/src/files/selection.ts`
- Modify: `apps/web/src/files/selection.test.ts`
- Modify: `apps/web/src/files/useFilePane.ts`
- Modify: `apps/web/src/files/FilePane.tsx`
- Modify: `apps/web/src/styles.css`

**Interfaces:**
- Produces: `resolvePanePath(input: string, cwd: string, home: string): string` and `PathBar` with `cwd`, `home`, and `onNavigate` props.
- Consumes: existing `navigateTo` so successful manual navigation participates in back/forward history.

- [ ] **Step 1: Add failing path resolution tests**

```typescript
it("resolves absolute and relative paths inside home", () => {
  expect(resolvePanePath("/public/home/alice/project", "/public/home/alice", "/public/home/alice"))
    .toBe("/public/home/alice/project");
  expect(resolvePanePath("../data", "/public/home/alice/project", "/public/home/alice"))
    .toBe("/public/home/alice/data");
});

it("rejects paths escaping the owner root", () => {
  expect(() => resolvePanePath("../../bob", "/public/home/alice/project", "/public/home/alice"))
    .toThrow("路径超出授权目录");
});
```

- [ ] **Step 2: Run the test and verify `resolvePanePath` is absent**

Run: `npm test -- --run apps/web/src/files/selection.test.ts apps/web/src/files/PathBar.test.tsx`

Expected: import/export failure for the new function/component.

- [ ] **Step 3: Implement lexical normalization without broadening roots**

Normalize slash-separated POSIX segments in TypeScript; reject NUL, empty resolved paths, and any normalized result for which `clampToHome(result, home) !== result`. Do not call `decodeURIComponent` on user input.

- [ ] **Step 4: Implement edit, Enter, Escape, and error states**

`PathBar` renders breadcrumbs when idle and a labelled text input while editing. Enter calls `onNavigate(resolved)`; Escape restores `cwd`; a rejected path displays `role="alert"`. Keep the input open while the target listing is pending or has an error so the user can correct it.

- [ ] **Step 5: Replace the inline breadcrumb and run component tests**

Run: `npm test -- --run apps/web/src/files/selection.test.ts apps/web/src/files/PathBar.test.tsx && npm run typecheck`

Expected: path behavior tests and TypeScript pass.

- [ ] **Step 6: Commit the path bar**

```bash
git add apps/web/src/files/PathBar.tsx apps/web/src/files/PathBar.test.tsx \
  apps/web/src/files/selection.ts apps/web/src/files/selection.test.ts \
  apps/web/src/files/useFilePane.ts apps/web/src/files/FilePane.tsx apps/web/src/styles.css
git commit -m "feat: add authorized file path bar"
```

### Task 6: Add the file-search user interface

**Files:**
- Create: `apps/web/src/files/FileSearchPanel.tsx`
- Create: `apps/web/src/files/FileSearchPanel.test.tsx`
- Modify: `apps/web/src/FilesPage.tsx`
- Modify: `apps/web/src/files/FilesManagerContext.tsx`
- Modify: `apps/web/src/styles.css`
- Test: `tests/ui/files.spec.js`

**Interfaces:**
- Consumes: `api.fileSearch`, current user, active pane cwd, and manager pane navigation.
- Produces: debounced search UI with filters, incomplete-page continuation, warnings, and open-in-current-pane action.

- [ ] **Step 1: Add failing UI tests**

```typescript
it("does not search until the query has non-whitespace text", async () => {
  render(<FileSearchPanel user="alice" root="/public/home/alice" onOpen={() => undefined} />);
  expect(api.fileSearch).not.toHaveBeenCalled();
});

it("continues an incomplete result page with its opaque cursor", async () => {
  // First response has incomplete=true and next_cursor="cursor-1".
  // Clicking 继续搜索 must call api.fileSearch with cursor="cursor-1".
});
```

- [ ] **Step 2: Run the new test and verify the component is absent**

Run: `npm test -- --run apps/web/src/files/FileSearchPanel.test.tsx`

Expected: module-not-found failure.

- [ ] **Step 3: Implement the focused search panel**

Use a 250-ms debounce, `kind` selector, optional size and modified-time controls, AbortSignal from TanStack Query, and a result list showing relative path, type, size, and modified time. Append only when continuing with the returned cursor; reset results when root/query/filter changes.

- [ ] **Step 4: Wire results to the active pane**

For directory results navigate directly; for file results navigate to `parentPath(entry.path)` and select the file after the listing resolves. Add `FilesManager.openPath(path, selectedPath?)` rather than reaching into React component state.

- [ ] **Step 5: Add Playwright coverage for path entry and search**

The browser test creates `/public/home/alice/demo-search/nested/result-model.txt`, enters `/public/home/alice/demo-search` in the path bar, searches for `model`, opens the result, and asserts the active pane cwd plus selected filename.

- [ ] **Step 6: Run web tests, typecheck, and UI test**

Run: `npm test -- --run apps/web/src/files/FileSearchPanel.test.tsx apps/web/src/files/PathBar.test.tsx && npm run typecheck && npx playwright test tests/ui/files.spec.js`

Expected: all commands exit 0.

- [ ] **Step 7: Commit the search UI**

```bash
git add apps/web/src/files/FileSearchPanel.tsx apps/web/src/files/FileSearchPanel.test.tsx \
  apps/web/src/FilesPage.tsx apps/web/src/files/FilesManagerContext.tsx \
  apps/web/src/styles.css tests/ui/files.spec.js
git commit -m "feat: add file discovery UI"
```

### Task 7: Expose AgentTask status and cancellation

**Files:**
- Create: `src/pilot107/api/agent_task_routes.py`
- Create: `tests/agent/test_agent_task_api.py`
- Modify: `src/pilot107/api/http_app.py`
- Modify: `src/pilot107/api/service.py`
- Modify: `src/pilot107/api/asgi_app.py`
- Modify: `src/pilot107/agent/tasks.py`
- Modify: `tests/agent/test_lifecycle_schemas.py`
- Modify: `apps/web/src/types.ts`
- Modify: `apps/web/src/api.ts`
- Modify: `apps/web/src/api.test.ts`

**Interfaces:**
- Produces: `GET /agent-sessions/{session_id}/tasks`, `GET /agent-tasks/{task_id}`, and `POST /agent-tasks/{task_id}/cancel`.
- Wire payload: existing `agent_task_payload(AgentTaskRecord)`; list response `{items: [...]}`.

- [ ] **Step 1: Add failing owner-bound API tests**

```python
def test_list_tasks_is_scoped_to_session_and_owner(self) -> None:
    response = self.api.handle_get(
        f"/api/v1/agent-sessions/{self.session_id}/tasks",
        headers=self.headers("alice"),
    )
    self.assertEqual(response.status, 200)
    self.assertEqual([item["task_id"] for item in response.payload["items"]], [self.task_id])

def test_cancel_requires_expected_version_and_propagates(self) -> None:
    response = self.api.handle_post(
        f"/api/v1/agent-tasks/{self.task_id}/cancel",
        body=json.dumps({"expected_version": 1}).encode(),
        headers=self.headers("alice"),
    )
    self.assertEqual(response.status, 200)
    self.assertTrue(response.payload["cancel_requested"])
```

Also assert Bob receives 404, stale version receives 409, and a missing task receives 404.

- [ ] **Step 2: Run the API test and verify the routes return 404**

Run: `uv run pytest -q tests/agent/test_agent_task_api.py`

Expected: route-not-found failures.

- [ ] **Step 3: Implement the route adapter and inject the existing service**

`AgentTaskRoutes` accepts `AgentTaskService`; it uses `service.store.list_tasks`, `service.store.get_task`, `service.request_cancel`, and `agent_task_payload`. Pass the API-side `agent_task_service` built in `build_api_service` into `Pilot107HttpApi` instead of leaving it reachable only through the tool handler. Add `cancel_requested` to `agent_task_payload` and freeze it in the lifecycle schema test so the UI never infers cancellation from state text.

- [ ] **Step 4: Register OpenAPI routes and TypeScript API**

Add `AgentTask` matching `pilot107.agent-task/v1`, including resource envelope, linked Run, result, lease, version, and timestamps. Add `agentSessionTasks`, `agentTask`, and `cancelAgentTask` client methods with encoded IDs.

- [ ] **Step 5: Run backend contract, API client, and spec tests**

Run: `uv run pytest -q tests/agent/test_agent_task_api.py tests/agent/test_agent_task_service.py tests/agent/test_lifecycle_schemas.py tests/test_asgi_app.py && npm test -- --run apps/web/src/api.test.ts && npm run typecheck`

Expected: all commands exit 0.

- [ ] **Step 6: Commit the AgentTask API**

```bash
git add src/pilot107/api/agent_task_routes.py src/pilot107/api/http_app.py \
  src/pilot107/api/service.py src/pilot107/api/asgi_app.py src/pilot107/agent/tasks.py \
  tests/agent/test_agent_task_api.py tests/agent/test_lifecycle_schemas.py \
  tests/snapshots/openapi_phase3b.json \
  apps/web/src/types.ts apps/web/src/api.ts apps/web/src/api.test.ts
git commit -m "feat: expose agent task lifecycle"
```

### Task 8: Show AgentTask lifecycle in the Project Agent workspace

**Files:**
- Create: `apps/web/src/AgentTaskPanel.tsx`
- Create: `apps/web/src/AgentTaskPanel.test.tsx`
- Modify: `apps/web/src/AgentProjectPanel.tsx`
- Modify: `apps/web/src/styles.css`

**Interfaces:**
- Consumes: project Agent session ID, `api.agentSessionTasks`, `api.cancelAgentTask`, and existing Run deep links.
- Produces: owner-visible task status, resource request, Slurm job/Run link, evidence references, errors, and cancellation.

- [ ] **Step 1: Write failing component tests**

```typescript
it("renders linked Run, resources, evidence and terminal status", () => {
  render(<AgentTaskPanel user="alice" sessionId="session-1" />);
  expect(screen.getByText("1 CPU · 512 MiB · 0 GPU")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /run-agent-task/ })).toHaveAttribute(
    "href", expect.stringContaining("/runs/run-agent-task"),
  );
  expect(screen.getByText("agent-task:task-1")).toBeInTheDocument();
});
```

Add tests for pending/running polling, cancel button visibility, terminal stop-polling, and `AUTH_REQUIRED` explanation.

- [ ] **Step 2: Run the component test and see module-not-found**

Run: `npm test -- --run apps/web/src/AgentTaskPanel.test.tsx`

Expected: the component does not yet exist.

- [ ] **Step 3: Implement a focused task panel**

Poll every two seconds only while any task is `pending`, `running`, or `cancelling`. Use the task version for cancellation. Render linked Run with the existing app navigation convention and show result `error_code/message` without exposing lease owner internals.

- [ ] **Step 4: Mount the panel only for a bound Project session**

Place it next to Sandbox/formal-run review in `AgentProjectPanel`; pass the session created from the project profile. An absent session renders no empty shell.

- [ ] **Step 5: Run focused and full web validation**

Run: `npm test -- --run apps/web/src/AgentTaskPanel.test.tsx apps/web/src/AgentProjectPanel.test.tsx && npm run typecheck && npm run build`

Expected: tests, typecheck, and Vite build pass.

- [ ] **Step 6: Commit the task UI**

```bash
git add apps/web/src/AgentTaskPanel.tsx apps/web/src/AgentTaskPanel.test.tsx \
  apps/web/src/AgentProjectPanel.tsx apps/web/src/styles.css
git commit -m "feat: show agent Slurm validation tasks"
```

### Task 9: Close the deterministic AgentTask-to-Slurm acceptance chain

**Files:**
- Create: `scripts/smoke-vm-agent-task.py`
- Create: `scripts/smoke-vm-agent-task.sh`
- Create: `tests/agent/test_vm_agent_task_smoke_contract.py`
- Modify: `scripts/accept-runtime-bundle.sh`
- Modify: `scripts/export-cpu-rc-bundle.sh`

**Interfaces:**
- Produces: a smoke report containing project/workspace/change set/Sandbox/session/turn/task/Run/job/Evidence/Capsule/follow-up identifiers.
- Consumes: public HTTP APIs plus read-only container/Slurm inspection for revision and scheduler assertions.

- [ ] **Step 1: Add a failing smoke contract test**

```python
def test_vm_agent_task_smoke_requires_every_lifecycle_checkpoint() -> None:
    source = (ROOT / "scripts/smoke-vm-agent-task.py").read_text()
    for token in (
        "sandbox_succeeded", "task_id", "linked_run_id", "job_id",
        "evidence_refs", "capsule_state", "followup_turn_id",
    ):
        assert token in source
```

- [ ] **Step 2: Run the contract test and verify the smoke is missing**

Run: `uv run pytest -q tests/agent/test_vm_agent_task_smoke_contract.py`

Expected: missing-file failure.

- [ ] **Step 3: Implement the HTTP-driven smoke**

The script creates an existing-directory Project with a tiny Python file, creates a reviewable ChangeSet, calls the Sandbox route with `("python", "-m", "py_compile", "main.py")`, creates an `experiment_builder` session with a 1-CPU/512-MiB/0-GPU/300-second envelope, submits a turn requesting one validation, and polls session tasks. It asserts the linked Run has a numeric VM Slurm job ID, reaches `SUCCEEDED`, has Evidence and ready Capsule, then asserts the ready outbox created a follow-up turn/event.

Use an explicit request key per object and print one JSON document. Never read the LLM API key or SSH credentials.

- [ ] **Step 4: Make the smoke deterministic in CI and live on the VM**

For automated acceptance, inject a fixed test `AgentdClient` event sequence that invokes `validation_schedule` with the exact resource arguments. For VM acceptance, use the deployed `campus-default` Agentd model and the same constrained prompt, retaining the task/result assertions as the authority.

- [ ] **Step 5: Add the smoke to runtime acceptance and bundle contents**

Run it after the base web smoke and image binding check. A missing Agentd, Sandbox failure, no task, non-Slurm Run, missing Evidence/Capsule, or absent follow-up is a hard runtime acceptance failure.

- [ ] **Step 6: Run the local lifecycle chain**

Run: `uv run pytest -q tests/agent/test_vm_agent_task_smoke_contract.py tests/agent/test_a3_vertical.py tests/agent/test_agent_task_service.py && PILOT107_PUBLIC_URL=https://127.0.0.1:8443 bash scripts/smoke-vm-agent-task.sh`

Expected: final JSON contains `status: "ok"` and all required identifiers.

- [ ] **Step 7: Commit the acceptance chain**

```bash
git add scripts/smoke-vm-agent-task.py scripts/smoke-vm-agent-task.sh \
  tests/agent/test_vm_agent_task_smoke_contract.py scripts/accept-runtime-bundle.sh \
  scripts/export-cpu-rc-bundle.sh
git commit -m "test: close VM agent task lifecycle"
```

### Task 10: Fix CPU-RC release completeness and systemd installation

**Files:**
- Create: `tests/test_cpu_rc_release_tooling.py`
- Modify: `scripts/build-cpu-rc-images.sh`
- Modify: `scripts/export-cpu-rc-bundle.sh`
- Modify: `scripts/import-cpu-rc-images.sh`
- Modify: `scripts/verify-cpu-rc-image-binding.sh`
- Modify: `scripts/accept-runtime-bundle.sh`
- Modify: `scripts/install-systemd-units.sh`

**Interfaces:**
- Produces: five revision-tagged release images (`slurm-sim`, `api`, `worker`, `web`, `agentd`) covering 11 compose services.
- Produces: systemd installer that exits 0 and always removes its generated temporary unit.

- [ ] **Step 1: Add failing release-tool tests**

```python
def test_cpu_rc_bundle_includes_agentd_in_every_binding_stage() -> None:
    for path in (
        "scripts/build-cpu-rc-images.sh", "scripts/export-cpu-rc-bundle.sh",
        "scripts/import-cpu-rc-images.sh", "scripts/verify-cpu-rc-image-binding.sh",
    ):
        assert "pilot107/agentd:cpu-rc-$" in (ROOT / path).read_text()

def test_cpu_rc_binding_requires_all_eleven_services() -> None:
    script = (ROOT / "scripts/verify-cpu-rc-image-binding.sh").read_text()
    assert "pilot-agentd" in script

def test_systemd_installer_exit_trap_expands_the_temp_path() -> None:
    script = (ROOT / "scripts/install-systemd-units.sh").read_text()
    assert "trap 'rm -f \"$tmp\"' EXIT" not in script
```

- [ ] **Step 2: Run tests and observe agentd/trap failures**

Run: `uv run pytest -q tests/test_cpu_rc_release_tooling.py`

Expected: all three tests fail against the current tooling.

- [ ] **Step 3: Tag, export, manifest, import, and bind Agentd**

Build `pilot107/agentd:cpu-rc-$revision`, add it to `images[]`, `images.txt`, Docker save, SBOM metadata, required-file assertions, and runtime binding. Map compose service `pilot-agentd` to that manifest record and change expected services from 10 to 11.

- [ ] **Step 4: Fix the installer trap at its source**

Replace the function-local EXIT trap with cleanup that captures a shell-escaped concrete path while it is in scope:

```bash
local tmp
tmp="$(mktemp)"
local cleanup_command
printf -v cleanup_command 'rm -f -- %q' "$tmp"
trap "$cleanup_command" EXIT
```

After installing the unit, remove the temporary file and clear the trap with `trap - EXIT`, so a successful function return cannot reference an unset local.

- [ ] **Step 5: Run tooling and shell validation**

Run the focused test and explicit syntax checks on the actual filenames:

```bash
uv run pytest -q tests/test_cpu_rc_release_tooling.py
bash -n scripts/build-cpu-rc-images.sh scripts/export-cpu-rc-bundle.sh \
  scripts/import-cpu-rc-images.sh scripts/verify-cpu-rc-image-binding.sh \
  scripts/accept-runtime-bundle.sh scripts/install-systemd-units.sh
```

Expected: pytest and both syntax-check commands exit 0.

- [ ] **Step 6: Build one bundle and verify five manifest images plus 11 services**

Run:

```bash
bash scripts/export-cpu-rc-bundle.sh
bundle_name="$(cat artifacts/deployment/LATEST_CPU_RC.txt)"
jq '.images | length' "artifacts/deployment/$bundle_name/RELEASE_MANIFEST.json"
```

Expected: manifest image count is `5`; runtime image-binding acceptance reports 11 services.

- [ ] **Step 7: Commit release tooling fixes**

```bash
git add tests/test_cpu_rc_release_tooling.py scripts/build-cpu-rc-images.sh \
  scripts/export-cpu-rc-bundle.sh scripts/import-cpu-rc-images.sh \
  scripts/verify-cpu-rc-image-binding.sh scripts/accept-runtime-bundle.sh \
  scripts/install-systemd-units.sh
git commit -m "fix: make CPU RC releases deployment complete"
```

### Task 11: Run the full release gate and deploy the fixed revision

**Files:**
- Modify: `docs/phase-3/s1_vm_deployment_evidence_20260718.md`
- Modify: `docs/phase-3/current_status_index.md`
- Create: `artifacts/acceptance/vm-demo/$revision/deployment-summary.json`

**Interfaces:**
- Consumes: clean tracked worktree and the release bundle from Task 10.
- Produces: one deployed revision, same-SHA source/runtime evidence, external HTTP evidence, and a rollback pointer to the previous release.

- [ ] **Step 1: Run focused backend and web suites**

Run:

```bash
uv run pytest -q \
  tests/test_cpu_rc_profile.py tests/agent/test_workspace_sandbox.py \
  tests/test_command_gateway.py tests/test_file_ops_executor.py \
  tests/test_file_upload_api.py tests/agent/test_agent_task_api.py \
  tests/agent/test_agent_task_service.py tests/agent/test_vm_agent_task_smoke_contract.py \
  tests/test_cpu_rc_release_tooling.py
npm test -- --run
npm run typecheck
npm run build
```

Expected: zero failures.

- [ ] **Step 2: Run the repository release gates on one commit**

Run: `bash scripts/accept-source-release.sh && bash scripts/accept-cpu-rc-release.sh`

Expected: source and CPU-RC release gates pass on the same full Git SHA.

- [ ] **Step 3: Export and checksum the immutable bundle**

Run:

```bash
bash scripts/export-cpu-rc-bundle.sh
bundle_name="$(cat artifacts/deployment/LATEST_CPU_RC.txt)"
sha256sum -c "artifacts/deployment/$bundle_name.tar.gz.sha256"
```

Expected: checksum output is `OK` and the bundle manifest revision equals `git rev-parse HEAD`.

- [ ] **Step 4: Upload without using the desktop key agent**

Resolve `bundle_name` from `LATEST_CPU_RC.txt`, then use password authentication with `IdentityAgent=none` to upload `artifacts/deployment/$bundle_name.tar.gz` and its sidecar to `root@114.214.241.31` port `8000`. Drive the password prompt through an in-memory `pexpect` process; do not put the password in a file, repository, shell history, or deployment log.

- [ ] **Step 5: Import before the short switch window**

On the VM, derive `release_name` from the uploaded archive basename, verify the uploaded SHA256, extract to `/root/$release_name`, import all five images, and inspect every image reference. Copy the old `.env.cpu-rc`, `certs`, `secrets`, and Slurm JWT key into the new release; update only revision-tagged image references and public URL.

- [ ] **Step 6: Switch the stack without deleting volumes**

Stop with `scripts/stop-cpu-rc.sh` and no `-v`; install the new systemd unit; set `/etc/pilot107/cpu-rc.env` to the new absolute env path, `PILOT107_PUBLIC_URL=https://114.214.241.31:8443`, and `PILOT107_SKIP_BUILD=1`; start the service and require enabled/active.

- [ ] **Step 7: Run VM acceptance**

Run on the VM:

```bash
cd "/root/$release_name"
bash scripts/check-cpu-rc.sh
bash scripts/smoke-vm-agent-task.sh
bash scripts/smoke-restart-volume-recovery.sh
```

Then verify `scontrol show node anode16` reports `CPUTot=6 RealMemory=10240`, all 11 compose services run, all healthchecks are healthy/none as declared, and every running image matches the release manifest.

- [ ] **Step 8: Run external browser/API acceptance**

From the workstation, require HTTP 200 for `/` and `/healthz`; use the file page to enter a path and search; use the Agent Project panel to observe Sandbox and AgentTask/Run/Evidence completion. Capture no secrets in screenshots or logs.

- [ ] **Step 9: Record evidence and commit status documentation**

Write the deployed SHA, bundle checksum, previous release path, Slurm capacity, test counts, smoke identifiers, external statuses, limitations, and rollback command into the evidence document and JSON summary.

```bash
git add docs/phase-3/s1_vm_deployment_evidence_20260718.md \
  docs/phase-3/current_status_index.md
git commit -m "docs: record VM demo closure deployment"
```

The JSON artifact remains untracked acceptance evidence unless the repository's artifact policy explicitly requires tracking it.

## Plan Self-Review

- Spec sections 2 through 9 map respectively to Tasks 1, 2, 5, 3-6, 7-9, 10, and 11.
- The plan does not add external SSH/MFA, GPU, multi-user production claims, content search, or arbitrary shell.
- Search types use `FileSearchEntry`/`FileSearchPage` consistently from gateway through HTTP and TypeScript.
- AgentTask wire data always uses the existing `agent_task_payload`; API/UI do not invent a second task schema.
- Release identity includes five image records and 11 running compose services.
- Every production change starts with a focused failing test and includes a green verification command before commit.
