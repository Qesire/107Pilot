# Phase 3G Agent Execution Outbox / Fencing 审查

日期：2026-07-18
范围：Agent approved action prepare/submit、durable outbox、execution phase fencing、Worker 接管、崩溃恢复和多进程竞争。
结论：本 Agent execution 切片 P0/P1 已清零；R4-2 的 Run submit、collection 与 Agent execution 均已接入统一 control substrate。下一阶段为 R4-3。

## 固定的业务契约

- production execute API 先写 `agent.execute`，随后立即领取只用于保持现有同步 HTTP 语义；API enqueue 后退出时 Worker 可独立接管；
- message ID 为 `agent:<execution_id>:prepare|submit`；prepare-only 后再请求 submit 不会被旧 succeeded message 吞掉；
- execution 行保存 `execution_phase`、`execution_owner`、`execution_fencing_token`；terminal write 必须同时匹配三者；
- prepare 与 submit 的 outbox token 可各自从 1 开始，因此 phase 是 fencing identity 的必要组成部分；旧 prepare writer 不能覆盖已进入 submit phase 的执行；
- 同 phase token 2 reclaim 后，即使 worker ID 与 token 1 相同，旧 writer 仍被拒绝；
- derived contract/run ID 由 execution ID 确定，崩溃重放只会命中同一领域对象；真正 Slurm submit 由 `run.submit` outbox、per-run marker 和 Run fencing 独立保护；
- execution 已 submitted/failed 后 replay 直接返回原终态；源 Run 后续变化不破坏已完成请求的幂等响应；
- production Worker health 与 CLI exit gate 纳入 Agent execution checked/succeeded/errors；无 control repository 的嵌入式兼容路径暂保留旧同步逻辑。

## Findings-first 结果

### 已修复 P1：旧 execution reclaim 只依赖五分钟 `updated_at`

旧 `claim_agent_action_execution` 会在 executing 超过五分钟后按状态重领，没有 owner/token 条件写回；旧进程恢复后仍可把新实例结果覆盖为 prepared、submitted 或 failed。production path 现在从 outbox token 建立 execution fence，所有中间/终态更新均做条件 UPDATE；同 owner token 1 在 token 2 接管后被 `AgentExecutionFenceConflict` 拒绝。

### 已修复 P1：prepare/submit 两个独立消息的 token 不具备全局单调性

prepare message 与 submit message 都可能得到 fencing token 1。若 execution 只保存裸 token，旧 prepare 与当前 submit 无法区分。现在 execution phase 与 token 联合作为 fence；submit claim 会把行原子切换到 submit phase，之后 prepare phase 写回即使 token 数值相同也失败。

### 已修复 P1：Run/Agent batch 预领取使未开始消息提前过期

原 dispatcher 一次领取整个 batch 再串行调用外部 backend。首个慢调用可能让后续消息在真正开始前过期并被其他 worker 重领。Run submit 与 Agent execution 现都每次只 claim 一条，处理完成后才领取下一条；collection 已在前一切片采用同一规则并带 heartbeat。

### 已修复 P1：终态 replay 被后续源 Run 变化误判为 stale

重构草案先做 source freshness 检查再读取 execution 终态；已经成功的幂等请求可能因后续 source event 返回 `AGENT.APPROVED_ACTION_STALE`。现在 submitted/failed execution 优先作为既成事实返回；只有尚未完成的 prepare/submit 才重新验证证据与 source timestamp。

## 验证证据

- prepare→submit：两个 phase message 分别 succeeded，单一 execution、单一 derived contract/run、backend submit 一次；
- enqueue-only：API 不领取 message，独立 dispatcher 完成 submitted；
- 两线程共享 SQLite：Agent 外部 submit side effect 恰好一次；
- 两个 `multiprocessing spawn` 进程共享 SQLite：side-effect log 恰好一行，per-run marker 唯一；
- execution DB terminal write 后 ack crash：租约回收只补 ack，backend submit 总数仍为一；
- phase fence：prepare token 1 不能覆盖 submit token 1；同 phase/同 owner token 1 不能覆盖 token 2；
- RuntimeReconcileWorker 明确报告 Agent checked=1、succeeded=1、errors=0；
- terminal replay 在 source Run 后续更新后仍返回原 submitted execution；
- 全量门禁：550 passed、10 PostgreSQL integration skipped、2 subtests；Ruff 与 strict mypy 通过；
- Docker enqueue-only 跨容器：API 只写 pending message，Worker `runtime-worker-9986098a76cd` 以 phase=submit/fence=1 完成 execution `agentexec_a7f75f2668a36d762af1d519bd87e0ec`；
- 派生 Run `run_agent_55f45c5355509bdfb80b0b1517c2352e`、Job `demo-7b11bc668738` 最终 SUCCEEDED；Agent、Run submit、7 个 collection outbox 均 attempts=1/fence=1/lease released；
- live API/Worker 使用已有依赖镜像的 source overlay；临时 Dockerfile/脚本已删除，不作为 R5 发布资产。

## 残余风险与 R4-3 输入

1. Agent 当前 message lease 为 60 秒且单条领取；派生写入确定性、外部 submit 另有 Run fence，但 R4-3 仍需为慢 execution 增加 renewal 指标/故障注入；
2. Worker health 只有最近 tick JSON，缺长期累计 Prometheus/trace；
3. SQLite 仍承载 Agent/Run 领域表；PostgreSQL control parity 不等于完整业务 Store parity；
4. legacy 无 control repository 路径仍按旧五分钟 reclaim，仅用于测试/嵌入式兼容，不是 production builder；
5. 真实 107、VM 上传/部署与生产身份未触碰。
