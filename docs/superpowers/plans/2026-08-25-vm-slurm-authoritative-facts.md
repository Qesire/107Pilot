# VM Slurm Authoritative Facts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the VM-local Slurm CLI the single authoritative resource-fact source for CPU-RC, retain the last healthy fact set when a later collection degrades, and prove that Dashboard, Preflight, and Agent read the same snapshot identity.

**Architecture:** The command gateway remains the narrow execution boundary and gains only the exact read-only argv used by `ExecutorPlatformCliCollector`. CPU-RC writes canonical records with `source_name="vm-slurm"`; a store-level `latest_usable` selector prevents partial/failed collections from shadowing the last healthy record. REST collection remains diagnostic and must never publish the canonical source. All three consumers resolve facts through the same selector and expose the selected snapshot ID, content digest, and authority ID.

**Tech Stack:** Python 3.11, dataclasses, FastAPI service wiring, command-gateway stdlib HTTP server, pytest/unittest, Docker Compose CPU-RC smoke scripts.

## Global Constraints

- Treat the Slurm instance inside the VM deployment as the real scheduling object for this demo; do not synthesize nodes or partitions in the application.
- Use `source_name="vm-slurm"` as the stable authority identifier. Do not add a database column or migration for `connection_id` in this change.
- A usable authoritative snapshot has successful Slurm partition/node probes, at least one parsed partition, and at least one parsed node. Ancillary `conda`, filesystem, or GPU limitations may keep `collection_status == "partial"` without invalidating Slurm capacity. Freshness is evaluated separately; stale healthy facts remain visible with a warning.
- Never let REST, an empty login snapshot, or a degraded refresh replace the selected healthy authority record.
- Keep the gateway allowlist exact. Do not add shell execution, arbitrary `scontrol`, arbitrary `sinfo`, arbitrary `df`, or arbitrary `conda` arguments.
- Each task ends with its own commit. Do not add existing untracked acceptance artifacts.

---

### Task 1: Align the command-gateway allowlist with the CLI collector

**Files:**
- Modify: `simulator/compose/scripts/command-gateway.py`
- Modify: `tests/test_command_gateway.py`

**Interfaces:**

```python
def _validate_command(argv: list[str], config: GatewayConfig, *, user: str | None) -> None:
    ...
```

The gateway must accept exactly these additional observations:

```text
scontrol show part
scontrol show nodes
sinfo -h -o %N|%P|%t|%c|%m|%G|%E
conda env list --json
df -P -h /public /public/home/<request-user>
```

An absent optional `conda` executable must return a command observation with `returncode=127`; it must not turn the gateway request into HTTP 500.

- [ ] **Step 1: Write failing allowlist tests**

Add focused cases to `CommandGatewayTests` that execute every argv from `default_login_snapshot_specs()` and assert that the safe commands reach `subprocess.run`. Keep explicit negative cases for `scontrol show secrets`, `sinfo -R`, `conda run`, and `df /etc`.

```python
def test_allows_exact_platform_snapshot_probes(self) -> None:
    config = gateway.GatewayConfig(
        token=None,
        allowed_roots=["/public", "/public/home/{user}"],
    )
    accepted = (
        ["scontrol", "show", "part"],
        ["scontrol", "show", "nodes"],
        ["sinfo", "-h", "-o", "%N|%P|%t|%c|%m|%G|%E"],
        ["conda", "env", "list", "--json"],
        ["df", "-P", "-h", "/public", "/public/home/alice"],
    )
    with _mock_ownership(), mock.patch.object(gateway.subprocess, "run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        for argv in accepted:
            gateway._run({"argv": argv, "user": "alice"}, config)
    self.assertEqual(run.call_count, len(accepted))
```

Add one `FileNotFoundError` case for `conda` and assert the response object is:

```python
{"returncode": 127, "stdout": "", "stderr": "conda: command not found\n"}
```

- [ ] **Step 2: Run the focused test and confirm the current rejection**

Run:

```bash
python -m pytest tests/test_command_gateway.py -q
```

Expected: the new test fails with `command not allowed: conda` or `command arguments not allowed: scontrol`.

- [ ] **Step 3: Implement exact argv validation**

Add `conda` and `df` to `ALLOWED_COMMANDS`, remove `sinfo` from the single-value `exact_commands` map, then extend `_validate_command` with literal matches. For `df`, resolve the request user, require the second path to equal `/public/home/{user}`, and call `_authorize_path` for both `/public` and the owner root; do not accept caller-supplied alternate roots. Catch `FileNotFoundError` around `subprocess.run` only for the exact optional `conda env list --json` argv and return 127.

```python
if argv[0] == "scontrol" and argv[1:] in (
    ["show", "config"],
    ["show", "part"],
    ["show", "nodes"],
):
    return
if argv[0] == "sinfo" and argv[1:] in (
    ["-h", "-o", "%P|%c|%m|%G|%T"],
    ["-h", "-o", "%N|%P|%t|%c|%m|%G|%E"],
):
    return
if argv[0] == "conda" and argv[1:] == ["env", "list", "--json"]:
    return
if argv[0] == "df" and user is not None and argv[1:] == [
    "-P", "-h", "/public", f"/public/home/{user}",
]:
    return
```

- [ ] **Step 4: Run gateway and collector contract tests**

Run:

```bash
python -m pytest tests/test_command_gateway.py tests/test_platform_parsers.py -q
```

Expected: all tests pass, including negative allowlist tests.

- [ ] **Step 5: Commit the gateway closure**

```bash
git add simulator/compose/scripts/command-gateway.py tests/test_command_gateway.py
git commit -m "fix: allow bounded Slurm fact probes"
```

---

### Task 2: Select the last usable canonical snapshot

**Files:**
- Modify: `src/pilot107/core/platform_snapshot_store.py`
- Modify: `tests/test_platform_snapshot_store.py`

**Interfaces:**

```python
VM_SLURM_SOURCE_NAME = "vm-slurm"

@dataclass(frozen=True)
class AuthoritativeSnapshotSelection:
    record: PlatformSnapshotRecord
    authority_id: str
    warnings: tuple[str, ...]

def latest_usable(
    self,
    *,
    owner: str,
    source_name: str = VM_SLURM_SOURCE_NAME,
    at: datetime | None = None,
) -> AuthoritativeSnapshotSelection | None:
    ...
```

- [ ] **Step 1: Write failing selection tests**

Create three records for one owner: an older VM record with successful Slurm probes and nodes/partitions but an ancillary `conda` limitation, a newer degraded VM record with no resources, and a newest complete REST record. Assert that `latest_usable` selects the older partial-but-Slurm-healthy VM record. Add tests that a stale healthy record is returned with `"stale_authoritative_snapshot"` in `warnings`, an ancillary limitation adds `"partial_ancillary_facts"`, and no Slurm-healthy record returns `None`.

```python
selection = store.latest_usable(owner="alice", at=now)
assert selection is not None
assert selection.record.snapshot_id == "platform-healthy"
assert selection.authority_id == "vm-slurm"
assert selection.warnings == (
    "partial_ancillary_facts",
)
```

- [ ] **Step 2: Run the store test and observe the missing selector**

Run:

```bash
python -m pytest tests/test_platform_snapshot_store.py -q
```

Expected: failure because `latest_usable` and `AuthoritativeSnapshotSelection` do not exist.

- [ ] **Step 3: Implement the minimal selector without schema changes**

Query the store's owner-scoped records by `source_name` in descending `captured_at` order and select the first record that passes `_has_healthy_slurm_capacity`. The predicate requires non-empty `payload["partitions"]`/`payload["nodes"]`, successful `scontrol_show_part`, and successful `scontrol_show_nodes` or `sinfo_pipe`. Do not reject an otherwise healthy record because `conda_env_list_json`, `df_public_home`, or GPU runtime is unavailable. Derive warnings from `record.collection_status` and `record.freshness(at=at)`.

```python
def _is_usable_vm_snapshot(record: PlatformSnapshotRecord) -> bool:
    results = {
        item.get("name"): item
        for item in record.payload.get("command_results", [])
        if isinstance(item, dict)
    }
    part_ok = results.get("scontrol_show_part", {}).get("returncode") == 0
    node_ok = any(
        results.get(name, {}).get("returncode") == 0
        for name in ("scontrol_show_nodes", "sinfo_pipe")
    )
    return (
        record.source_name == VM_SLURM_SOURCE_NAME
        and part_ok
        and node_ok
        and bool(record.payload.get("partitions"))
        and bool(record.payload.get("nodes"))
    )
```

Do not change `latest()` because diagnostic callers may still need the most recent raw observation.

- [ ] **Step 4: Run store and snapshot-service tests**

Run:

```bash
python -m pytest \
  tests/test_platform_snapshot_store.py \
  tests/test_platform_snapshot_service.py \
  tests/test_platform_parsers.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit the authoritative selector**

```bash
git add src/pilot107/core/platform_snapshot_store.py tests/test_platform_snapshot_store.py
git commit -m "feat: retain last usable VM Slurm snapshot"
```

---

### Task 3: Publish only CLI facts as the CPU-RC authority

**Files:**
- Modify: `src/pilot107/api/service.py`
- Modify: `tests/test_api_service.py`
- Modify: `tests/api/test_service_snapshot_wiring.py`

**Interfaces:**

```python
def _platform_snapshot_source_name(*, cluster_backend: str) -> str:
    return "vm-slurm" if cluster_backend == "command-gateway" else "cli"
```

- [ ] **Step 1: Write failing service-wiring tests**

Add a CPU-RC service test that captures records stored by the background collector and asserts:

```python
assert stored.source_name == "vm-slurm"
assert stored.source_type == "cli"
assert rest_collector.collect.call_count == 0
```

Add a non-command-gateway case that preserves existing behavior. If `tests/api/test_service_snapshot_wiring.py` does not already construct the service with a fake backend, add the fixture there rather than mocking private module globals.

- [ ] **Step 2: Run the focused wiring tests**

Run:

```bash
python -m pytest tests/test_api_service.py tests/api/test_service_snapshot_wiring.py -q
```

Expected: the command-gateway case fails because the stored source is `command-gateway-auto` and the REST collector is still scheduled.

- [ ] **Step 3: Make CPU-RC CLI authoritative and REST diagnostic-only**

In service construction, branch on the resolved cluster backend. For `command-gateway`, schedule only the CLI collector for canonical platform refresh and store it as `vm-slurm`. Do not attempt REST collection in the authority refresh loop. Retain REST collector construction for deployments that explicitly use it, and label any separately persisted REST record with a non-canonical source name.

```python
if settings.cluster_backend == "command-gateway":
    platform_snapshot_collector = ExecutorPlatformCliCollector(...)
    platform_snapshot_source_name = VM_SLURM_SOURCE_NAME
else:
    platform_snapshot_collector = rest_or_cli_collector
```

- [ ] **Step 4: Run service and collector integration tests**

Run:

```bash
python -m pytest \
  tests/test_api_service.py \
  tests/api/test_service_snapshot_wiring.py \
  tests/test_platform_snapshot_service.py -q
```

Expected: all pass; the command-gateway fixture stores one canonical CLI record.

- [ ] **Step 5: Commit CPU-RC authority wiring**

```bash
git add src/pilot107/api/service.py tests/test_api_service.py tests/api/test_service_snapshot_wiring.py
git commit -m "fix: make VM CLI the CPU-RC fact authority"
```

---

### Task 4: Bind Dashboard, Preflight, and Agent to one snapshot identity

**Files:**
- Modify: `src/pilot107/api/http_app.py`
- Modify: `src/pilot107/core/platform_preflight.py`
- Modify: `src/pilot107/agent/read_tools.py`
- Modify: `tests/test_read_models.py`
- Modify: `tests/agent/test_read_tools.py`
- Modify: `tests/test_preflight.py`

**Interfaces:**

Every consumer summary must include:

```json
{
  "authority_id": "vm-slurm",
  "snapshot_id": "platform-...",
  "content_sha256": "...",
  "warnings": []
}
```

- [ ] **Step 1: Write a cross-consumer failing test**

Seed an older healthy canonical record followed by a degraded record. Read the HTTP latest snapshot response, run Preflight, and call `platform_get_snapshot`; assert that the three payloads contain the same `snapshot_id` and `content_sha256`.

```python
ids = {
    dashboard.payload["snapshot_id"],
    preflight.platform_snapshot_id,
    agent_result.result["snapshot_id"],
}
assert ids == {healthy.snapshot_id}
```

Also assert a clean `platform_facts_unavailable` error when no usable canonical record exists; do not fall back to an empty record.

- [ ] **Step 2: Run the focused tests and confirm consumer divergence**

Run:

```bash
python -m pytest \
  tests/test_read_models.py \
  tests/agent/test_read_tools.py \
  tests/test_preflight.py -q
```

Expected: at least the HTTP or Agent assertion selects the latest degraded record through `latest(owner)`.

- [ ] **Step 3: Route all consumers through `latest_usable`**

Replace consumer calls to `platform_snapshot_store.latest(owner)` when the purpose is current platform facts. Serialize the selection envelope once, using a helper in `http_app.py` or a small shared pure function in `platform_snapshot_store.py`; avoid independently recomputing IDs.

For Preflight, include `snapshot_id` and `content_sha256` in the result/evidence metadata used by formal submission. For Agent, return the selection envelope plus the snapshot body and evidence ref `platform-snapshot:<snapshot_id>`.

- [ ] **Step 4: Run the cross-consumer and existing API tests**

Run:

```bash
python -m pytest \
  tests/test_read_models.py \
  tests/agent/test_read_tools.py \
  tests/test_preflight.py \
  tests/test_platform_snapshot_store.py -q
```

Expected: all pass and the fixture observes one identity across all consumers.

- [ ] **Step 5: Commit consumer convergence**

```bash
git add \
  src/pilot107/api/http_app.py \
  src/pilot107/core/platform_preflight.py \
  src/pilot107/agent/read_tools.py \
  tests/test_read_models.py \
  tests/agent/test_read_tools.py \
  tests/test_preflight.py
git commit -m "fix: converge Slurm fact consumers"
```

---

### Task 5: Add a VM authority smoke and ship it in the CPU-RC bundle

**Files:**
- Create: `scripts/smoke-vm-slurm-authority.py`
- Create: `scripts/smoke-vm-slurm-authority.sh`
- Modify: `scripts/accept-runtime-bundle.sh`
- Modify: `scripts/export-cpu-rc-bundle.sh`
- Modify: `tests/test_cpu_rc_release_tooling.py`

**Interfaces:**

The smoke prints one JSON object and exits non-zero on any invariant failure:

```json
{
  "status": "PASS",
  "authority_id": "vm-slurm",
  "snapshot_id": "platform-...",
  "content_sha256": "...",
  "partition_count": 1,
  "node_count": 1,
  "consumer_ids_equal": true
}
```

- [ ] **Step 1: Write failing release-tooling contract tests**

Assert that export includes both new scripts and that runtime acceptance runs `vm_slurm_authority` before `agent_task_lifecycle`. Assert that seal mode treats a failure as `FAIL`, never `KNOWN_SKIP`.

- [ ] **Step 2: Run the release-tooling test**

Run:

```bash
python -m pytest tests/test_cpu_rc_release_tooling.py -q
```

Expected: failures name the missing scripts and acceptance step.

- [ ] **Step 3: Implement the read-only HTTP smoke**

Use the public API and `X-Pilot107-User`; do not SSH into containers. Fetch the latest platform endpoint, the capability/preflight endpoint used by the UI, and run one read-only Agent Turn asking for platform facts. Poll durable events, extract the tool result, and compare the three identities. Require at least one partition and one node.

The wrapper follows existing VM smoke environment conventions:

```bash
if [[ -z "${PILOT107_COMPETITION_BASE_URL:-}" ]]; then
  export PILOT107_COMPETITION_BASE_URL="${PILOT107_PUBLIC_URL%/}/api/v1"
fi
python3 "$root/scripts/smoke-vm-slurm-authority.py" "$@"
```

- [ ] **Step 4: Wire export and runtime acceptance**

Add both files to the explicit export list and required-files assertion. Add `step_vm_slurm_authority` to `accept-runtime-bundle.sh` immediately after `check_cpu_rc`, and record the JSON stdout in that step's evidence log.

- [ ] **Step 5: Run local contract verification**

Run:

```bash
python -m pytest tests/test_cpu_rc_release_tooling.py -q
bash -n scripts/smoke-vm-slurm-authority.sh scripts/accept-runtime-bundle.sh scripts/export-cpu-rc-bundle.sh
python -m compileall -q scripts/smoke-vm-slurm-authority.py
git diff --check
```

Expected: all commands succeed.

- [ ] **Step 6: Commit the authority acceptance gate**

```bash
git add \
  scripts/smoke-vm-slurm-authority.py \
  scripts/smoke-vm-slurm-authority.sh \
  scripts/accept-runtime-bundle.sh \
  scripts/export-cpu-rc-bundle.sh \
  tests/test_cpu_rc_release_tooling.py
git commit -m "test: gate VM Slurm fact authority"
```

---

## Final Verification

- [ ] Run the complete focused Python suite:

```bash
python -m pytest \
  tests/test_command_gateway.py \
  tests/test_platform_parsers.py \
  tests/test_platform_snapshot_service.py \
  tests/test_platform_snapshot_store.py \
  tests/test_api_service.py \
  tests/api/test_service_snapshot_wiring.py \
  tests/test_read_models.py \
  tests/agent/test_read_tools.py \
  tests/test_preflight.py \
  tests/test_cpu_rc_release_tooling.py -q
```

- [ ] Build/deploy through the normal CPU-RC release path, then run:

```bash
PILOT107_PUBLIC_URL=https://114.214.241.31:8443 \
  bash scripts/smoke-vm-slurm-authority.sh
```

Expected: one `PASS` JSON object, at least one partition/node, and identical snapshot identities across consumers.

- [ ] Confirm the live latest record is canonical and healthy; preserve its JSON in the acceptance artifact directory. Do not claim closure from unit tests alone.
