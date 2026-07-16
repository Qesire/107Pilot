# Phase 3A Review Report

日期：2026-07-15  
范围：Run/Contract/Advice/Execution 读模型、cursor、事件、SSE、Run 图谱、响应元数据。

## Findings

### P1：Recipe 过滤缺少 Contract owner 联合条件

状态：已修复。

`RunStore.list_runs_page()` 原先只按 `contract_id` 和 `recipe_version_id` 联合查询。
如果内部损坏数据把 Alice Run 指向 Bob Contract，Alice 可以通过 Recipe 过滤观察关联元数据。

修复：

- 子查询增加 `contracts.owner = runs.owner`；
- 增加损坏跨 owner Contract link 的负面测试；
- 常规 owner-scoped Run 列表仍可显示损坏 Run，但 Recipe 元数据不能跨 owner 参与过滤。

### P1：SSE 内部轮询继承条件 GET header

状态：已修复。

SSE handler 原先把浏览器请求的全部 header 传入内部事件查询。携带 `If-None-Match` 的非标准客户端
可能让内部查询返回 304，导致 stream 错误。

修复：

- SSE 内部请求剥离 `If-None-Match`；
- live handler 测试携带 stale ETag，仍返回 `text/event-stream` 和事件。

## Residual Risks

- SQLite 轮询 SSE 适合当前单机比赛模式，不是多副本生产事件系统；
- cursor 不签名，但包含 kind/filter scope，owner 仍由服务端身份强制绑定。伪造 cursor 只能改变位置，
  不能改变 owner 或过滤范围；
- `%LIKE%` 搜索在大文本规模下需要 FTS，当前 1 万 Run 的无搜索分页已验证索引；
- HTTP transport 仍是 thread-per-connection，生产阶段迁移 ASGI 和 outbox/broker；
- Run/Contract 跨表完整性主要由应用层保证，PostgreSQL 阶段补正式外键和事务边界。

以上残余项不阻断 Phase 3B，但均已进入主执行计划。

## Verification

- `PYTHONPATH=src uv run pytest -q tests/test_read_models.py`：7 passed；
- `PYTHONPATH=src uv run pytest -q`：361 passed；
- `uv run ruff check src tests scripts/smoke_sim_phase3a.py`：通过；
- `uv run mypy src`：42 source files 通过；
- `bash scripts/check-sim-core.sh`：Slurm controller UP，两个 worker idle；
- `bash scripts/smoke-sim-phase3a.sh`：通过，真实 simulator job `30`。

## Decision

Phase 3A 的 P0/P1 finding 已清零，允许进入 Phase 3B。
