# Phase 3G trace、LLM/SSE metrics 与控制仓库运行时接线审查

日期：2026-07-18  
范围：持久 request/domain trace、LLM/SSE 专项 metrics、API/Worker 控制仓库运行时选择。  
结论：本切片的功能契约和 P0/P1 审查已通过；R4-3 尚未完成，完整 PostgreSQL 业务 Store parity 仍是后续主功能缺口。

## 已完成

- `ControlRepository` 新增持久 trace，关联 `request_id`、`run_id`、`job_id`、`session_id`，只保存方法、低基数路由、状态、actor 与时间，不保存请求体、响应体或凭据；
- SQLite 与 PostgreSQL 使用同一 trace 契约；PostgreSQL 以独立 `003g.002` migration 追加表和索引，未修改既有 `003g.001` checksum；
- API 的 GET/POST/PATCH 自动写 trace，SSE 只记录一次外层连接，不为内部 poll 制造 trace；trace 写失败不改变业务响应，并暴露 success/error counter；
- OpenAI-compatible provider 按每次 attempt 记录成功/失败、时延和网关报告的 input/output token；格式修复重试会分别计数，observer 失败不影响 LLM 结果；
- stdlib SSE 暴露 active、完成原因、时延和发送事件数，覆盖 once complete、deadline、poll error 和 client disconnect；
- API 与 Worker 支持 `PILOT107_CONTROL_POSTGRES_DSN`。设置时使用 PostgreSQL control repository，未设置时保留 SQLite 本地模式。

## 审查与验证

- SQLite/PostgreSQL 共用 repository 契约新增 trace 写入、四类 ID 查询、排序、校验和 limit 边界；本机未提供 PostgreSQL 实例，因此 13 项 PostgreSQL integration 明确 skipped；
- SSE live replay 验证事件摘要、无原始敏感 payload、metrics 回落到 active=0，并产生单条 run trace；
- LLM fake gateway 验证 usage token 采集和成功 outcome；Prometheus render 验证低基数标签；
- API/Worker config 验证默认 SQLite 与显式 PostgreSQL DSN 选择；DSN dataclass 字段不进入 repr；
- 全量 Python：590 passed、13 skipped、2 subtests passed；
- Ruff、strict mypy（73 source files）与 `git diff --check` 通过。

全量测试首次在文件系统/网络沙箱内出现 7 个 `PermissionError`，均为测试创建 `127.0.0.1` 回环 HTTP server 被禁止；相同 revision 在允许本机回环套接字后 590 项全部通过。这些失败不是产品回归，也未被隐藏为 skip。

## 未完成与不得过度声明

1. `RunStore`、`ContractStore`、`RemediationStore`、`TemplateMarketStore`、`PlatformSnapshotStore` 和 `UserEntitlementStore` 仍以 SQLite 为主；当前 PostgreSQL 选择只覆盖 control repository；
2. 本机没有 PostgreSQL 镜像或临时实例，本次新增 `003g.002` 尚未获得新的真实 PostgreSQL 运行证据；
3. ASGI compatibility path 仍不是原生长连接 SSE transport，Compose 当前 stdlib SSE 路径已覆盖；
4. 低优先级安全增强按当前执行优先级后置；本切片未发现阻塞功能交付的 P1 安全问题；
5. 未访问真实 107，未上传或部署 VM。
