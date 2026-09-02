# Agent Turn 闭环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use test-driven development and verification-before-completion. This plan is executed inline; do not dispatch subagents.

**Goal:** 修复 phase-aware Builder 跨 Turn 续修，并统一 Agent 审批说明、错误透传、中文提示词和入口最小权限。

**Architecture:** Python 持久工作流负责权威 continuation receipt 和 capability；TypeScript Agentd 负责严格工具 schema、中文角色行为和 Pi loop。HTTP 工具边界继续 fail closed，但让合法 ToolResult 错误可被模型纠正。

**Tech Stack:** Python 3.12、pytest、TypeScript、TypeBox、Vitest、pi-agent-core。

## Global Constraints

- 不做前端视觉或布局重构。
- 不放宽审批、资源、Workspace、Project、Session 或 Turn fencing 边界。
- 所有实现先写失败测试并观察预期失败。

---

### Task 1: Builder 持久续修与审批说明

**Files:**
- Modify: `tests/agent/test_builder_workflow_service.py`
- Modify: `src/pilot107/services/builder_workflow_service.py`

- [ ] 增加跨 Turn repair、权威 next submission 字段和 `approval_summary_zh` 的失败测试。
- [ ] 运行聚焦 pytest，确认失败来自旧 Turn 限制和缺失字段。
- [ ] 实现最小服务变更并运行聚焦 pytest 至通过。

### Task 2: 严格 ToolResult 错误透传

**Files:**
- Modify: `tests/test_agent_tool_gateway_api.py`
- Modify: `src/pilot107/api/agent_tool_routes.py`
- Modify: `services/pilot-agentd/tests/tool-gateway.test.ts`
- Modify: `services/pilot-agentd/src/tool-gateway.ts`

- [ ] 增加非 2xx 合法 ToolResult 可读、恶意或错配正文仍拒绝的失败测试。
- [ ] 运行 Python/TypeScript 聚焦测试确认失败。
- [ ] 让 API 输出闭合错误信封，并让 Agentd 在任何状态码下先严格校验信封。

### Task 3: 分入口权限与同步自然语言说明

**Files:**
- Modify: `services/pilot-agentd/tests/project-tools.test.ts`
- Modify: `services/pilot-agentd/src/project-tools.ts`
- Modify: `tests/agent/test_project_agent_service.py`
- Modify: `src/pilot107/services/project_agent_service.py`
- Modify: `services/pilot-agentd/tests/protocol.test.ts`
- Modify: `services/pilot-agentd/src/protocol.ts`
- Modify: `tests/agent/test_protocol.py`
- Modify: `src/pilot107/agent/protocol.py`
- Modify: `tests/test_agent_turn_worker.py`
- Modify: `src/pilot107/worker/agent_turn_worker.py`

- [ ] 用失败测试定义各 profile 精确工具集和 `workspace_patch.approval_summary_zh`。
- [ ] 统一 Python capability、Python/TS protocol pairing 与实际 Agentd 注册表。
- [ ] 运行相关聚焦测试至通过。

### Task 4: 中文提示与非小预算闭环

**Files:**
- Modify: `services/pilot-agentd/tests/tasks.test.ts`
- Modify: `services/pilot-agentd/src/tasks.ts`
- Modify: `services/pilot-agentd/tests/readonly-turn.integration.test.ts`
- Modify: `services/pilot-agentd/src/turn-executor.ts`

- [ ] 增加中文角色、权限声明、自然语言摘要和超过旧 20-step 阈值仍继续的失败测试。
- [ ] 将所有系统/修复/恢复提示改为中文，并将异常循环保险提高到 64/128。
- [ ] 运行 Agentd 聚焦测试至通过。

### Task 5: 回归与本地网页验收

**Files:**
- Verify only; no frontend layout changes.

- [ ] 运行 Python 与 Agentd 完整测试和静态检查。
- [ ] 固定启动本地服务与网页挂载。
- [ ] 仅用 `pilot-browser` 从真实前端创建/继续 Builder Session，确认事件流中有中文自然语言、折叠工具数据可展开、repair 可跨 Turn 闭环到 scheduled。
- [ ] 保存本地验收证据并报告仍留给后续前端设计的问题。

