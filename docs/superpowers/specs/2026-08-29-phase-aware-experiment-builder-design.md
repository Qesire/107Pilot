# Phase-aware Experiment Builder orchestration design

- Date: 2026-08-29
- Status: proposed for written review
- Scope: `experiment_builder` Turns only
- Model: USTC-107 `deepseek-v4-flash`
- Deployment target: CPU-RC VM with authoritative `vm-slurm`

## 1. Problem and evidence

The current Builder exposes eight low-level tools and asks the model to drive the
entire workflow:

```text
project_get -> project_blueprint_save -> workspace_patch -> workspace_diff
            -> sandbox_exec -> validation_schedule
```

The 2026-08-29 heat-diffusion Turn proved that the real Pi loop, tool gateway,
Workspace, and Sandbox all work. The model created the Blueprint and four files,
then passed static Sandbox validation. It nevertheless exhausted the 20-step Pi
limit before Slurm handoff because it repeatedly read and rewrote shell parsing
code. Raising the limit would increase latency and cost without fixing this
workflow-design fault.

The existing limits measure different things:

- `PI_STEP_LIMITS.experiment_builder = 20` limits Pi model/tool rounds;
- the capability claim `max_invocations = 32` limits individual gateway calls;
- `max_commands = 8` limits Sandbox commands.

Neither 20 nor 32 is backed by a representative task distribution. They remain
safety ceilings, not a target workflow length.

## 2. Goals

1. Make a normal blank-workspace scientific build complete in two model/tool
   rounds: one context read and one build submission.
2. Make a Sandbox repair require at most two additional rounds per repair.
3. Move diff inspection, Sandbox selection, resource derivation, and validation
   scheduling into deterministic application code.
4. Keep Pi responsible for scientific design, source content, and diagnosis of
   structured validation failures.
5. Preserve capability binding, owner isolation, digest guards, immutable
   Evidence, ChangeSet review, AgentTask durability, and one-submit semantics.
6. Keep the existing 20-step Pi ceiling during the first release. Do not raise it
   to 32.

## 3. Non-goals

- Replacing `pi-agent-core` or creating a permanent OS process per Agent.
- Changing read-only, remediation, market, or template-publication profiles.
- Allowing the model to submit formal Runs or publish ChangeSets.
- Adding free-form shell, network access, package installation, or new cluster
  credentials.
- Treating Slurm success as proof of scientific validity.
- Building a general-purpose workflow engine in this iteration.

## 4. Options considered

### 4.1 Prompt and budget tuning only

Keep all eight tools, add stronger instructions, and increase the step limit.
This is small but leaves orchestration probabilistic and repeats the failure mode
already observed. Rejected.

### 4.2 Phase-aware Builder facade over existing primitives

Expose two semantic tools to `experiment_builder`, while retaining the existing
Project, Workspace, Sandbox, and AgentTask services underneath. The facade
derives and executes safe workflow steps deterministically. Adopted.

### 4.3 New persistent workflow engine

Create a separate background workflow aggregate for every build phase. This
offers the strongest crash recovery but duplicates much of AgentTask and expands
the schema and operations surface. Deferred until production evidence shows the
facade cannot provide sufficient recovery.

## 5. Architecture

The Builder-facing catalog becomes:

```text
builder_context_get
builder_build_submit
```

The existing low-level tools remain internal service primitives and remain
available to other profiles where their contracts are still appropriate.
`experiment_builder` no longer sees `project_get`, `workspace_list`,
`project_blueprint_save`, `workspace_diff`, `sandbox_exec`, or
`validation_schedule` directly.

```text
Pi Agent
  | 1. builder_context_get
  v
compact bound context + phase + file digests + resource envelope
  | 2. builder_build_submit(Blueprint + atomic patches)
  v
BuilderWorkflowService
  |- verify bound phase and optimistic versions
  |- save typed Blueprint
  |- apply digest-guarded patch batch
  |- persist and policy-check ChangeSet diff
  |- run the Blueprint's Sandbox validation
  |- on failure: return compact repair diagnostics
  `- on success: derive and schedule one durable AgentTask
                                  |
                                  v
                      authoritative vm-slurm Run
```

The facade is orchestration, not a bypass. Each internal operation continues to
use the existing domain service and produces its existing durable records.

## 6. Tool contracts

### 6.1 `builder_context_get`

Arguments are an empty closed object. Project and Workspace IDs are injected
from the Turn binding and never exposed as model-selected parameters.

The compact result contains:

```json
{
  "phase": "drafting",
  "project": {
    "version": 1,
    "goal": "...",
    "blueprint": null
  },
  "workspace": {
    "snapshot_digest": "...",
    "files": [
      {"path": "src/main.c", "sha256": "...", "size_bytes": 1200}
    ]
  },
  "resource_envelope": {
    "partition": "CPU-RC",
    "qos": "qos_cpu_rc",
    "cpus": 4,
    "memory_mib": 4096,
    "gpus": 0,
    "walltime_seconds": 600
  },
  "next_action": "builder_build_submit"
}
```

It excludes historical full ChangeSets, full diffs, local host paths, tokens,
and redundant owner metadata. Existing file contents are not injected
automatically. For this first slice, blank workspaces are the supported Builder
entry point; source-import and repair profiles keep their read tools.

### 6.2 `builder_build_submit`

The closed argument object contains:

- `request_key`: stable model-provided idempotency key;
- `expected_project_version`;
- `expected_workspace_snapshot_digest`;
- `blueprint`: the existing typed `ProjectBlueprint` schema;
- `patches`: the existing atomic, digest-guarded patch array;
- `base_change_set_id`: null for the first build, latest failed ChangeSet ID for
  a repair.

The model does not resend Sandbox argv, Slurm resources, job count, submission
count, or an arbitrary validation script. The service derives them from the
approved resource envelope and Blueprint:

- Sandbox argv is the single Blueprint validation whose execution is
  `sandbox`;
- Slurm entrypoint is the single Blueprint validation whose execution is
  `slurm`;
- the entrypoint file is read from the just-created Workspace and supplied to
  AgentTask as the bounded validation script;
- CPU, memory, GPU, walltime, tasks, and submissions are the intersection of
  Blueprint hints and the approved envelope, failing closed on any excess.

### 6.3 Results and termination

On Sandbox failure:

```json
{
  "status": "repair_required",
  "phase": "sandbox_failed",
  "change_set_id": "changeset-...",
  "diff_sha256": "...",
  "diagnostics": {
    "exit_code": 1,
    "stdout_tail": "...",
    "stderr_tail": "...",
    "truncated": false
  },
  "current_files": [
    {"path": "scripts/run.sh", "sha256": "...", "size_bytes": 900}
  ],
  "next_action": "builder_build_submit"
}
```

The result is non-terminating so Pi can submit a digest-guarded repair. Output
tails are byte bounded and secret-redacted.

On Sandbox success, the service schedules exactly one durable validation and
returns a terminating receipt:

```json
{
  "status": "scheduled",
  "phase": "validation_scheduled",
  "change_set_id": "changeset-...",
  "diff_sha256": "...",
  "sandbox_result_id": "sandbox-...",
  "task_id": "task-...",
  "linked_run_id": null,
  "next_action": null
}
```

The Pi Turn stops immediately. AgentTask, Run, Evidence, and Capsule progress
remain asynchronous and durable.

## 7. Phase and no-progress policy

`BuilderWorkflowService` derives phase from durable Project, ChangeSet,
Sandbox, and AgentTask records; phase is not trusted from model input.

Allowed transitions:

```text
drafting -> sandbox_failed -> sandbox_failed
drafting -> validation_scheduled
sandbox_failed -> validation_scheduled
```

Rejected transitions include:

- context reads after a build submission when the returned receipt already
  contains the current phase;
- repair against anything except the latest failed ChangeSet;
- resubmitting identical content under a different request key;
- scheduling when a validation AgentTask already exists;
- any operation after `validation_scheduled`.

The prompt targets one context read followed by one initial build, but the
facade does not impose a small semantic call budget. Progressive repairs remain
valid while each patch changes content and continues from the latest failed
ChangeSet. The existing 20-step Pi ceiling and 32-invocation gateway ceiling
remain the emergency safeguards for pathological loops.

## 8. Consistency and idempotency

The gateway's existing invocation reservation remains the first idempotency
layer. `builder_build_submit.request_key` is the workflow idempotency layer and
is bound to owner, Session, Turn, Project, Workspace snapshot, Blueprint digest,
and patch digest.

Replaying the same key and content returns the persisted receipt. Reusing the
key with different content fails with a stable conflict. A process failure after
Workspace mutation but before scheduling is recovered by deriving phase from the
persisted Blueprint, ChangeSet, and Sandbox result; the next invocation resumes
from the first missing deterministic operation instead of applying files again.

No filesystem rollback is attempted after a successfully persisted ChangeSet.
The ChangeSet is the recovery boundary. Failed patch batches continue to use the
existing all-or-nothing filesystem rollback.

## 9. Context and output-size policy

- Context results contain manifest metadata, not full historical payloads.
- Diff policy runs server-side; Pi receives only the diff digest, file summary,
  and policy findings.
- Sandbox output is capped to diagnostic tails for the model while the complete
  bounded result remains persisted as Evidence.
- Every result includes `phase` and `next_action`.
- Tool errors retain stable machine codes and never collapse to `{}`.
- Assistant narration remains suppressed; only tool events and the terminal
  receipt are rendered during Builder execution.

## 10. Security and authority

- The worker injects bound Project and Workspace IDs.
- Capability claims add only the two facade tool names for
  `experiment_builder`; the model cannot select low-level operations.
- The facade calls existing services under the same owner binding.
- Resource values come from the approved Session envelope and authoritative
  `vm-slurm` preflight, never from model claims alone.
- Sandbox remains bubblewrap-backed, argv-only, network-disabled, credential
  cleared, time-bounded, and output-bounded.
- Only validation AgentTask scheduling occurs automatically. Publishing the
  ChangeSet and submitting a formal Run remain explicit user-approved actions.

## 11. Metrics and evidence-based limits

Add per-Turn metrics and terminal metadata for:

- Pi steps;
- provider calls;
- tool invocations by name and outcome;
- build and repair submission counts;
- repeated/no-progress rejection counts;
- time to Sandbox receipt and time to AgentTask handoff;
- terminal phase and failure code.

The first statistical review occurs after at least 30 Builder Turns spanning
small, medium, and scientific tasks. Report p50, p90, and p95. The initial
acceptance targets are:

- blank successful build: at most 2 tool invocations and 3 Pi steps;
- one-repair build: at most 3 tool invocations and 5 Pi steps;
- p95 successful Builder: at most 8 Pi steps;
- zero duplicate validation AgentTasks;
- zero formal Run submissions without user approval.

Only after this dataset exists may the 20-step and 32-invocation ceilings be
revised.

## 12. Testing

### 12.1 Unit and contract tests

- Builder exposes exactly the two facade tools; other profiles remain unchanged.
- Tool schemas are closed and omit Project/Workspace IDs and raw scheduler
  fields.
- Context is compact, bound, and phase-aware.
- Initial submission persists Blueprint, ChangeSet, diff digest, and Sandbox.
- Sandbox success schedules exactly one AgentTask and terminates Pi.
- Sandbox failure returns repair diagnostics and does not schedule.
- Repair requires the latest failed ChangeSet and current file digests.
- Request replay is idempotent; conflicting reuse fails closed.
- Repeated/no-progress actions receive stable machine errors.
- Resource and script derivation cannot exceed the envelope or escape Workspace.

### 12.2 Integration tests

Use the real Pi Agent with faux model responses to prove:

- success completes in context + submission;
- one Sandbox failure completes after one repair submission;
- a scheduled receipt stops the Pi loop immediately;
- no blank assistant card is required for success;
- the 20-step hard limit still terminates pathological behavior.

### 12.3 VM acceptance

With `deepseek-v4-flash`, run the heat-diffusion smoke from an empty Project and
require:

- exact scientific source contract;
- successful bubblewrap Sandbox;
- one AgentTask and one linked VM-local Slurm job;
- distinct `srun -c 1`, `srun -c 2`, and `srun -c 4` Evidence;
- numerical convergence audit in [1.8, 2.2];
- downloadable raw results, JSON summaries, report, SVGs, Capsule, and
  authoritative platform provenance;
- ChangeSet publication and formal Run submission only through the existing
  explicit approval path.

## 13. Rollout and rollback

Ship behind `PILOT107_PHASE_AWARE_BUILDER=1`, enabled in CPU-RC after source
acceptance. The legacy catalog remains available when the flag is false during
one release for rollback only. Both modes share the same durable Project,
Workspace, ChangeSet, Sandbox, and AgentTask data.

Rollback disables the flag and redeploys the prior immutable image set. It does
not delete Projects, ChangeSets, tasks, Runs, Evidence, or user artifacts.

## 14. Acceptance boundary

The refactor is complete only when all source gates pass, the immutable VM image
binding is verified, browser-visible Files and Agent workflows remain healthy,
and the real heat-diffusion task completes through the phase-aware facade on
`vm-slurm`. A passing unit test or a scheduled Slurm job alone is insufficient.
