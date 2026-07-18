# Phase 3G Run Submission Outbox / Fencing 审查

日期：2026-07-18
范围：Run submit durable outbox、API/Worker 接线、per-run Slurm marker、submission fencing、跨线程/进程/容器竞争与崩溃恢复。
结论：本 Run submit 切片 P0/P1 已清零；生产 builder 已使用统一 substrate。R4 尚未完成，collection、Agent execution、全领域 PostgreSQL Store、metrics/recovery/security 仍需后续切片。

## 固定的业务契约

- API submit 先以确定性 message ID 将 `run.submit` 写入 durable outbox，再立即尝试领取；HTTP 成功语义保持同步兼容；
- API 在 enqueue 后退出时，独立 Worker 会领取同一消息并完成提交；
- Run 行持久化 `submission_owner` 与单调 `submission_fencing_token`，旧 worker 的 receipt/failure 写回被数据库拒绝；
- outbox ack 与 retry 同时受 message fencing 保护；Run 已写入但 ack 前崩溃时，新 worker 只补 ack，不再调用 backend；
- 外部 submit 后进程崩溃时，新 worker 只按 owner、精确 per-run job name 和时间窗 reconciliation；单一匹配绑定原 Job；
- transport timeout 或 crash 后 marker 尚不可见时，仅延迟重试 reconciliation，绝不自动重提；预算耗尽后 outbox dead-letter，Run 进入 `SUBMISSION_UNCERTAIN`；
- 正常的明确 backend rejection 仍进入 `SUBMIT_FAILED`；workflow retry 只 enqueue，由 dispatcher 统一执行；
- API/Worker 默认共享 SQLite control repository；PostgreSQL control implementation 已有 parity，但完整领域 Store 尚未切换。

## Findings-first 结果

### 已修复 P1：同用户并发提交共享 `pilot107-run` marker

旧 REST payload 固定使用 `pilot107-run`，command backend 也不设置 job name；reconciliation 只能依赖共享名称与时间窗，同一用户并发时会得到多个候选。现在 RunService 以 `job_name_marker + sha256(run_id)[:20]` 生成稳定名称，REST job payload 与两类 `sbatch --job-name` argv 使用同一值，reconciliation 查询精确 marker。两个 Run 的 marker 不同且重试稳定。

### 已修复 P1：模糊 transport 结果可能自动产生第二次外部 submit

旧逻辑在 timeout 后查询不到 marker 会立即 retry submit；Slurm 可见性延迟或旧调用仍在飞行时存在重复作业窗口。生产 outbox 路径现对 `not_found` 只重试查询，backend submit call 始终最多一次；达到 attempt budget 仍不可判定时 fail closed 到 `SUBMISSION_UNCERTAIN`。legacy 无 control repository 的单元兼容路径尚保留旧行为，但 API/Worker builder 已全部启用 control repository。

### 已修复 P1：demo Worker 用容器服务 UID 检查模拟用户目录

跨容器 live 首次接管时，Worker 对 `/public/home/alice` 使用 `LocalPathChecker`，以 UID 10700 得到 `WORKDIR_NOT_READABLE/NOT_EXECUTABLE`，消息按预算进入 dead-letter；API demo 路径一直使用 pure-path 契约，因此同步金路径未暴露。demo/in-memory Worker 现与 API 一致，只做 allowed/shared/local path policy，不虚构模拟用户的本地权限。修复后的新 Run 一次领取成功。

## 验证证据

- 单元/契约覆盖：同步 enqueue+dispatch、enqueue-only crash、两个线程竞争、两个 `spawn` worker 进程竞争、外部 submit 后 crash、Run write 后 ack 前 crash、无 reconcile dead-letter、模糊 timeout 零重提、dependency audit 单次写入；
- 两个 spawn 进程共享 SQLite/outbox 时，外部 side-effect log 恰好 1 行，Run 只绑定一个 Job；
- stale Run receipt：token 1 worker 在 token 2 抢占后写回被 `RunStoreFenceConflict` 拒绝；
- 常规全量门禁：533 passed、9 PostgreSQL integration skipped、2 subtests；Ruff 与 strict mypy 通过；
- Docker Web 金路径：`run_b7a18f7d500d4693a499dec7b5bfef39`，Job `demo-50c3ceb3a5a8`，SUCCEEDED、collection succeeded、20 Evidence objects；outbox succeeded、attempts=1、fence=1；
- Docker enqueue-only 跨容器恢复：`run_live_outbox_recovery_fixed_20260718`，Worker owner `runtime-worker-03b5f580ad62`、fence=1、Job `demo-632e58043ccf`、Run SUCCEEDED、collection succeeded、outbox succeeded/attempts=1；
- 第一次 live failure `run_live_outbox_recovery_20260718` 保留为 dead-letter 证据，证明错误 preflight 未触发外部 submit，修复使用全新 Run 复验而非篡改历史。

## 残余风险与下一切片

1. collection task 与 remediation/Agent execution 仍使用各自旧 lease 字段，尚未统一 fencing token/outbox；
2. SQLite RunStore 仍是主要领域数据库；PostgreSQL control parity 不等于全领域 PostgreSQL parity；
3. fencing 保证过期 worker 不能写回，但 Slurm 本身没有幂等 API；当前选择 at-most-once + uncertain fail closed，而非用自动重提换取表面可用性；
4. submission 指标目前进入 Worker health JSON，尚未形成 Prometheus metrics、trace 与长期累计计数；
5. Docker Registry/PyPI DNS 在本次重建时不可达，live 使用已有依赖镜像的 source overlay；R5 必须提供锁定 wheelhouse 和完全离线构建，不把 overlay 当发布资产；
6. 真实 107、VM 上传与部署、生产身份仍未触碰。
