# Phase 3G Control Repository / Fencing / Outbox 审查

日期：2026-07-18  
范围：backend-neutral 控制面 Repository 契约、SQLite 参考实现、PostgreSQL 实现、迁移、lease/fencing、durable outbox 和双后端并发 parity。  
结论：本底座切片 P0/P1 已清零，可用于下一切片的 submit/reconcile/collection/Agent 接线；R4 尚未完成，不能据此宣称现有业务 Store 已迁移 PostgreSQL 或多副本外部副作用已零重复。

## 固定的契约

- `ControlRepository` 不暴露 SQLite/PostgreSQL connection 类型，统一 lease、outbox 和 fencing 行为；
- lease 以 `(resource_kind, resource_id)` 唯一，活跃 owner 可续期，过期抢占必须增加 fencing token；
- release/renew/ack/retry 都绑定 owner 与 fencing token，旧 worker 即使恢复运行也不能提交过期结果；
- outbox 的 `message_id` 同时承担幂等键和内容绑定：相同内容重放返回原对象，不同内容冲突 fail closed；
- claim 支持 topic 隔离、租约过期重领、attempt 计数、延迟重试和 attempt budget 后 dead-letter；
- SQLite 使用 `BEGIN IMMEDIATE` + CAS；PostgreSQL 使用 row lock、`FOR UPDATE SKIP LOCKED` 和数据库事务；
- PostgreSQL migration 由 advisory transaction lock 串行化，checksum 漂移阻断启动，schema 使用 TIMESTAMPTZ/JSONB 和必要 due/topic 索引；
- PostgreSQL 必须使用 UTF-8 server encoding，避免 owner/topic/payload 退化为 driver bytes。

## Findings-first 结果

### 已修复 P1：非 UTF-8 PostgreSQL 被误报为 migration checksum 漂移

第一次真实 parity 使用 SQL_ASCII 临时 cluster。psycopg 对其中的 TEXT 返回 bytes，首次 migration 成功，但第二个 Repository 实例把 bytes 直接 `str()` 后与十六进制 checksum 比较，误判为 schema 被篡改。实现现于 migration 前验证 `server_encoding=UTF8` 并明确 fail closed；UTF-8 cluster 上重复初始化和四线程并发初始化均通过。生产 compose 也固定使用 PostgreSQL 官方默认 UTF-8 初始化路径。

### 已处理环境阻碍：Docker Registry 不可达但未降低验证标准

`postgres:17-alpine` 连续两次在 registry TLS/EOF 阶段失败，直接 HTTPS 探测也在代理握手后失败。审查没有把 skipped 测试计为通过，而是从 Ubuntu 仓库仅下载 PostgreSQL 16 `.deb` 到 `/tmp`，用户态解包并启动只监听 `/tmp` Unix socket 的临时 UTF-8 cluster。仓库同时保留 `postgres:16-alpine` 专用 compose 与一键 smoke，网络恢复后可重复同一契约。

## 验证证据

- SQLite/PostgreSQL 共用契约：16/16 通过；
- 4 个并发 lease claimant：同一资源恰好 1 个 owner；
- 40 条同 topic outbox：4 个并发 worker 共领取 40 条，message ID 40 个唯一，attempt 均为 1；
- 过期 outbox 被新 worker reclaim 后 fencing token `+1`，旧 worker ack 被拒绝；
- topic filter 不领取其他 due message；重试到达 attempt budget 后进入 dead-letter 且不再被领取；
- migration 首次创建、重复初始化和并发初始化通过；非 UTF-8 配置已有明确阻断；
- 无 PostgreSQL 环境时，8 条 PostgreSQL 契约显示 skipped 而不是伪通过；专用 smoke 需要显式 `PILOT107_TEST_POSTGRES_ALLOW_RESET=1` 才能清空测试表；
- 常规全量 Python 回归：516 passed、8 PostgreSQL integration skipped、2 subtests；真实 PostgreSQL 启用时最终门禁为 524 passed、2 subtests；Ruff 与 strict mypy 通过。

## 残余风险与下一切片

1. RunStore、ContractStore、TemplateMarketStore、PlatformSnapshotStore、UserEntitlementStore 和 RemediationStore 仍直接使用 SQLite；本切片只提供生产一致性 substrate；
2. 当前 Run submit 仍是 API 内联外部调用，collection/remediation 各自维护旧 lease 字段；必须接入统一 outbox/fencing 后才可执行多 API/Worker crash 结论；
3. fencing 只能阻断过期 worker 写回数据库；Slurm/gateway 外部副作用还需稳定 idempotency marker、reconciliation 与 outbox dispatcher 联合证明零重复；
4. PostgreSQL 旧 SQLite 数据导入、回滚运行手册、备份恢复和全领域 repository parity 尚未完成；
5. metrics/trace/audit redaction、安全负面测试和 VM 资源约束仍属于后续 R4/R5。
