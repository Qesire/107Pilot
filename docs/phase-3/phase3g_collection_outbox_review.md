# Phase 3G Collection Outbox / Fencing 审查

日期：2026-07-18
范围：Evidence collection durable outbox、task generation、task fencing、Worker 接线、线程/进程竞争与 ack crash 恢复。
结论：本 collection 切片未发现遗留 P0/P1；production Worker 已进入统一 control substrate。R4-2 尚未完成，Agent execution 仍需迁移。

## 固定的业务契约

- `collection_tasks` 是 collection 业务状态源，`control_outbox` 是唯一 production dispatch 队列；
- Worker 先扫描 due/过期 legacy task，以 `collection:<task_id>:<generation>` 幂等写入 `collection.execute`，再按 outbox lease 领取；
- outbox fencing token 同步写入 task 行，success/failure 必须同时匹配 owner 与 token；即使部署后复用相同 worker ID，旧 token 也不能写回；
- task 成功写库、outbox ack 前崩溃时，新 worker 观察到 terminal task 后只补 ack，不再次调用 collector；
- runtime task 从 succeeded 再激活时 generation 单调递增，创建新的确定性 message ID；旧 succeeded message 不会吞掉新一轮采集；
- retryable failure 同时持久化 task backoff 与 outbox backoff；达到预算后 task 永久失败、outbox dead-letter；鉴权失败直接永久失败；
- Worker 每次只预领取一个 collection message，并在阻塞 collector 期间按租约三分之一周期续租；续租本身受 owner/token/未过期条件保护；
- 无 control repository 的测试/嵌入式兼容路径暂保留旧 task lease，production Worker builder 已始终注入 SQLite control repository。

## Findings-first 结果

### 已修复 P1：只校验 owner 不能阻止同名旧 worker 写回

旧 `mark_collection_task_succeeded/failed` 只比较 `lease_owner`。进程重启或 worker ID 固定时，新实例会复用相同 owner；旧实例即使租约已过期，仍可覆盖新实例结果。现在 task claim 保存 outbox fencing token，所有 production terminal 写回同时比较 state、owner、token；同名 token 1 在 token 2 接管后被 `CollectionTaskFenceConflict` 拒绝。

### 已修复 P1：task ID 固定消息无法表达重复 runtime 采集

`runtime_status` 会在新的非终态 snapshot 后从 succeeded 重新激活。若 message ID 只含 task ID，既有 succeeded outbox 行会让 enqueue 幂等命中旧终态，导致新一轮任务永不执行。现在 task 行保存 generation，再激活时原子加一；新旧消息分别保持完整审计历史。

### 已修复 P1：任务成功写库后 ack 崩溃可能重复采集

Worker 现在先以 task fence 持久化 succeeded，再 ack outbox。若进程在两者之间退出，租约过期后的新 worker 会识别同 generation task 已终态，只 ack 消息。自动测试验证 collector 调用总数仍等于七个任务数。

### 已修复 P1：批量预领取与慢 collector 会使后续消息租约过期

旧迁移草案一次领取整个 batch，再串行执行 collector；首个慢任务可能让尚未开始的后续消息全部过期。现在 collection dispatcher 每次只领取一个 message，执行期间 heartbeat 续租，完成后才领取下一个。1 秒租约、1.2 秒阻塞 collector 的故障注入中，第二个 Worker 可处理其余任务，但不能重领慢任务。

## 验证证据

- RunStore 契约：同一 owner 名下 token 1 在 token 2 reclaim 后 success 写回被拒绝；
- 两线程共享 SQLite/handler 竞争：七个 collection task 总领取七次、collector 唯一调用七次；
- 两个 `multiprocessing spawn` Worker 进程共享 SQLite/outbox：外部 side-effect log 七行且七行唯一；
- task DB success 后 ack crash：恢复后七类任务 collector 总调用仍为七次；
- runtime task generation 1/2 分别形成 succeeded outbox 行，新一轮恰好执行一次；
- 1 秒租约慢任务注入：heartbeat 跨过原始 expiry，两个 Worker 总 collector 调用仍为七次；
- backend-neutral renewal 契约由 SQLite 实跑、PostgreSQL 共用测试类覆盖；本机无 PostgreSQL 实例/镜像，因此 10 项 PostgreSQL integration 明确 skipped；
- 最终门禁：541 passed、10 skipped、2 subtests；Ruff 与 strict mypy 通过。
- Docker demo 跨容器：`run_4c0ac2cde0c340cb872b7f60024467cf`、Job `demo-ed945d861a4e`，Run SUCCEEDED、collection succeeded、20 Evidence objects；7 个 task 与 7 个 outbox message 均 attempts=1、fence=1、generation=1、lease 已释放；
- live Worker 使用已有依赖镜像的临时 source overlay，临时 Dockerfile 已删除；该镜像只用于验证，不作为 R5 发布资产。

## 残余风险与下一切片

1. collector 对 Slurm/共享目录的读取及 Evidence 写入仍需保持确定性和幂等；fencing/heartbeat 能压缩重复窗口，但不能撤销进程失联前已发生的外部 I/O；
2. heartbeat 续租失败目前进入 Worker task error，R4-3 仍需补长期累计指标与告警；
3. SQLite RunStore 仍承载 collection 业务表；PostgreSQL control parity 不等于 collection 领域 Store parity；
4. Agent execution 尚未接入 outbox/fencing；
5. 真实 107、VM 上传与部署、生产身份仍未触碰。
