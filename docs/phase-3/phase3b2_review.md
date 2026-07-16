# Phase 3B-2 Review Report

日期：2026-07-15  
范围：schema migration、PlatformSnapshot store/read API、health/readiness、FastAPI/OpenAPI 适配。

## Findings

### P1：平台命令动态参数可能通过读取 API 泄露

状态：已修复。

采集层原先只脱敏 `stdout/stderr`，`squeue -u alice` 和 home 路径仍存在于 `argv`；safe API 又保留
了 `argv`。现已在采集层对 argv 同步脱敏，并在 owner-scoped API 中防御性移除 argv 和输出。

### P1：OpenAPI snapshot detail 缺少 path parameter

状态：已修复。

通用 forwarder 不从函数签名声明 `snapshot_id`，首版 OpenAPI 没有该路径参数。已补显式约束，
快照测试锁定 path/query 参数和唯一 operation ID。

### P1：ASGI 转发行为没有 contract 证据

状态：已修复。

仅验证 `app.openapi()` 不能证明兼容层保持 auth、request ID、ETag 和 POST body 行为。已加入纯 ASGI
消息测试，覆盖 401、Request-ID 透传、304 和隐藏 POST 兼容路由。

### P2：可选依赖被错误表示为已在线验证

状态：已修复。

run backend 和 local LLM 只在进程配置层可知，不能标成 `ok`。现在报告 `configured/disabled`；
SQLite、Evidence root 和 PlatformSnapshot schema 只有真实检查通过才报告 `ok`。

## Residual Risks

- ASGI 尚未承接 SSE；完整事件流仍使用 stdlib server，切换默认生产入口前必须补 ASGI stream test；
- OpenAPI 首批仅显式公开 health/platform，且响应 schema 还不是可生成强类型客户端的完整模型；
- owner/query/error 仍主要位于 2100 行以上的 `http_app.py`，后续领域路由必须逐模块迁出；
- readiness 不主动调用 Slurm 或 LLM，避免健康探针制造负载；运行时 SLO 需独立 metrics/probe；
- 全 `scripts/` Ruff 有 36 项既有问题，当前阶段检查范围通过但正式 CI 前必须清理；
- 当前目录无 `.git` 元数据，无法提供 diff/commit/CI 可追溯证据。

## Verification

- `PYTHONPATH=src uv run pytest -q`：379 passed；
- `uv run ruff check src tests scripts/smoke_sim_phase3a.py`：通过；
- `uv run mypy src`：46 source files 通过；
- OpenAPI contract snapshot：通过；
- ASGI auth/request-id/ETag/POST compatibility tests：通过。

本阶段未新增 Docker 运行时行为，因此未重复 live job smoke；3B-3 的 collector 实现必须先验证本地
Docker simulator，再运行 live probe。

## Decision

本阶段 P0/P1 已清零，允许进入 Phase 3B-3。上述 residual risks 不允许被解释为 FastAPI 已完成
生产切换，也不允许把 `configured` 的 LLM/Slurm 宣传为在线健康。
