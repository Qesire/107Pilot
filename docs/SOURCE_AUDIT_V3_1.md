# 107Pilot Source Audit v3.1

Status: integration cleanup baseline  
Design authority: WorkArea → Research Graph → Experiment; Launch → Run(s) → SchedulerJob(s)

## 1. Integration baseline

This branch starts from `research-workspace-http-v1` rather than `main`.

Reasons:

- it already contains the UX v2 shell/file/transfer work;
- it contains explicit Research Workspace ownership/binding semantics;
- it contains the server-side evidence-first Run Workspace projection;
- `main` is retained as release history but is not the most complete product integration state;
- `r3-failure-recovery` and `r3-runtime-adaptive-polling` are feature-source branches to be selectively ported, not alternative roots.

No historical branch is treated as an acceptance-ready release by itself.

## 2. Canonical terminology after v3.1

| Existing name | v3.1 interpretation | Action |
|---|---|---|
| ResearchWorkspace | WorkArea persistence/binding foundation | retain, evolve; compatibility names may remain in DB/API during migration |
| ExperimentShell | compatibility execution shell built from Contract/Run | retain temporarily; migrate semantics, do not treat as canonical Experiment |
| RunWorkspace | evidence-first projection of one Run | retain; long-term product name should avoid implying an Agent writable workspace |
| Agent Workspace | isolated writable engineering workspace | retain as the only canonical writable `Workspace` concept |
| Contract | persisted execution configuration revision | retain |
| Run | one 107Pilot execution record | retain |
| Scheduler Job | actual Slurm job | retain/distinguish from Run |
| Launch | one committed logical execution that may produce 1..N Runs | missing; add |
| Research Graph | WorkArea-level observed/proposed/confirmed object relations | missing; add |
| Experiment | scientific semantic subgraph inside a WorkArea | missing in canonical form; do not infer from a single Contract/Run |

## 3. Functionality completion inventory

### Keep: mature or structurally sound

- Contract canonical persistence/digest/server validation.
- Run store/state machine/lineage.
- Run Evidence pipeline, Evidence browser, Capsule and provenance gates.
- `RunWorkspaceService`: server-derived outcome, attention, next action and provenance.
- Runtime Watch worker architecture.
- Resource capability/entitlement/platform observation/resource ledger separation.
- File operations, uploads, FileBrowser v2 foundations, picker and global TransferManager.
- Recipe/Market domain.
- Diagnosis and remediation state machine.
- Agent isolated project/workspace, ChangeSet, validation and approval boundaries.
- Identity/Slurm/Evidence transport separation.

### Integrate from feature branches

From `r3-failure-recovery`:

- `RunRepairPanel.tsx` and `run-repair.ts` behavior;
- run-local repair flow with persistent diagnosis gate;
- proposal approval/rejection, execution and derived-run comparison;
- isolated repair project only when source modification is required.

Do **not** carry branch-local temporary validation machinery as product source.

From `r3-runtime-adaptive-polling`:

- visibility/state-aware Runtime Watch polling policy;
- terminal-state polling stop and child-stream gating.

Do **not** carry `_temp_*` scripts/workflows.

### Missing canonical v3.1 capabilities

- WorkArea user-facing domain/API terminology and current WorkArea selection.
- Generic ResearchObject/ResearchRelation graph with provenance/status/confidence.
- deterministic CodeAnalysisSnapshot and execution-model discovery.
- Launch domain and Launch-bound Preflight transaction.
- runtime reconciliation of predicted vs observed jobs/tasks.
- Experiment memberships/roles/relations as a semantic graph layer.
- BindingProposal and StructureProposal approval flows.
- WorkArea-level recommended actions and unclassified-object queues.
- WorkArea-first frontend shell and Research Graph/Experiment views.

## 4. Confirmed redundancy / compatibility debt

### A. Dead or duplicate implementation candidates

1. `WorkspacePage` in `apps/web/src/pages.tsx`
   - legacy dashboard implementation;
   - v2 `App.tsx` renders `WorkspacePageV2` instead;
   - should be removed after a repository-wide reference check and import cleanup.

2. `PlannedPage` in `apps/web/src/pages.tsx`
   - placeholder for Market/Agent/Terminal/Studio from an earlier slice;
   - current `App.tsx` routes real implementations;
   - should be removed after reference check.

3. UX validation/application scripts and workflows named `_temp_*`
   - branch-local implementation scaffolding, not product capability;
   - exclude from integration branch; delete only from branches that are being promoted.

### B. Semantically obsolete but reusable — do not delete

1. `ExperimentShell.tsx`
   - layout/trajectory/context patterns are reusable;
   - current `ExperimentContext = Contract | Run` is not a real Experiment;
   - `experimentRunNextAction()` duplicates business decisions in the browser;
   - migrate into WorkArea/Launch/Run views rather than delete immediately.

2. `WorkspacePageV2.tsx`
   - good Workbench layout and preparation cards;
   - currently chooses a focus Run and labels `/runs` as Experiment compatibility data;
   - migrate to `WorkAreaSummary`/recommended actions instead of rewriting UI from scratch.

3. `RunEvidencePanel.tsx`
   - mature Run-local evidence UI;
   - current remediation entry can jump to `/agent`;
   - integrate `RunRepairPanel` and keep `/agent` only for explicit isolated engineering work.

4. `ResourceDashboard.tsx`
   - no longer belongs on the default Workbench;
   - retain for advanced resource/platform detail until reference/use audit is complete.

### C. Generated deployment artifacts — not source redundancy

`src/pilot107/web/static/**` is generated frontend output, but current CI explicitly checks build drift and deployment expects it to be tracked. Do not delete it as dead source. Consolidate it only by changing the build/release contract first.

## 5. First cleanup rules

- Never delete a capability because a newer page changes its navigation placement.
- Separate `dead code` from `obsolete semantics`.
- Do not rename persisted tables destructively during the WorkArea migration; add compatibility aliases/migrations first.
- Do not port temporary validation scripts/workflows into the integration branch.
- Do not recompute server-authoritative `next_action` from raw state in new frontend code.
- Agent inference never becomes a WorkArea binding, Experiment structure, scientific fact or hard Preflight blocker without the required authority transition.
- A Run may exist in a WorkArea without belonging to any Experiment.
- A Launch may produce multiple Runs/jobs; do not preserve submit→Run→Job 1:1 as a domain invariant.

## 6. Cleanup sequence

1. Mechanical dead-code/reference cleanup.
2. Selective port of RunRepair and adaptive Runtime polling.
3. Rename product-facing Research Workspace semantics to WorkArea while retaining persistence compatibility.
4. Add Research Graph foundation.
5. Add Launch and Launch-bound Preflight.
6. Migrate Workbench/AppShell from `/runs = 实验` to WorkArea-first navigation.
7. Replace compatibility Experiment shell semantics with WorkArea/Experiment/Launch/Run projections.
8. Run integrated Python/Web/UI/Compose/Security gates when GitHub runner execution is available.

## 7. Acceptance rule

A source capability is considered ready only when all three are true:

`Ready(c) = DomainCorrect(c) ∧ Integrated(c) ∧ Verified(c)`

Existing code alone is not completion evidence, and a red workflow with zero executed steps is not code-failure evidence.
