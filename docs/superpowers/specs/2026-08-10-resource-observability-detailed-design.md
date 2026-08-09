# 107Pilot 平台动态状态、作业资源观测与 Agent 取证详细设计

- 日期：2026-08-10
- 状态：逐节设计已确认；实现未开始
- 上位规格：`2026-08-09-resource-observability-design.md`
- Agent 上位设计：`2026-08-10-pi-hpc-agent-core-design.md`
- 环境边界：先在本地 Docker Slurm simulator 验证；远程 VM 当前不可用且不作为前置条件
- 前端边界：本规格只定义后端事实、采集、API 和 Agent 工具契约；不设计 Dashboard，也不迁就现有 Dashboard 数据形状

## 1. 背景与细化结论

现有系统已经有 `PlatformSnapshot`、owner-scoped read model、基础 Resource Dashboard、Run 状态对账、终态 Evidence、Diagnosis 和 Pi Agent Core 总体设计，但仍存在四个结构性缺口：

1. 现有平台采集运行在 API 进程中，每个进程独立启动、没有 leader election，约 5 分钟保存一次整快照；浏览器每 20 秒读取不能等同于 20 秒采集。
2. `PlatformSnapshot` 同时承载能力事实、节点、队列和命令观测，若直接扩展为高频时序会造成整块 JSON 重复、作用域混杂和写放大。
3. 当前 simulator 使用 `JobAcctGatherType=jobacct_gather/none`，能够验证 SlurmDBD、状态和部分 allocation accounting，但不能证明 `sstat`、CPU、RSS 或 I/O 资源用量。
4. Agent 虽能读取 Run、日志和 Diagnosis，却缺少充分、可追溯的资源事实，容易把缺失值当成零、把多 task `MaxRSS` 当成作业峰值，或只复述一个缺少底层依据的“利用率低”标签。

本设计采用：

> 类型化观测管线、每连接单租约采集、数据库产品读模型、短期有界时序、不可变终态 Summary，以及面向 Pi Agent Core 的丰富 owner-scoped 取证工具。

Prometheus 和 Slurm 25.11 OpenMetrics 可作为数据源和运维出口，但不是 107Pilot 产品事实的唯一真源。浏览器、Agent 和 SSE 都只读取 107Pilot Store，不直接放大 Slurm 查询。

## 2. 方案比较与选择

### 2.1 方案 A：继续扩展整块 `PlatformSnapshot`

改动最小，但 20 秒保存全部节点和作业会制造大量重复 JSON；能力、平台、账号和 Run 的频率、权限和保留周期也不同。拒绝作为正式动态结构。

### 2.2 方案 B：类型化观测管线与产品读模型（采用）

保留现有快照的低频兼容职责，新增独立的 PlatformPulse、AccountPulse、RunResourceSample 和 RunResourceSummary。每个 `connection_id` 只有一个持租约 Worker；字段级保留 source、时间、质量和覆盖率；短期样本降采样，终态总结固化为 Evidence。

该方案能在本地 simulator 完成闭环，也能直接为 Agent 提供平台、账号、活动 Run、accounting 和历史比较事实。

### 2.3 方案 C：Prometheus 优先

Prometheus TSDB、recording rules 和 Grafana 已成熟，但 Slurm metrics 需要管理员配置、可信网络和信息披露治理，也不能天然绑定 107Pilot Contract、Run、Evidence、owner 和审批。保留为未来 `ObservationSourceAdapter` 和运维设施，不作为比赛首版硬依赖。

## 3. 系统边界与不变量

```text
Slurm CLI / REST / OpenMetrics
              │
              ▼
      ObservabilityWorker
       per-connection lease
              │
              ▼
    normalized Product Store
       │       │       │
       ▼       ▼       ▼
      API   Pi Agent  Runtime Watch
```

必须始终成立：

1. API、浏览器、SSE 和 Agent 请求不得直接触发 Slurm 命令。
2. Observability Collector 只有固定白名单只读能力，不复用提交、取消或文件写入权限。
3. Agent 在当前学生授权范围内拥有丰富只读信息；丰富读取权限不扩大 Action Tool 权限。
4. 采集失败不得改变 Run 主状态，也不得用全零样本覆盖最近成功事实。
5. 缺失、未支持、权限不足、覆盖不足、非法和陈旧必须明确区分。
6. 每个派生值都能追溯到 source operation、采集周期、字段口径和能力版本。
7. 其他学生的作业名称、ID、路径、日志和账号不可进入当前学生或其 Agent 上下文。
8. 当前学生自己的作业名、路径、脚本名、资源字段和日志不做无必要脱敏；认证凭据仍必须清除。
9. 短期样本可按策略清理；已发布 Summary、Evaluation 和 Evidence 不得原地改写。

## 4. 事实分层、作用域与兼容迁移

### 4.1 `ClusterCapabilitySnapshot`

低频、相对稳定的连接级能力事实：

- 分区、QoS、TRES/GRES；
- CPU/GPU 总容量和最大 walltime；
- accounting、GPU accounting、REST、OpenMetrics 和字段支持；
- Slurm、API、collector 和 parser 版本；
- 可见性和 PrivateData 行为；
- 运行环境能力和已知限制。

默认每 5 分钟 probe，也在连接建立、认证恢复、profile revision 和来源版本变化时执行。

### 4.2 `PlatformPulse`

快速连接级聚合，不包含其他学生作业明细：

- 节点状态分布；
- CPU、GPU 和可证明的内存总量/已分配量；
- 队列状态聚合；
- 有权限获得时的 pending reason Top N；
- 分区动态摘要；
- partial、freshness、采集窗口和安全警告。

默认目标频率为 20 秒，但受连接预算约束。

### 4.3 `AccountPulse`

当前学生账号视角：

- managed/external 作业数量；
- pending/running/completing 分布；
- 当前分配 CPU/GPU/内存；
- pending requested resources；
- 本人的 pending reasons；
- 与本周期一致的 `AccountJobObservation` 子记录。

作用域为 `owner + connection_id`，与对应 `PlatformPulse` 共享 `cycle_id`。

### 4.4 `RunResourceSample`

活动 Run 的 job/step 资源样本：

- CPU 时间；
- RSS；
- I/O；
- allocation 和 step TRES；
- 可选 GPU 指标；
- 采样覆盖、来源连续性和质量。

默认目标频率为 30 秒，按连接批量采集，不能为每个 Run 启动独立无限循环。

### 4.5 `RunResourceSummary`

终态长期事实，固定分为：

```text
requested  ← 冻结 Contract
allocated  ← Slurm allocation/accounting
used       ← sstat/sacct/已验证 metrics
timing     ← Slurm Submit/Eligible/Start/End
quality    ← 完整度、覆盖率、来源和警告
```

短期样本可以清理，Summary、评价和 Evidence 随 Run 长期保留。

### 4.6 作用域

| 对象 | 主作用域 | 用户明细 |
|---|---|---|
| CapabilitySnapshot | `connection_id` | 无 |
| PlatformPulse | `connection_id` | 无其他学生明细 |
| AccountPulse | `owner + connection_id` | 仅当前学生 |
| RunResourceSample | `owner + run_id + attempt` | 仅当前学生 |
| RunResourceSummary | `owner + run_id + attempt` | 仅当前学生 |

同一学生在终端提交的作业可以作为 external job 进入 AccountPulse，也可以被 Agent 解释，但不能伪造 Contract、Run、Evidence 或自动修复链。

### 4.7 兼容迁移

- 现有 `PlatformSnapshot` 表和 `/api/v1/platform/snapshots*` 保留。
- 它继续承担低频能力快照和既有 preflight 的兼容职责，不承载 20 秒动态时序。
- 新接口使用 `/api/v1/observability/*`。
- 新 Worker 启用后，必须关闭旧的“每 API 进程独立采集”线程，禁止双重访问 Slurm。
- 旧快照不回填为伪动态样本。
- 现有 Dashboard 在未来重写前可以继续读取旧接口，但本规格不为其数据形状妥协。

## 5. 采集 Worker、数据源与负载控制

### 5.1 独立 Worker

采集从 API 进程下沉到独立 `ObservabilityWorker`。API replicas 只读取数据库；多个 ObservabilityWorker 可以部署以实现故障接管，但同一连接同时只能有一个有效采集者。

租约键：

```text
resource_kind = observability_connection
resource_id   = connection_id
```

建议默认租约 45 秒、每 15 秒续租。获取或接管租约时递增 `fencing_token`；每个 cycle 保存 token，Store 提交时再次验证。旧 Worker 即使完成远端命令，在失去租约后也不得写入。

租约复用现有 `ControlRepository`，不实现第二套锁服务。

### 5.2 四条调度通道

| 通道 | 触发 | simulator 默认 |
|---|---|---:|
| capability | 周期、连接变化 | 5 分钟 |
| platform/account pulse | 周期 | 20 秒 |
| active Run samples | 活动 Run 周期 | 30 秒 |
| terminal accounting | Run 终态事件 | 立即 + 有限重试 |

每个真实连接必须配置：

```text
minimum_interval
max_commands_per_minute
max_concurrent_requests
command_deadline
batch_size
failure_backoff
```

20/30 秒是本地目标，不是对真实集群无条件高频调用的承诺。远程环境恢复后必须按管理员允许预算调整。

默认 freshness 窗口：

| 通道 | fresh | stale | expired |
|---|---:|---:|---:|
| capability（5m interval） | ≤ 10m | 10–30m | > 30m |
| platform/account（20s interval） | ≤ 45s | 45s–5m | > 5m |
| active Run（30s interval） | ≤ 75s | 75s–5m | > 5m |

真实连接修改 interval 时必须同时保存显式 freshness policy，不能只改调度周期而继续沿用不匹配的 TTL。Summary 是不可变终态事实，不按动态 TTL 过期；它的 quality 由 accounting completeness 表达。

### 5.3 `ObservationCycle`

单次周期：

```text
scheduled
→ claim/renew lease
→ freeze connection profile revision
→ build field-level source plan
→ batch remote reads
→ normalize and validate
→ commit cycle + samples atomically
→ publish summary event
```

记录至少包含：

```text
cycle_id
connection_id
lane
profile_revision
fencing_token
scheduled_at / started_at / completed_at
source_plan
command_count
duration_ms
status: complete | partial | failed | skipped_budget
warnings
```

`partial` 允许可信字段继续使用；`failed` 不生成伪样本；`skipped_budget` 表示主动遵守预算，不算远端错误。

### 5.4 Source Adapter

```text
ObservationSourceAdapter
├─ SlurmOpenMetricsAdapter
├─ SlurmRestObservationAdapter
└─ SlurmCliObservationAdapter
```

来源优先级按字段决定，而不是整周期只选一个来源：

1. 已通过 capability probe 的 Slurm OpenMetrics；
2. 已验证版本的 slurmrestd；
3. 固定 argv 的 CLI；
4. unavailable。

典型映射：

- 平台节点/队列聚合可来自 OpenMetrics；
- 当前学生作业明细可来自 owner 限制的 `squeue`；
- 活跃 Run 使用 `sstat`；
- 终态 Run 使用 `sacct`。

不同来源的字段必须保留 provenance，不能被无标记地拼成“精确同时刻”快照。

### 5.5 批量和公平性

- 同一账号活动作业用一次受限 `squeue -u <cluster_user>`。
- 多个活动 Run 的 `sstat` job IDs 按连接和批次合并。
- 终态 `sacct` 可以合并近期待固化 job IDs，但每条记录独立归属。
- 一个 Run 不得在单个 tick 中耗尽整个连接预算。
- terminal accounting 最高优先；Runtime Watch 已发现资源压力的 Run 次优先；普通活动 Run 公平轮转。
- API、SSE、浏览器和 Agent 工具调用不增加远端调用次数。

### 5.6 失败和退避

- 单命令失败：周期可为 partial。
- 认证失效：通道进入 `auth_required`，停止密集重试。
- 远端超时：保留最后样本，指数退避至最多 5 分钟。
- capability 证明字段 unsupported 后，在下一次慢速 probe 前不重复尝试。
- 恢复时逐级缩短间隔并加入 jitter，避免请求洪峰。
- terminal accounting 使用独立预算，不被平台 pulse 失败阻塞。

## 6. 数据模型、时间一致性与保留

### 6.1 `ObservedMeasure`

```text
value
unit
availability
source_adapter
source_operation
captured_at
quality
coverage
warning
```

`availability`：

```text
available
unsupported
permission_denied
not_collected
insufficient_coverage
invalid
```

`freshness` 在读取时按通道 TTL 计算：

```text
fresh | stale | expired | unknown
```

这样不会把“不支持”和“曾经支持但已陈旧”混为一类。

规范单位：CPU 核/CPU 秒、bytes、seconds、0–1 ratio、GPU 数量。原始单位保留在 provenance；解析失败标记 invalid，不猜测。

### 6.2 持久对象

```text
observation_cycles
cluster_capability_versions
platform_pulses
account_pulses
account_job_observations
run_resource_samples
run_resource_accumulators
run_resource_summaries
resource_evaluations
observability_minute_buckets
```

使用显式核心列和带 schema version 的规范 payload，不建立允许任意 metric name/label 的高基数通用表。

### 6.3 幂等 ID

```text
platform_pulse_id = hash(cycle_id, connection_id)
account_pulse_id  = hash(cycle_id, connection_id, owner)
run_sample_id     = hash(cycle_id, run_id, job_instance_key)
```

`cycle_id` 由持久调度消息产生，同一次重试不变。重复写相同内容幂等；相同 ID、不同内容视为 fencing 或实现错误并拒绝。

### 6.4 作业实例身份

仅 `run_id + job_id` 不足以抵御 requeue、重提和 Job ID 复用。定义：

```text
job_instance_key
├─ connection_id
├─ cluster_id
├─ job_id_raw
├─ submit_time
├─ sluid / original_sluid（可用时）
└─ run_attempt
```

没有 SLUID 时至少使用 `connection_id + job_id + submit_time + run_attempt`。终态 accounting 必须先匹配冻结身份。

### 6.5 非原子时间窗口

每条 pulse/sample 都包含：

```text
cycle_id
window_started_at
window_ended_at
observed_at
max_source_skew_ms
```

平台和账号可共享 cycle，但字段保留各自 source/captured_at。偏差超过阈值时标记 partial。跨周期复用旧字段必须标记 `carried_forward` 和原始周期，不能静默拼接。

### 6.6 Capability semantic digest

每次 probe 都产生轻量 cycle；规范 capability payload 计算 semantic digest。内容未变时只更新最近确认引用；变化时创建新的不可变 capability version。Preflight 和 Agent 同时引用 version 与最近确认时间。

### 6.7 `RunResourceAccumulator`

活动 Run 保存在线有界累加器：

```text
sample_count
first_sample_at / last_sample_at
cpu_time_first / cpu_time_last
max_rss_observed
io_read_first / io_read_last
io_write_first / io_write_last
gpu_coverage_duration
gpu_util_weighted_sum
source_discontinuities
invalid_sample_count
version
```

它保存可复核聚合，保证原始样本清理后仍能形成 Summary。更新使用乐观版本或事务锁并验证 fencing token。

### 6.8 保留和降采样

| 数据 | 首版保留 |
|---|---:|
| 原始 Platform/Account/Run 样本 | 2 小时 |
| 1 分钟聚合 | 24 小时 |
| ObservationCycle 元数据 | 7 天 |
| Capability 变更版本 | 长期 |
| Run accumulator | 活动期至 Summary 固化 |
| Run Summary/Evaluation | 随 Run/Evidence 长期 |

Retention Worker 使用独立租约；必须先成功生成 minute bucket 才能删 raw；活动周期、尚未固化 Summary 的终态 Run 和 Evidence 引用对象不得删除。空间不足时优先停止新增高频样本，不能删除长期 Summary/Evidence。

## 7. 指标语义与 Slurm 记录归并

### 7.1 PlatformPulse

允许字段：

```text
node_counts_by_state
cpu_total / cpu_allocated
gpu_total / gpu_allocated
memory_total / memory_allocated（来源可靠时）
jobs_by_state
pending_reason_top
partition_dynamic_summary
```

`total - allocated` 只能叫“未分配量”，不能叫“当前可调度量”。不根据节点总内存减作业内存猜测可调度内存。只能看到本人队列时，平台级 pending reason 为 unavailable，不能用本人数据冒充全局。

节点同时保存 `state_raw`、`base_state`、`state_flags` 和 `normalized_state`。归一化优先级：down/fail/not-responding，draining/drained，completing，allocated，mixed，idle，unknown。原始 flags 始终保留。

### 7.2 AccountPulse

账号聚合固定区分：

```text
managed_jobs_by_state
external_jobs_by_state
allocated_resources_running
requested_resources_pending
pending_reasons
```

`AccountJobObservation` 可包含当前学生的 job instance、job ID、名称、managed/external、run_id、状态、partition/QoS、Submit/Eligible/Start、pending reason、ReqTRES 和 AllocTRES。

`connection_id + job_instance_key` 匹配 Run 才标记 managed；明确属于当前 cluster_user 但未匹配的是 external；无法证明 owner 的记录不进入账号明细。

### 7.3 活跃 Run 与 `sstat`

`sstat` 主要返回 step 数据，故每个 Run 样本包含 `StepResourceObservation[]`：

```text
step_id
step_kind: batch | extern | numbered | unknown
ntasks
alloc_tres
ave_cpu
max_rss / max_rss_task / max_rss_node
tres_usage_in / tres_usage_out
captured_at
```

规则：

- 使用 `--allsteps` 和固定字段集合。
- 没有 step 时返回 not_collected，不返回零。
- `MaxRSS` 是 step 内单 task 最大 RSS，不是整个作业峰值。
- 多 step 只能取最大并标为 `max_single_task_rss`。
- `AveCPU × NTasks` 只有在 probe 验证字段完整且 step 无重叠时才可用于累计估计。
- batch、extern 和 numbered steps 不得无条件求和。
- I/O 只有 total 口径能形成累计量；Ave/Max 保持原口径。
- 活跃数据主要服务趋势和压力检测，不直接形成最终效率结论。

每个派生值必须保存 `aggregation_method`、included/excluded steps 和 `scope=task|step|allocation|job`。

### 7.4 终态 `sacct`

必须同时读取 allocation row 和 step records，不能使用只读 allocation、导致利用率为零的模式。至少采集：

```text
JobIDRaw / SLUID / OriginalSLUID
Submit / Eligible / Start / End
State / ExitCode
ElapsedRaw / TimelimitRaw
ReqTRES / AllocTRES / ReqMem
AllocCPUS / NTasks
TotalCPU / CPUTimeRAW
MaxRSS / MaxRSSTask / MaxRSSNode
TRESUsageInTot / TRESUsageOutTot
AveCPU / AveRSS
```

处理顺序：验证 job identity、识别 allocation row、分类 step、保存安全原始记录摘要、按字段归并、生成 completeness。多个 submit identity、requeue、resize 或重复 record 不能任取第一条；匹配不唯一则 `identity_ambiguous`。

### 7.5 权威口径

CPU：优先经过 probe 验证的 allocation-level `TotalCPU`，其次是可证明不重叠的 step 聚合，否则 unavailable。

```text
cpu_efficiency = total_cpu_seconds /
                 (allocated_cpu_count × elapsed_running_seconds)
```

内存：始终可展示 `max_single_task_rss`；只有单 task 或可靠 job-level cgroup peak 才计算 memory efficiency。多 task MaxRSS 不乘 task 数。

I/O：只有 total 字段形成累计读写量。

GPU：capability 确认对应 TRES、保存覆盖时长、覆盖不足不计算；未配置不等于 0%。

时间：

```text
queue_wait     = Start - Submit
eligible_wait  = Start - Eligible
runtime        = End - Start
walltime_ratio = runtime / requested_walltime
```

缺少必要时间戳时对应派生值 unavailable。

### 7.6 Summary 完整度

```text
identity_status
allocation_record_status
step_record_status
cpu_quality
memory_quality
io_quality
gpu_quality
timing_quality
finalization_status
```

总体状态：`complete | partial | unsupported | identity_ambiguous | retry_exhausted`。Partial Summary 可以展示，但规则只能使用质量合格字段。

## 8. 活跃生命周期与终态固化

### 8.1 采样启停

```text
Run 绑定 job_instance_key
→ PENDING：账号/调度观测，不调用 sstat
→ RUNNING：创建 sampling record
→ COMPLETING：继续采样
→ terminal：停止普通采样，进入 finalizer
```

状态变化消费现有 Run reconcile 事件；观测模块不复制 Run 状态机。

### 8.2 `ResourceFinalizationTask`

Run 终态事件幂等创建：

```text
task_id
run_id
job_instance_key
terminal_event_id
state
attempts
next_attempt_at
first_attempt_at / deadline_at
last_accounting_digest
stable_observations
last_error
```

状态：

```text
waiting_accounting
→ collecting
→ validating_identity
→ waiting_stability
→ finalized
```

降级：`unsupported | identity_ambiguous | retry_exhausted | auth_required`。它们不改变 Run 终态。

### 8.3 Accounting 延迟

simulator 默认重试：立即、5s、15s、30s、60s、120s、300s。真实连接可配置更长窗口，但必须有最大尝试数、deadline、连接预算和 jitter。Capability 已证明 accounting unsupported/permission denied 时直接生成对应 Summary，不做无意义重试。

正常固化至少要求：

- allocation record 与 job identity 唯一匹配；
- 状态终态且 End/Elapsed 已出现；
- 必需字段可解析；
- 两次相邻规范化 accounting digest 相同；
- 两次查询满足最小稳定间隔。

只有身份和时间、资源字段仍为空时继续等待；超过 deadline 后固化 partial/retry_exhausted。

### 8.4 不阻塞终态主链

```text
Run terminal
├─ logs_finalize
├─ existing Diagnosis
└─ resource_finalization
```

Accounting 延迟不得阻塞 Run 终态、日志、Capsule、Diagnosis 或用户查看结果。

### 8.5 不可变修订

```text
summary_id
run_id
job_instance_key
revision
status
supersedes_summary_id
created_at
content_sha256
```

Summary 发布后不原地修改。若先产生 partial，后来 accounting 恢复，则创建 revision 和新 Evidence，并引用被替代版本。API 默认返回最新版本但允许查询历史。

Late reconciliation 仅由恢复事件、终态后 30 分钟或 24 小时检查触发；24 小时后不自动轮询，除非以后提供明确的管理员重采集操作。

### 8.6 Requeue 和多 attempt

同一 Slurm Job requeue 时形成 `ResourceAttempt[1..n]`。边界优先由 Restarts、SLUID/OriginalSLUID、Submit/Start 变化和状态重新进入 pending/running 识别。每个 attempt 独立 accumulator、采样范围和 accounting；无法可靠拆分时标记 `attempt_boundaries_uncertain`，不计算跨 attempt 效率。

### 8.7 Evidence 与恢复

Summary 规范 JSON、SHA-256、Evidence object 和 `run.resource_summary_available` 事件通过同一事务或 transactional outbox 发布。FinalizationTask 复用 durable outbox 的 claim、heartbeat、retry、dead-letter 和 fencing。远端查询后崩溃可安全重试；相同 task/accounting digest 去重。

## 9. 确定性评价、历史比较与建议

### 9.1 `ResourceEvaluation`

```text
evaluation_id
run_id / summary_id
rule_id / rule_version
eligibility
severity / confidence
summary
measured_values / thresholds
evidence_refs
workload_fingerprint
suggested_action
created_at
```

CPU、内存、GPU、walltime 和队列分别表达，不生成一个误导性的综合资源分数。

### 9.2 Eligibility gate

```text
identity unique
→ Summary quality sufficient
→ metric supported
→ coverage sufficient
→ runtime sufficient
→ rule/unit version valid
```

不满足时保存 `eligible=false` 和原因，不产生优化结论。

### 9.3 首版终态规则

| rule | eligibility 与阈值 | 输出边界 |
|---|---|---|
| `CPU_UNDERUTILIZED` | runtime ≥ 10m，可靠 TotalCPU，efficiency < 20% | 检查并行度或尝试减少 CPU；不保证性能不变 |
| `MEMORY_OVERALLOCATED` | 单 task 或可靠 job peak，peak/allocated < 30% | 多 task MaxRSS 不触发 |
| `GPU_UNDERUTILIZED` | runtime ≥ 10m，coverage ≥ 80%，加权平均 < 20% | 先查数据管线、batch size、CPU 和并行方式 |
| `WALLTIME_OVERREQUESTED` | 可靠 runtime/requested < 20% | 单次低置信度；历史重复后增强 |

OOM、TIMEOUT、非零退出、依赖、节点和文件系统错误继续属于 Diagnosis。Observability 只提供 `OOM_RESOURCE_CONFIRMATION` 等支持事实，不制造第二个竞争结论。

### 9.4 运行期条件

Observability 产生 `ResourceConditionFact`，由 Runtime Watch 决定是否形成 provisional alert：

- `MEMORY_PRESSURE`：可靠 job-level 指标连续三个样本超过分配量 90%；
- `WALLTIME_NEAR_LIMIT`：运行达到 walltime 90%；
- `RESOURCE_SIGNAL_STALE`；
- `GPU_SIGNAL_UNAVAILABLE`。

多 task 只有可靠 job-level 指标才启用 MEMORY_PRESSURE。

### 9.5 可比较 Run

`workload_fingerprint` 包含 template/version、entry content digest、command shape、关键非资源参数、input asset/dataset digest 或稳定引用、runtime environment identity；明确排除 CPU/GPU/内存/walltime 请求、run ID、时间戳和随机工作目录。

资产无摘要时 `comparability=weak`，不能把历史提升为高置信度。

首版只正式增强 walltime：最近 5 个同 fingerprint Run 至少 3 个成功且 ratio 均低于 20%，才提升为 high confidence。CPU、内存和 GPU 可以展示趋势，但不能在没有扩展性实验时声称“最佳资源数”。

### 9.6 用户决策

接受、拒绝或延后建议不修改 Evaluation，而新增：

```text
ResourceAdviceDecision
evaluation_id
owner
decision
note
decided_at
derived_run_id
```

由此追踪建议、审批、派生 Run、新 Summary 和前后比较。

## 10. API、事件与权限

### 10.1 原则

- 身份从认证会话获得，新接口不接受任意 owner 切换。
- connection_id 可显式提供；只有一个连接时允许省略。
- latest、series、Summary、accounting records 是不同读模型。
- API 只读 Store，不访问 Slurm。
- 当前学生及其 Agent 可以获得丰富 owner-scoped 事实；严格限制凭据和跨学生数据，而不是过度裁剪本人信息。

### 10.2 平台与账号 API

```http
GET /api/v1/observability/connections/{id}/capabilities/latest
GET /api/v1/observability/connections/{id}/platform/latest
GET /api/v1/observability/connections/{id}/platform/series
GET /api/v1/observability/connections/{id}/nodes
GET /api/v1/observability/connections/{id}/account/latest
GET /api/v1/observability/connections/{id}/account/series
GET /api/v1/observability/connections/{id}/account/jobs
GET /api/v1/observability/connections/{id}/account/jobs/{job_instance_key}
```

`account/jobs` 支持 state、origin、q、limit、cursor 和 as_of_cycle。游标绑定认证 owner、连接、周期和过滤条件。

### 10.3 Run API

```http
GET /api/v1/runs/{run_id}/resources
GET /api/v1/runs/{run_id}/resources/series
GET /api/v1/runs/{run_id}/resources/accounting-records
GET /api/v1/runs/{run_id}/resources/quality
GET /api/v1/runs/{run_id}/resources/comparisons
GET /api/v1/runs/{run_id}/resource-summaries
GET /api/v1/runs/{run_id}/resource-summaries/{summary_id}
GET /api/v1/runs/{run_id}/resource-evaluations
POST /api/v1/runs/{run_id}/resource-evaluations/{evaluation_id}/decision
```

活动 Run 的 `/resources` 返回 accumulator、最新样本质量和 series 摘要；终态返回最新 Summary、finalization 和评价；requeue 返回 attempts；有修订时声明历史数量。

### 10.4 Series

参数：from、to、window、`step=raw|1m`、attempt、limit、cursor。Raw 最大 2 小时，1m 最大 24 小时。固定时间桶中的缺失返回 null 和 missing reason；不做隐藏插值；carried-forward 明确标记。

### 10.5 HTTP 语义

| 情况 | 结果 |
|---|---|
| fresh data | 200/fresh |
| 只有旧缓存 | 200/stale |
| 从未采集 | 404 observation_not_found |
| 字段不支持 | 200 + field unsupported |
| auth 失效但有缓存 | 200/stale + connection warning |
| Store 不可用 | 503 |
| 不属于当前用户 | 404，避免枚举 |
| 非法窗口/cursor | 400 |

Latest ETag 基于 pulse digest，Summary 使用不可变强 ETag；`as_of_cycle` 冻结 account 聚合和 job 明细的一致视图。

### 10.6 事件

```http
GET /api/v1/observability/events/stream?connection_id=...
GET /api/v1/runs/{run_id}/events/stream
```

事件：

```text
observability.platform_pulse_available
observability.account_pulse_available
observability.connection_degraded
run.resource_sample_available
run.resource_condition_changed
run.resource_summary_available
run.resource_summary_superseded
run.resource_evaluation_available
```

SSE 只携带摘要，不携带完整时序；durable event ID 支持 `Last-Event-ID` 补读。

### 10.7 用户决策不是执行

Decision 请求只有 accepted/rejected/deferred、可选 note 和 request_key。接受建议不等于应用 patch；后续 Agent/Contract/Run Action Tool 仍需独立审批。

### 10.8 信息分级

Agent 可读：集群能力、节点状态/名称/维护原因、分区/QoS/GRES/TRES、当前学生全部 managed/external 作业、job ID/名称/workdir、Contract、日志、时序、accounting step、质量和来源。

Agent 不可读：其他学生私有明细和任何密码、JWT、私钥、ControlMaster、认证 header。

节点和平台细节由 connection `visibility_policy` 决定：

```text
platform_detail: aggregate | nodes | full_permitted
node_identity: visible | pseudonymized | hidden
node_reason: visible | hidden
owner_job_detail: full
owner_source_record: normalized | bounded_raw
```

`owner_job_detail=full` 不允许被前端摘要需求降级；它保证当前学生及其 Agent 能读取本人完整观测。比赛和校内自部署模式可以使用 `platform_detail=full_permitted`、`node_identity=visible`、`node_reason=visible` 和 `owner_source_record=bounded_raw`。原始命令输出不直接进入普通产品响应，但 owner-scoped 规范化记录和受限原始 artifact 可由 Agent 工具按需读取。

### 10.9 Legacy API

`/api/v1/platform/snapshots*` 和 `/api/v1/platform/capabilities` 保留，不重定向到新动态接口，也不承诺动态时序。未来前端重写和 API 版本策略再决定废弃时间。

## 11. Pi Agent Core 观测集成

### 11.1 运行位置

```text
Pi Agent Core（应用侧）
   ├─ Observability Tools → Product Store
   ├─ Run/Contract Tools
   ├─ File Tools
   └─ Approved Action Tools
```

Agent 不驻留登录节点，也不持有 Slurm token、SSH、MFA 或通用 shell。只有独立 Worker 经 ClusterConnector 访问集群。因此并发学生问答不会产生同数量的登录节点轮询。

### 11.2 四种上下文

`platform_guidance`：能力、平台/账号 pulse、节点、分区、QoS、pending reason、账号活动作业。

`run_live_diagnosis`：Contract、job instance、Runtime Watch 日志/alert、近期资源样本、时序空洞、节点和 walltime。

`terminal_optimization`：Summary、allocation/step accounting、Evaluation、Diagnosis 和允许修改的 Contract 字段。

`run_comparison`：workload fingerprint、各 Run 的请求、Summary、质量、结果和代码/模板/输入版本差异。

### 11.3 渐进式取证

会话初始只接收连接、能力版本、相关 Run、freshness、可用证据索引和工具列表。Agent 再按问题逐步请求有界明细，而不是一次塞入全部 24 小时时序。

工具：

```text
observability.get_platform_state
observability.get_node_details
observability.get_account_jobs
observability.get_job_details
observability.get_run_live_resources
observability.get_run_resource_series
observability.get_run_accounting_records
observability.get_run_resource_summary
observability.compare_runs
observability.explain_metric_quality
observability.get_source_record
```

每个工具从 Agent Session 派生 owner，不接受模型指定其他 owner；带分页、时间窗和字节预算；访问进入 Agent trace。

`get_source_record` 只读取 bundle 已引用的规范化 source record；确需核对解析时，可返回经过凭据清理的有界原始 artifact 片段。它不能接受任意集群路径，也不能读取其他学生记录。

### 11.4 工具响应

```json
{
  "facts": {},
  "availability": {},
  "quality": {},
  "observed_window": {},
  "source_refs": [],
  "warnings": [],
  "next_cursor": null
}
```

工具返回结构化事实，不只返回自然语言标签。Agent 必须能看到 ReqTRES/AllocTRES、allocation row、step、MaxRSS 口径、缺失原因、accounting 延迟、series 空洞、coverage 和 source discontinuity。

### 11.5 `AgentObservationBundle`

当 Agent 从解释进入修改或实验建议时，冻结：

```text
bundle_id
owner / session_id / purpose
connection_id / capability_version
cycle_ids
run_ids / summary_ids / evaluation_ids
log_segment_refs
tool_result_digests
freshness_manifest
content_sha256
```

建议、patch 和派生 Run 引用 bundle。普通对话可读 latest；产生可执行建议必须固定证据。

### 11.6 充分性门禁

| 问题 | 最低证据 |
|---|---|
| 为什么排队 | 本人 job state、pending reason、capability |
| CPU 是否浪费 | reliable TotalCPU、allocated CPU、runtime |
| 内存是否过量 | reliable job peak 或单 task MaxRSS |
| GPU 是否低效 | accounting support、coverage、时序 |
| walltime 是否过长 | requested walltime、可靠 Start/End |
| 哪次 Run 更好 | fingerprint 可比较、双方 Summary 合格 |

不足时 Agent 必须说明缺少什么和为何缺少，不能补造平台事实。

### 11.7 受控刷新提示

`observability.request_refresh` 只向调度器提交优先级提示，不立即执行命令：仅 stale/expired 接受、受 cooldown/预算限制、重复合并，返回 scheduled/rate_limited/auth_required/unsupported。它不修改作业，无需作业操作审批，但必须审计。

### 11.8 读取与执行分离

Agent 可生成 explanation、uncertainties、suggested contract patch/experiment、tradeoff 和 evidence refs。真正的 `contract.create_derived`、`files.apply_patch`、`run.submit`、`run.cancel` 属于另一组 Action Tools，继续要求审批、preflight 和 policy gate。

### 11.9 External job

Agent 可解释当前学生 external 作业的状态、pending reason 和已观测资源，也可建议导入；缺少冻结 Contract、代码版本和 lineage 时，不得声称完整复现、自动修复、克隆/提交或作为模板验证结果。

### 11.10 不可信日志和审计

当前学生日志可给校内自部署模型，不做无必要脱敏，但标记 `content_role=untrusted_observation`。日志中的指令不能改变 system policy、工具或审批。

Agent trace 保存 session、tool、bounded arguments、result object IDs/digest、freshness、duration、error 和 bundle ID；大段日志/时序只引用现有对象，不复制一份。

LLM 不可用时，确定性 Evaluation、口径说明、Summary、series 和固定建议模板仍可使用。

## 12. 异常、健康与运行治理

### 12.1 分通道健康

每连接维护 capability、platform、account、active_run、terminal_accounting 和 retention health，状态为：

```text
unknown | warming_up | healthy | degraded | stale |
auth_required | unsupported | paused_budget
```

平台正常而 sstat unsupported 时必须分别表达，不能把连接整体伪装成全绿或全红。

### 12.2 最近成功事实和错误分类

失败不覆盖最近样本；latest 返回旧样本、stale、失败时间、错误类别和下一次重试。

标准码：

```text
AUTH_REQUIRED
PERMISSION_DENIED
SOURCE_UNSUPPORTED
SOURCE_SCHEMA_CHANGED
REMOTE_TIMEOUT
REMOTE_UNAVAILABLE
RATE_BUDGET_EXHAUSTED
PARSE_FAILED
UNIT_INVALID
IDENTITY_AMBIGUOUS
CLOCK_SKEW
STORE_UNAVAILABLE
FENCED
RETENTION_FAILED
```

错误摘要有界且清除凭据。

### 12.3 Version/schema drift

Capability probe 保存 Slurm/version、source API、supported operations/fields、unit behavior、PrivateData behavior 和 digest。REST/OpenAPI、CLI 字段、metrics label、accounting plugin 或单位语义变化时停止派生计算，保留安全原始记录并标记 schema changed。

### 12.4 Counter discontinuity 和数值验证

累计 CPU/I/O 下降时不产生负增量，而新建 series segment 并检查 requeue、step/source 切换。写入前验证非负数量、时间顺序、0–1 ratio、64 位安全范围和单位一致。`allocated > total` 不裁剪，保留并警告。

### 12.5 Clock skew

记录 collector_received_at、source_reported_at 和 database_committed_at。时钟偏差过大时 freshness 按接收时间计算，不用跨主机时间戳计算细粒度延迟；Slurm 内部 Submit/Start/End 仍可作同一时间域相对计算。

### 12.6 Store pressure 与优先级

数据库/磁盘压力时停止 raw 高频写入，保留最近样本并优先：

```text
terminal Summary/Evidence
> capability change
> account/platform latest
> active Run raw samples
```

没有成功 bucket 时不清理 raw；恢复后不补造失去的实时样本。

### 12.7 Auth recovery

auth_required 停止密集调用，不后台尝试密码/OTP。认证恢复事件先触发 capability probe，再带 jitter 恢复通道，terminal accounting 优先。

### 12.8 运维指标与告警

低基数指标：

```text
pilot107_observation_cycles_total{lane,status}
pilot107_observation_cycle_duration_seconds{lane}
pilot107_observation_last_success_age_seconds{lane}
pilot107_observation_active_runs
pilot107_observation_budget_skips_total{lane}
pilot107_resource_finalization_tasks{state}
pilot107_resource_summary_total{status}
pilot107_observation_store_errors_total
```

owner、job/run ID、node、error message 和无界 partition/QoS 不作 Prometheus labels。

运维告警覆盖：平台超过两个周期未成功、lease 无人持有、schema drift、finalization 积压、retry-exhausted 增加、retention 失败、空间压力和 fencing 冲突。用户资源低效进入 Evaluation，不进入运维告警。

## 13. 本地模拟验收

### 13.1 当前能力缺口

当前 Slurm 25.11.2 simulator 已有 slurmdbd 和 `AccountingStorageTRES=gres/gpu`，但使用 `JobAcctGatherType=jobacct_gather/none`。这不足以证明活动资源采样。实施时新增可选 `compose.observability.yml` 和测试 profile，优先验证：

```text
JobAcctGatherType=jobacct_gather/linux
JobAcctGatherFrequency=5
AccountingStorageType=accounting_storage/slurmdbd
```

优先 linux plugin，避免首版依赖容器 cgroup v2 特权。基础 simulator 在插件行为验证前不直接替换。

Fake GPU GRES 只能验证请求、分配、TRES、unsupported 和 coverage gate，不能证明 NVML/CUDA/GPU utilization。

### 13.2 分层矩阵

L0 模型/规则：单位、availability/freshness、node flags、job identity/requeue、step 分类、accounting 归并、fingerprint、eligibility 和缺失值。

L1 Store/并发：migration、幂等、ID 冲突、lease/fencing、accumulator 并发、Summary revision、outbox、retention 和 Evidence 引用；SQLite/PostgreSQL 共享 contract。

L2 Adapter：CLI/REST/OpenMetrics 统一契约，字段发现、固定 argv、batch、partial、schema drift、权限、timeout、单位、空结果、延迟和 counter reset。Golden fixture 不替代 live。

L3 Docker Slurm live：CPU busy、sleep、单 task 内存、多 task 内存、有界 I/O、PENDING、success、exit、TIMEOUT、CANCELLED。

### 13.3 Live 证明

- PENDING 不调用 sstat；RUNNING 产生真实 step 样本。
- Busy 与 sleep 的 CPU 事实可区分。
- 单 task RSS 口径正确，多 task MaxRSS 不冒充 job peak。
- 终态 sacct 唯一匹配冻结身份。
- Summary digest/Evidence 可验证。
- GPU 是 unsupported 而不是 0%。

### 13.4 Platform/account/identity

构造 idle/mixed/allocated/draining/down；验证 PlatformPulse。Alice 看到本人 managed/external；Bob 私有 job、路径和资源不进入 Alice/Agent context；允许公开的节点事实仍可读。Pending requested 与 Running allocated 分开。API 高频读不增加命令数。

### 13.5 延迟、恢复和负载

可控 adapter 注入无 sacct、先 allocation 后 step、字段延迟、稳定 digest、deadline partial、late revision、Worker 提交前崩溃和双 Worker fencing。

真实 Docker 运行少量作业；100 活动 Run 公平性用可控 adapter。证明命令预算、squeue/sstat batch、terminal 优先、无永久饥饿、coverage 降级，以及 Agent/API/SSE 不增远端调用。

### 13.6 Retention

Fake clock 验证 raw → 1m bucket → 2h raw 清理 → 24h bucket 清理 → Summary/Evidence 保留；aggregation 失败不删 raw，活动 accumulator 不删，重启和重复清理幂等。

### 13.7 Agent 固定问题

1. “为什么排队”：必须读 job、reason、capability 并引用。
2. “多任务内存是否申请过多”：只有多 task MaxRSS 时必须拒绝结论。
3. “GPU 利用率为什么为零”：unsupported 时纠正前提。
4. “把内存改小重跑”：能建议，但冻结 bundle、等待审批和 preflight。
5. “比较修复前后”：fingerprint 不兼容时不得直接比较。
6. stale：最多一个受限 refresh hint。
7. 日志伪造指令：不扩大工具权限。

### 13.8 故障矩阵

auth/recovery、profile revision、lease loss、schema drift、clock skew、partial、counter reset、Store outage、retention failure、SSE resume、owner 越权、Evidence 重放和 LLM unavailable。

### 13.9 机器可读证据

报告保存 revision、sim image/Slurm version、JobAcctGatherType、capability matrix、场景结果、unsupported fields、command budget、digests 和 limitations，并明确区分 fixture passed、Docker live passed、GPU synthetic only、VM not tested、real107 not tested。

## 14. 实施切片与完成定义

### Slice 0：合同和 simulator 准备

固定类型/adapter contract，验证 jobacct plugin，建立 CPU/内存/I/O 作业和证据分级。

### Slice 1：平台、账号与 Agent 平台问答

单连接租约 → capability/platform/account → latest API → Agent context/tools → refresh hint → owner 隔离。退出条件是 Agent 能引用新鲜事实解释排队和资源选择，API 不放大命令。

### Slice 2：活动 Run 与运行期 Agent

Run event → batched sstat → sample/accumulator/series → Agent live tools → ResourceConditionFact。退出条件是本地真实采样和受控解释成立。

### Slice 3：终态 Summary、评价与 Agent 优化

Terminal → accounting retry/stability → immutable Summary/Evidence → Evaluation → bundle/comparison/proposal。退出条件是建议—审批—派生 Run—前后比较信息链成立。

### Slice 4：时序、保留和治理

Minute buckets、retention、pressure、series/SSE、health metrics、schema/auth recovery、100 Run budget 和 PostgreSQL parity。

### Slice 5：本地封版

Fixture、Docker live、Agent 固定问题、崩溃/fencing、retention 和机器可读报告；远程 VM 恢复后只追加兼容验证。

### 明确不做

- 前端 Dashboard 重构；
- 队列等待时间预测；
- 自动资源扩缩、取消或提交；
- 计费系统；
- Prometheus 作为产品主存储；
- 跨学生私有作业明细；
- 用 fake GPU 宣称真实利用率；
- 远程 VM 不可用期间的部署承诺。

### 本地完成门禁

- 单租约采集得到证明；
- Platform、Account、Run 三层闭环；
- 真实验证 CPU、单 task 内存和 terminal accounting；
- partial/stale/unsupported 不被伪装；
- Agent 能充分取证且不能越权执行；
- Summary/Evidence 可重放；
- 100 Run 负载契约、retention 和崩溃恢复通过；
- GPU、VM 和真实 107 未验证边界写入报告。

最终纵向链：

```text
平台动态事实
→ 当前学生作业事实
→ 活动 Run 资源事实
→ 终态资源总结
→ 确定性评价
→ Pi Agent 充分取证、解释和建议
→ 用户审批后的派生实验
```

## 15. 参考资料

- [Slurm squeue](https://slurm.schedmd.com/squeue.html)：状态、pending reason、用户过滤与 RPC 负载警告
- [Slurm sstat](https://slurm.schedmd.com/sstat.html)：运行中 job/step 指标、JobAcctGather 依赖和字段口径
- [Slurm sacct](https://slurm.schedmd.com/sacct.html)：allocation/step accounting、TotalCPU、CPUTimeRAW、MaxRSS 和 TRES
- [Slurm REST API](https://slurm.schedmd.com/rest.html)：版本、认证、可信网络与连接限制
- [Slurm 25.11 Metrics Guide](https://slurm.schedmd.com/metrics.html)：OpenMetrics、访问控制、基数和性能影响
- [Prometheus recording rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/)
- [Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/)
