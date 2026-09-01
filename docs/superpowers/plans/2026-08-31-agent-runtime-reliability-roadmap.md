# Agent Runtime Reliability Closure Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved Agent runtime reliability design through four independently reviewable TDD plans.

**Architecture:** Preserve the persistent control plane and asynchronous Slurm execution plane. Close causality first, then tool recovery, workspace consistency, and finally long-context continuation; each phase must retain a working product and pass its own acceptance gate.

**Tech Stack:** Python, TypeScript, SQLite/PostgreSQL, Pi Agent Core, Slurm VM simulation, pytest, Vitest, React Testing Library.

## Global Constraints

- Main Agent owns and revises every implementation plan.
- Repository code edits and test execution may由接手模型执行；接手前必须完整阅读本路线图、对应 phase 计划和
  [2026-09-01-agent-runtime-handoff.md](./2026-09-01-agent-runtime-handoff.md)，遵守既有 TDD、独立复审和 dirty-worktree 保护约束。
- 产品运行时模型固定为 USTC107 配置中的 `deepseek v4 flash`。不得把执行代码的模型与 107Pilot 面向用户的运行时模型混为一谈。
- No phase may overwrite unrelated dirty-worktree changes.
- RED output, GREEN output, focused regression and completion verification must be recorded for every task.
- Do not start a later phase when the earlier phase's completion gate is red.

---

- [ ] **Phase 1 — AgentTask Evidence Gate (P0) — IN PROGRESS**

Task 1–5 已完成并经独立复审；Task 6 前端/live Slurm 验收于 2026-09-01 按用户要求暂停。
继续执行 [2026-08-31-agent-task-evidence-gate.md](./2026-08-31-agent-task-evidence-gate.md) 的 Task 6，
不得把 focused 后端测试替代真实前端入口验收。

- [ ] **Phase 2 — Invocation Recovery (P0)**

Execute [2026-08-31-agent-invocation-recovery.md](./2026-08-31-agent-invocation-recovery.md). Task 1 may begin after Phase 1 domain enums are frozen, but integration and completion verification wait for Phase 1 GREEN.

- [ ] **Phase 3 — Workspace Live Revision (P1)**

Execute [2026-08-31-workspace-live-revision.md](./2026-08-31-workspace-live-revision.md). It consumes Phase 2 durable operation identity and Phase 1 workspace-bound Evidence receipt.

- [ ] **Phase 4 — Context Continuation (P1)**

Execute [2026-08-31-agent-context-continuation.md](./2026-08-31-agent-context-continuation.md). It consumes Phase 2 per-tool checkpoints/heartbeats and must not be used to mask unresolved invocation or workspace conflicts.

- [ ] **Final integrated acceptance**

Run the union of all four completion gates, then execute the unchanged public UI path against VM-local Slurm with real Run/Evidence/Capsule records. Verify restart recovery at API, Worker and agentd boundaries; exactly one Slurm job; no premature follow-up; one workspace revision; bounded continuation; and no fake terminal records.
