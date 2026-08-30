# Agent Runtime Reliability Closure Design

- 日期：2026-08-31
- 状态：proposed for implementation review
- 范围：AgentSession、AgentTurn、pilot-agentd、AgentTask、Run、Evidence、Workspace 及其 Worker/Outbox 生命周期
- 目标环境：CPU-RC VM、vm-local Slurm；同一协议适用于真实 Slurm

本文是以下规格的可靠性补充，保持它们已经确认的边界：

- `2026-08-10-pi-hpc-agent-core-design.md`：持久控制面、短时 Pi Turn、AgentTask/Run/Evidence 和 Slurm 执行面；
- `2026-08-25-vm-slurm-pi-scientific-demo-closure-design.md`：VM-local Slurm authoritative facts、Evidence/Capsule 门禁；
- `2026-08-29-agent-turn-closure-design.md`：普通 Agent 表现、typed tools、receipt、工具错误和 64/128 安全上限；
- `2026-08-29-phase-aware-experiment-builder-design.md`：Builder facade、幂等 ChangeSet 和 validation schedule receipt。

本文对旧规格中未完全闭合的可靠性语义作出更具体的规定。若旧文档把
`schedule receipt` 误解为任务完成、把 64 个 Pi steps 当作立即失败，或允许
Workspace 只比较一次初始快照，则以本文为准。本文不改变 owner、capability、
审批、Slurm token、Evidence provenance 或“Pi 不获得通用宿主 shell”的安全边界。

## 1. 目标

本设计要使 Agent runtime 在进程、容器、Worker、网络和模型 provider 发生故障时，
仍能依靠持久控制面恢复，并且不会把非终态的中间 receipt 误报为成功结果。

具体目标如下：

1. 保留“持久 Session + 短时 Pi Turn + 共享 pilot-agentd + Slurm 瞬态作业”的架构。
2. 为 Turn、Outbox、ToolInvocation 和 AgentTask 增加可验证的 lease、heartbeat、fencing 和 stale reconciliation 语义。
3. 每一个完整且已持久化的 tool result 都形成可恢复 checkpoint；崩溃恢复不得重做已确认完成的有副作用操作。
4. 明确区分 schedule receipt、Run terminal、Evidence finalized 和最终 AgentTask result。
5. 让 AgentTask 只有在 Run 终态、Evidence 完成且完整性检查通过后才可进入 `completed`；VM closure 路径还必须等 Capsule READY。
6. 把 Workspace 的 immutable base snapshot、live revision/digest 和单写者 CAS 绑定到每次 patch、ChangeSet、Run 和 Evidence。
7. 通过 context compaction 控制 prompt 大小，但不删除 durable event、Evidence 或审计事实。
8. 把 64-step 从“硬失败点”改成“高位熔断、checkpoint、续接 Turn”；异常循环仍有明确的绝对终止条件。
9. 保证迁移可兼容已有 Session、Turn、Task、Run、Evidence 和 Workspace 数据，不通过删除数据实现升级。

## 2. 非目标

本文不做以下事情：

- 不把每个用户或 Session 绑定到一个常驻 Pi OS 进程；
- 不把 Slurm daemon、Slurm accounting 或节点本身改为应用数据库的替代品；
- 不让浏览器连接、HTTP request 或 SSE stream 承担长期任务的状态真源；
- 不提供通用 SSH、远程 shell、`sbatch`、`srun` 或 Slurm token 给 Pi；
- 不自动覆盖用户在 live workspace 中的新修改；
- 不把 stdout、完整历史消息或大文件强行放入模型 context；
- 不因为一次网络错误就推断外部副作用不存在；
- 不在本设计中扩大模型、工具、资源 envelope 或审批权限；
- 不用 fake model、预置数据库行或直接写入结果表代替端到端验收。

## 3. 保留现架构的理由

### 3.1 持久控制面

数据库是 Session、Turn、ToolInvocation、AgentTask、Run、Evidence、Workspace 和
Outbox 的权威状态。Worker 从数据库 claim 带 fencing token 的 lease，再调用
agentd；事件先 durable append，再发布浏览器 hint。这样浏览器断线、agentd 重启、
Worker 替换和 API 重启不会丢掉控制面事实。

### 3.2 瞬态 Pi 执行

`pilot-agentd` 是共享的受限服务。活动请求可以在进程内保存 abort controller，
但不能成为长期真源。每个活动 Turn/attempt 临时创建 Pi Agent，从 checkpoint
恢复消息和已完成工具，再在 Turn 或 durable AgentTask 边界释放。空闲 Session
对应零常驻 Pi 进程，水平扩展无需 sticky session，也不会因一个用户占用一个进程
而耗尽应用节点资源。

### 3.3 Slurm 瞬态执行面

Slurm 控制 daemon 可以作为长期运行的基础设施容器，MariaDB、日志和 public volume
也可以持久；“瞬态”指 allocation、job、step 和计算过程不是 Agent 的控制面状态。
RunStore 记录 submit intent、job receipt、数字 Job ID 和 lineage，Worker 通过
`squeue`/`sacct` 或等价 backend reconcile。已提交或状态不确定的 job 只能对账，
不能因 Agentd/Worker 重启而盲目再次创建。

### 3.4 为什么不采用每 Session 常驻 Pi 进程

常驻 Pi 进程会把会话持久性、进程调度、模型上下文、工具副作用和重启恢复混在
同一个内存对象中。它无法自然地解决容器迁移、进程 OOM、用户并发、版本升级和
长 Slurm job；还会让资源消耗随 Session 数而不是活动 Turn 数增长。持久数据库
加短时执行实例能把恢复边界明确放在 checkpoint、invocation receipt 和 Run/Evidence
事实之上，符合既有架构及安全隔离要求。

## 4. 总体生命周期

```text
HTTP/API
  │ create Session / submit Turn
  ▼
Durable DB + Outbox
  │ claim Turn lease + fencing token
  ▼
Python Worker
  │ heartbeat lease / invoke agentd
  ▼
pilot-agentd ── new Pi Agent for one bounded Turn/attempt
  │                 │
  │                 └─ Tool Gateway → durable invocation → domain operation
  │
  ├─ complete tool result → durable event + checkpoint
  ├─ high-water step → checkpoint + continuation Turn
  └─ schedule receipt → release Pi; no terminal task result yet

Worker/Runtime Reconciler
  ├─ AgentTask and Outbox stale claims
  ├─ Slurm Run: pending/running/terminal
  ├─ Evidence: collect → finalize → integrity_checked
  └─ optional VM Capsule: building → READY
```

每条边界都必须是可重放、可 fencing、可审计的数据库状态转换。通知只降低延迟，
不改变状态真源。

## 5. Schedule receipt 与 terminal evidence result

### 5.1 两种不同的结果

`schedule receipt` 是调度请求已经被控制面接受、持久化并具备后续对账条件的结果。
它不是科学结果，也不是 AgentTask completed。它至少包含：

```yaml
ScheduleReceipt:
  receipt_id: string
  task_id: string
  owner: string
  session_id: string
  originating_turn_id: string
  request_digest: sha256
  idempotency_key: string
  run_id: string
  submit_state: admitted | submitting | pending | submitted | submission_uncertain
  slurm_job_id: string | null
  resource_envelope_id: string
  workspace_revision: integer
  workspace_digest: sha256
  created_at: timestamp
```

调度工具返回 receipt 后，当前 Pi Turn 可以终止并释放 Pi。模型和 UI 必须使用
“已排队/等待运行/等待证据”等文案，不能使用“实验完成”“验证通过”或等价的终态文案。

`terminal evidence result` 是单独的、只能由 Worker/Runtime Reconciler 生成的结果：

```yaml
TerminalEvidenceResult:
  task_id: string
  run_id: string
  run_terminal_state: completed | failed | cancelled | orphaned
  evidence_state: finalized
  integrity_state: checked
  evidence_refs: [EvidenceRef]
  evidence_digest: sha256
  platform_snapshot_ref: SnapshotRef
  source_revision: string
  workspace_revision: integer
  workspace_digest: sha256
  capsule_ref: CapsuleRef | null
  capsule_state: READY | null
  terminal_at: timestamp
```

### 5.2 AgentTask 完成门禁

AgentTask 的状态机必须经过以下顺序：

```text
created
→ admitted
→ submitting
→ pending
→ running
→ awaiting_run_terminal
→ awaiting_evidence
→ awaiting_integrity
→ awaiting_capsule       (仅 VM closure)
→ completed
```

以下状态可从任何非终态进入，但仍需保留已有事实：

```text
input_required | cancelling | cancelled | failed | blocked | orphaned
```

成功任务进入 `completed` 的必要条件：

1. `Run.state` 是明确 terminal state；
2. Run 的 Job ID、Step accounting、ExitCode、stdout/stderr 引用已经记录；
3. Evidence 对象已 `collected`、`finalized`，并且 `integrity_checked=true`；
4. Evidence digest 与 Run、source revision、platform snapshot 和 workspace digest 绑定；
5. VM closure 任务额外要求关联 Capsule `READY`、manifest 完整且 digest 校验通过；
6. 这些条件在同一个最终化 transaction 中写入 Task outcome，或由带版本/CAS 的连续事务安全地完成。

只有 receipt 的 Task 必须保持 `pending`、`running` 或 `awaiting_*`，不得进入
`completed`。Run 成功但 Evidence 收集失败必须是 `awaiting_evidence` 或 `failed`，
不得伪造成功结果。Run 已终止但结果为失败、取消或 orphaned 时，必须在 Evidence
完成和完整性检查后进入对应的 Task `failed`、`cancelled` 或 `orphaned`；这些终态
不是成功的 `completed`。Evidence 成功但 integrity check 失败必须是 `failed` 或 `blocked`。

### 5.3 事件与通知

最小事件集合为：

```text
task_created
schedule_receipt_issued
run_submitted
run_state_observed
run_terminal
evidence_collected
evidence_finalized
evidence_integrity_checked
capsule_ready
task_completed
task_failed
```

其中 `schedule_receipt_issued` 是非终态事件；`task_completed` 只在所有门禁满足
后产生。重复事件以 `(aggregate_id, event_type, idempotency_key)` 去重，事件 sequence
仍必须连续且由 durable store 先写入。

## 6. Lease、heartbeat 与 stale reconciliation

### 6.1 通用 lease 字段

所有可被 Worker 处理的对象使用统一语义：

```yaml
Lease:
  lease_owner: string
  lease_expires_at: timestamp
  fencing_token: positive integer
  heartbeat_at: timestamp
  state_version: positive integer
```

claim 必须原子地递增 `fencing_token` 和 `state_version`，并设置 `lease_owner`、
`lease_expires_at` 与 `heartbeat_at`。任何 append、finish、cancel、retry 或外部
副作用写回必须同时匹配 owner、fencing token、state version 和未过期 lease。

### 6.2 ToolInvocation lease

ToolInvocation 的状态为：

```text
reserved → running → completed
                   └→ failed
                   └→ stale → reconciling → completed | failed | unknown
```

规则：

- handler 开始前先持久化 `reserved/running` 和唯一 `durable_operation_key`；
- handler 执行期间每不超过 lease 三分之一时间发送 heartbeat，默认间隔 5 秒，最小 lease 30 秒；
- stale 判定需要 `now > lease_expires_at + clock_skew_guard`，默认 guard 10 秒；
- stale reconciler 先按 durable operation key 查询领域 receipt、Slurm submit marker、ChangeSet journal 或传输记录；
- 找到已完成 receipt 就补写 `completed`，不能再次调用 handler；
- 找不到 receipt 时，只有 handler 明确声明“无外部副作用且可重试”，才允许重新执行；否则进入 `unknown` 并要求人工/领域 reconciler 继续判断；
- 旧 worker 即使恢复通信，也不能用旧 fencing token 覆盖新状态。

### 6.3 Handler durable operation key

每个有副作用的 typed handler 必须接收并持久化如下稳定 key：

```text
durable_operation_key =
  sha256(owner || session_id || turn_id || tool_name || tool_call_id ||
         canonical_arguments_digest || target_revision)
```

同一 key 只能对应一个 canonical arguments digest、一个 owner/session scope 和一个
领域操作。重复请求返回原有 receipt/result；相同 key 搭配不同内容返回稳定 conflict。
该 key 必须贯穿：ToolInvocation、ChangeSet publish journal、Run submit intent、
Slurm marker、Evidence finalization 和 Capsule build。外部系统不支持事务时，先写
intent/marker，再执行，之后通过 receipt reconciliation 收敛。

### 6.4 Turn heartbeat

Turn claim 后，Worker 必须在整个 `stream_durable_turn` 期间独立于模型事件发送
heartbeat，默认每 10 秒一次，或者在 `lease_remaining <= lease_duration / 3` 时立即续租。
续租失败时：

1. 停止向 agentd 发起新的工具调用；
2. 发送 cancel/abort（若连接仍可用）；
3. 以当前最后完整 checkpoint 将 Turn 转为 `interrupted`；
4. 让 Outbox 进入带退避的 pending/retry；
5. 由新 Worker 使用新 fencing token 恢复。

Turn lease 默认 120 秒，最大单次 agentd 请求 timeout 必须小于 Turn lease 的二分之一；
若配置更长 provider/tool timeout，必须同步提高 lease 并保留 heartbeat，不得只改 HTTP timeout。

### 6.5 Outbox heartbeat

Outbox message 在 dispatch 期间同样需要 heartbeat。Worker 处理一个长 Turn、Run submit
或 Evidence finalization 时不能依赖初始 outbox lease 覆盖整个执行时间。outbox heartbeat
必须匹配 message id、owner 和 claim token；lease 丢失时停止 ack/retry 原消息，避免旧 Worker
把新 Worker 的消息标记为 succeeded。

### 6.6 Stale reconciliation

Runtime Worker 每个 tick 必须处理：

- 过期 Turn lease；
- 过期 Outbox claim；
- 过期 ToolInvocation lease；
- `submission_uncertain` 的 Run；
- `awaiting_run_terminal`、`awaiting_evidence` 和 `awaiting_integrity` 的 AgentTask；
- Capsule 构建中断后的 manifest/receipt。

reconciler 必须先读外部事实，再决定 retry。对 Slurm 只使用数字 Job ID、submit marker、
accounting 和受限 metadata 对账；不得用“数据库没有 job_id”推断提交没有发生。

## 7. 完整 tool result checkpoint

### 7.1 持久化边界

只有 handler 已返回、Tool Gateway 已完成权限和 invocation 校验、ToolResult 已通过
schema/大小/secret-redaction 检查，才称为“完整 tool result”。此时在一个 durable
transaction 中完成：

1. 写入 `tool_call_completed` 事件；
2. 写入或确认 ToolInvocation terminal receipt；
3. 将该 invocation 的 result digest、result 摘要和副作用 receipt 加入 checkpoint；
4. 更新 Turn `final_checkpoint`、checkpoint digest、event sequence 和 state version；
5. 发布浏览器 hint。

部分 delta、started、progress、handler timeout 或未知外部状态不得进入 completed
invocation checkpoint。它们可以保留为事件，但恢复时必须视为未完成或未知。

### 7.2 Checkpoint 内容

```yaml
TurnCheckpoint:
  schema_version: integer
  session_id: string
  turn_id: string
  parent_checkpoint_digest: sha256 | null
  message_prefix_digest: sha256
  bounded_messages: Message[]
  completed_invocations:
    - tool_call_id: string
      durable_operation_key: string
      tool_name: string
      arguments_digest: sha256
      result_digest: sha256
      result_ref: string
      side_effect_receipt_ref: string | null
  workspace_revision: integer | null
  workspace_digest: sha256 | null
  active_task_refs: [string]
  usage: UsageSummary
  digest: sha256
```

恢复必须从最后一个完整 checkpoint 加载已完成 invocation，并通过 durable operation
key 查询其结果。若 provider 在重试时给出不同 tool call ID，服务端仍须优先使用
operation key 和参数 digest 识别已完成操作；无法安全匹配时必须停在 `unknown`，不能
猜测并再次执行。

### 7.3 跨 Turn checkpoint

Turn 结束、达到高位熔断或转换为 AgentTask 时，checkpoint 必须带 `parent_checkpoint_digest`。
续接 Turn 使用新 turn_id，但保留 session、workspace、task 和 operation lineage。旧
Turn 的 checkpoint、事件和错误不可变；新 Turn 只能追加新事件和新 checkpoint。

## 8. Workspace 版本、快照与单写者

### 8.1 三种不同概念

- `base_snapshot`：immutable、content-addressed、用于 ChangeSet 和审计的基线；
- `live_workspace_revision`：当前隔离 workspace 的单调递增 revision；
- `live_workspace_digest`：该 revision 对 manifest、文件内容和权限相关 metadata 的摘要。

base snapshot 一旦被 ChangeSet、Run 或 Evidence 引用，不得原地修改。新内容必须
生成新的 snapshot/revision/digest；snapshot digest 不是 live revision 的替代品。

### 8.2 Workspace data model

```yaml
Workspace:
  workspace_id: string
  owner: string
  project_id: string
  base_snapshot_id: string
  base_snapshot_digest: sha256
  live_revision: integer
  live_digest: sha256
  writer_owner: string | null
  writer_lease_expires_at: timestamp | null
  writer_fencing_token: integer
  state: active | conflicted | frozen | archived
```

```yaml
WorkspaceMutation:
  mutation_id: string
  durable_operation_key: string
  expected_live_revision: integer
  expected_live_digest: sha256
  resulting_live_revision: integer
  resulting_live_digest: sha256
  base_snapshot_id: string
  changeset_id: string
```

### 8.3 单写者与 CAS

一个 workspace 同时只能有一个有效 writer lease。patch、revert、publish、repair 和
ChangeSet finalization 必须携带 writer fencing token 以及
`expected_live_revision + expected_live_digest`。数据库 transaction 中执行：

```text
if workspace.state != active: conflict
if writer token/lease invalid: conflict
if live revision or digest != expected: workspace_conflict
apply mutation atomically
increment live_revision
recompute live_digest
append mutation journal
```

用户或另一 Session 在执行期间修改 workspace 时，旧 Agent 不得覆盖修改；它必须收到
结构化 `workspace_conflict`，包含新的 revision/digest 和可重新生成 patch 所需的
immutable base ref。自动 retry 只能重算，不得重复写入旧 patch。

Run、Evidence 和 Capsule 必须记录提交时的 base snapshot digest 及 live revision/digest，
以便确认结果到底来自哪一版代码。

## 9. Context compaction

### 9.1 原则

compaction 只改变下一次 Pi Turn 的输入，不删除 durable history。原始消息、事件、
ToolInvocation、Evidence、checkpoint 和错误保留在控制面，模型只收到有界摘要和可追溯引用。

### 9.2 保留层级

每个 Turn context 按以下顺序构造：

```text
system/security/profile rules
→ current user goal and explicit approvals
→ current Session/Project/Workspace state
→ latest checkpoint and unfinished invocation/task receipts
→ compacted decision/failure summary
→ bounded source/evidence windows with digest refs
```

至少保留：当前 goal、未完成任务、最近完整 tool result、workspace revision/digest、
Run/Task status、用户决定、失败 code、待处理 conflict 和下一步。旧消息压缩为带
`summary_digest`、source event range 和生成版本的 immutable ContextSummary。

### 9.3 压缩安全约束

- 不压缩 system/security/capability 规则；
- 不把摘要当作 Evidence 原文或代码真源；
- 每个摘要都绑定 Session、Turn、checkpoint digest、model/prompt version；
- 摘要生成失败时使用更小的原始窗口并记录 `context_compaction_failed`，不得发送越过字节上限的请求；
- workspace/source context 只使用指定 revision/digest 的片段；
- 4 MB checkpoint、单事件和 provider context 上限继续有效，超限进入结构化 failure。

## 10. Pi step 高位熔断与 checkpoint 续接

### 10.1 新语义

64 步不再是当前 Turn 的硬失败点。它是高位熔断阈值：达到阈值后，系统完成当前
完整 tool result，写 checkpoint，然后停止当前 Pi loop，创建续接 Turn。

```text
pi_steps < 48       normal
48 <= pi_steps < 64 high-water warning + compact context
pi_steps == 64      checkpoint + continuation_required + release Pi
```

当前 Turn 以 `turn_yielded` 事件结束。该事件不是 `turn_failed`，也不是 AgentTask
terminal result。续接 Turn 使用同一 Session、同一 Task/Project/Workspace lineage，
从最后 checkpoint 开始，不重复已完成 invocation。

### 10.2 绝对安全边界

为避免异常循环无限续接，单一用户请求的累计 Pi steps 设为 256；累计 provider calls
设为 24；累计同义 no-progress rejection 设为 8。达到任一绝对边界时：

1. 写入最后可用 checkpoint；
2. 产生明确 `turn_failed`，error code 为 `agent_runtime_budget_exhausted`；
3. 若存在未决有副作用 invocation，则先进入 `unknown` reconciliation，不报告成功；
4. 保留 continuation lineage，允许用户发起新请求，但不自动无限创建 Turn。

每个 Turn 的 Tool Gateway invocation 上限仍为 128，Sandbox command 和 ResourceEnvelope
上限不因此放宽。高位熔断只改变 Pi step 的恢复语义，不改变权限和资源预算。

## 11. 数据模型与状态机

### 11.1 AgentSession

```yaml
AgentSession:
  session_id: string
  owner: string
  state: idle | queued | running | blocked | archived
  state_version: integer
  context_checkpoint_ref: string
  context_summary_ref: string | null
  active_turn_id: string | null
  active_task_refs: [string]
  workspace_id: string | null
  resource_usage: UsageSummary
  created_at: timestamp
  updated_at: timestamp
```

Session state 不替代 Turn/Task/Run state。Session 只有在关联对象状态发生合法变化
后通过 CAS 更新；事件重放不能把 Session 从 running 直接改为 completed。

### 11.2 AgentTurn

```yaml
AgentTurn:
  turn_id: string
  session_id: string
  owner: string
  parent_turn_id: string | null
  state: queued | running | interrupted | yielded | completed | cancelled | failed
  state_version: integer
  pi_version: string
  model_profile: string
  prompt_version: string
  tool_schema_version: string
  lease: Lease | null
  event_sequence: integer
  pi_steps: integer
  provider_calls: integer
  checkpoint_ref: string | null
  checkpoint_digest: sha256 | null
  continuation_required: boolean
  error: ErrorEnvelope | null
```

### 11.3 AgentTask

除第 5 节的状态外，Task 必须保存 `schedule_receipt_ref`、`run_id`、`evidence_refs`、
`evidence_digest`、`integrity_checked_at`、`capsule_ref`、`capsule_state`、
`durable_operation_key`、lease 和 `reconciliation_attempt`。Task outcome 不得只由
agentd 的 HTTP response 设置。

### 11.4 Run/Evidence/Capsule

- Run：保存 submit intent、durable operation key、Slurm Job ID、submit response、state、ExitCode、Step accounting 和 provenance；
- Evidence：保存 source revision、workspace digest、platform snapshot、Run refs、原始对象 refs、manifest digest、finalization/integrity timestamps；
- Capsule：保存可复现输入、代码、配置、结果和 manifest；不保存 credential；只有 manifest、对象和 digest 都可读且校验通过时为 `READY`。

## 12. 失败模式与恢复策略

| 失败 | 持久状态 | 恢复动作 | 禁止行为 |
|---|---|---|---|
| 浏览器断线 | Turn/Task 继续 running/pending | 重连读取 durable events/receipt | 取消后台 Turn |
| agentd 调用前退出 | Turn lease 可过期，checkpoint 不变 | 新 Worker 从 checkpoint 重试 | 伪造 tool result |
| tool 完成后 agentd 退出 | completed invocation + checkpoint | 通过 operation key 重放 result，不重做副作用 | 仅按新 toolCall ID 再执行 |
| Worker 在 stream 中退出 | Turn/Outbox lease 过期 | stale reconcile 后新 fencing claim | 旧 Worker ack/finish |
| heartbeat 续租失败 | interrupted 或 stale | abort、checkpoint、retry | 继续写旧 lease |
| handler 返回未知错误 | invocation unknown/reconciling | 查 receipt/marker，再决定 | 假设没有副作用 |
| Slurm submit response 丢失 | submission_uncertain | marker/job/accounting 对账 | 直接第二次 submit |
| Run 完成但 Evidence 未收齐 | Task awaiting_evidence | 继续收集/finalize/integrity check | Task completed |
| Evidence digest 校验失败 | Task failed/blocked | 保留原始对象，重新验证或人工处理 | 修改已 finalized Evidence |
| Workspace revision 变化 | workspace_conflict | 读取新 revision，重算 patch | 覆盖 live 文件 |
| context compaction 失败 | context_compaction_failed | 发送更小有界窗口 | 发送全历史 |
| 64 steps | Turn yielded + checkpoint | 创建有限续接 Turn | 标记 Turn failed |
| 累计 256 steps | Turn failed + checkpoint | 等待用户新请求 | 无限续接 |
| Capsule 构建中断 | Task awaiting_capsule | 通过 manifest receipt 恢复 | 直接报告 VM closure 完成 |
| 模型 provider 不可用 | Turn interrupted 或 failed | 重试受限 provider call，保留 checkpoint | 重复工具副作用 |

## 13. 迁移兼容

迁移采用 additive-first 方式，禁止删除或重写历史事实：

1. 新增 lease heartbeat、fencing/state version、operation key、checkpoint digest、workspace live revision/digest、Task evidence gate 和 Capsule state 字段；
2. 旧 `AgentTurn` 中已有 `lease_expires_at`、`fencing_token`、`state_version` 和 `final_checkpoint` 直接保留，`heartbeat_at` 初始取 `updated_at`，operation key 由旧 turn/request/tool identity 的 canonical digest 派生；
3. 旧 Task 只包含 schedule receipt 时迁移为 `awaiting_run_terminal`，不得迁移为 completed；
4. 已有 Run terminal 且已有 Evidence 引用的记录进入一次性 integrity reconciliation；校验通过才补写 `evidence_integrity_checked`；
5. 缺少 workspace live revision 的旧 workspace 从 immutable base manifest 建立 revision 1 和 digest；无法可靠计算 digest 时进入 `conflicted`，不自动发布；
6. 旧 20/64 step 失败记录保持原始 outcome；新执行使用本文的 64 high-water/256 absolute cap；
7. 读路径同时接受旧字段和新字段，写路径只产生新 schema；所有 event payload 带 `schema_version`；
8. migration 完成前，旧 worker 只可处理兼容的 queued/pending 对象；新 worker 发现缺少关键 gate 字段时先执行 backfill/reconciliation；
9. 回滚应用代码不会删除新列、事件或 receipt；回滚版本只能把未知新状态显示为安全的 `blocked`，不能把它显示为 completed。

## 14. 测试与验收

### 14.1 数据库和状态机测试

- 同一 owner/session/turn/tool content 的 operation key 重放返回同一 receipt；内容变化返回 conflict；
- 旧 fencing token、旧 state version、过期 lease 的 append/finish/ack 全部被拒绝；
- Turn、Outbox、Invocation heartbeat 可在 lease 到期前续租；到期后新 worker 可 reclaim；
- stale reconciler 找到外部 receipt 时只补写状态，不重复 handler/Slurm submit；
- `schedule_receipt_issued` 永远不能直接产生 Task completed；
- Run terminal 但 Evidence 未 finalized 时 Task 仍不可完成；integrity failure 和 Capsule 非 READY 的状态符合门禁；
- event sequence 连续、重放幂等，浏览器 hint 丢失不影响 durable history。

### 14.2 Turn/agentd 测试

- agentd 重启前后从最后完整 checkpoint 恢复；每个完整 tool result 都可从 checkpoint 读取；
- 在 tool completed event 后、terminal event 前模拟 agentd crash，恢复不重复有副作用 handler；
- 模拟 provider 返回不同 tool call ID 时，operation key 能安全匹配；无法匹配时进入 unknown 而不是重执行；
- stream 持续超过初始 lease 时 heartbeat 仍保持同一 claim；heartbeat 失败后旧 worker 不能写回；
- 64 steps 产生 `turn_yielded`、checkpoint 和一个 continuation Turn；累计 256 steps 产生 `agent_runtime_budget_exhausted`；
- context compaction 保留 goal、最新 checkpoint、未决 Task、workspace revision/digest 和失败摘要，不越过 context/事件字节上限；
- API/SSE 只显示 schedule receipt 的非终态语义，只有 terminal evidence result 才显示完成。

### 14.3 Workspace 测试

- immutable base snapshot 被引用后不可修改；每次成功 mutation 只生成新的 live revision/digest；
- 两个 writer 同时 CAS 同一 revision 时恰有一个成功，另一个得到 `workspace_conflict`；
- 用户 live 修改后旧 ChangeSet、repair 和 publish 全部拒绝覆盖；
- Run/Evidence/Capsule 均保存同一提交边界的 base snapshot 和 live digest；
- patch、publish journal 和 handler retry 使用同一 durable operation key 不重复写文件。

### 14.4 VM/Slurm 端到端验收

在同一不可变发布 revision 上执行：

1. 自然语言 Agent Turn 生成工作区和 ChangeSet；
2. Sandbox/validation 成功后 UI 获得 schedule receipt，Pi 释放，AgentTask 仍为 pending/awaiting；
3. Worker/Runtime Watch 从 receipt 对账到唯一数字 Slurm Job ID；
4. `squeue` 运行期和 `sacct` 终态引用同一 Run/Job；
5. Run terminal 后 Evidence 被收集、finalized、integrity checked；
6. VM closure 路径构建 Capsule，只有 Capsule READY 后 AgentTask 才 completed；
7. 在 agentd、Worker、API 容器分别重启并重新执行 tick，结果不重复提交、不丢事件、不丢 Evidence；
8. 浏览器断线、HTTP 重试、重复 schedule request 和 stale Worker 均产生相同最终事实；
9. 导出的 Evidence/Capsule 绑定同一 source revision、workspace digest、platform snapshot 和 Slurm Job ID；
10. 任何关键门禁失败都报告结构化 failure，不用预置记录或 fake terminal result 代替。

## 15. 可观测性与完成定义

每个 Turn/Task/Invocation/Run 都记录：claim/release/heartbeat、fencing conflict、
stale reconciliation、checkpoint digest、continuation count、operation key digest、
schedule-to-terminal latency、terminal-to-evidence latency、integrity latency、
workspace conflict 和 context compaction 统计。日志和 metrics 不得包含 token、MFA、
完整 capability 或用户 credential。

本设计完成的判据是：

- 空闲 Session 无常驻 Pi 进程；
- agentd/Worker 重启后可从 durable checkpoint、receipt 和 reconciliation 继续；
- 任意完整 tool result 都有可验证 checkpoint 和 operation key；
- 长 Slurm 任务只占用 AgentTask/Run/Reconciler，不占用 Pi Turn；
- schedule receipt 从不伪装 terminal evidence result；
- Task completed 必须满足 Run terminal、Evidence finalized、integrity checked，VM closure 还满足 Capsule READY；
- 64-step 触发 checkpoint 续接，异常累计预算仍能安全失败；
- Workspace 永远以 immutable base + live revision/digest + 单写者 CAS 保护；
- 旧数据可迁移、可回滚读取，且不删除用户历史或外部事实。
