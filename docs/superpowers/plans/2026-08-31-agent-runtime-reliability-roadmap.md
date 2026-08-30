# Agent Runtime Reliability Closure Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved Agent runtime reliability design through four independently reviewable TDD plans.

**Architecture:** Preserve the persistent control plane and asynchronous Slurm execution plane. Close causality first, then tool recovery, workspace consistency, and finally long-context continuation; each phase must retain a working product and pass its own acceptance gate.

**Tech Stack:** Python, TypeScript, SQLite/PostgreSQL, Pi Agent Core, Slurm VM simulation, pytest, Vitest, React Testing Library.

## Global Constraints

- Main Agent owns and revises every implementation plan.
- GPT-5.6 Luna performs repository code edits and test execution task-by-task.
- No phase may overwrite unrelated dirty-worktree changes.
- RED output, GREEN output, focused regression and completion verification must be recorded for every task.
- Do not start a later phase when the earlier phase's completion gate is red.

---

- [ ] **Phase 1 — AgentTask Evidence Gate (P0)**

Execute [2026-08-31-agent-task-evidence-gate.md](./2026-08-31-agent-task-evidence-gate.md). This removes the current premature follow-up race and is the first production-code phase.

- [ ] **Phase 2 — Invocation Recovery (P0)**

Execute [2026-08-31-agent-invocation-recovery.md](./2026-08-31-agent-invocation-recovery.md). Task 1 may begin after Phase 1 domain enums are frozen, but integration and completion verification wait for Phase 1 GREEN.

- [ ] **Phase 3 — Workspace Live Revision (P1)**

Execute [2026-08-31-workspace-live-revision.md](./2026-08-31-workspace-live-revision.md). It consumes Phase 2 durable operation identity and Phase 1 workspace-bound Evidence receipt.

- [ ] **Phase 4 — Context Continuation (P1)**

Execute [2026-08-31-agent-context-continuation.md](./2026-08-31-agent-context-continuation.md). It consumes Phase 2 per-tool checkpoints/heartbeats and must not be used to mask unresolved invocation or workspace conflicts.

- [ ] **Final integrated acceptance**

Run the union of all four completion gates, then execute the unchanged public UI path against VM-local Slurm with real Run/Evidence/Capsule records. Verify restart recovery at API, Worker and agentd boundaries; exactly one Slurm job; no premature follow-up; one workspace revision; bounded continuation; and no fake terminal records.
