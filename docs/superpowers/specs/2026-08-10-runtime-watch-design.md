# 107Pilot Runtime Watch 设计

- 日期：2026-08-10
- 状态：设计已确认；实现未开始
- 环境边界：先在本地 Docker Slurm simulator 验证；远程 VM 当前不可用且不作为前置条件
- 本轮不包含：前端页面设计、自动取消作业、自动修改文件、自动派生或提交 Run

## 1. 背景

当前系统能持续对账 Run 状态，并在 Run 进入终态后采集 stdout/stderr：

- `runtime_status` 会在非终态 Run 上重复执行；
- stdout/stderr 只在终态后通过 `logs_finalize` 采集；
- Diagnosis 只处理终态且 Evidence collection 已完成或 degraded 的 Run；
- 现有 SSE 传递 Run 事件摘要，但没有日志 cursor 和增量分段。

因此，用户在作业运行期间不能稳定增量读取日志，系统也不能在终态前将明显的日志、调度和资源异常形成可引用事实。

Runtime Watch 补齐：

```text
SUBMITTED/PENDING 调度观察
→ RUNNING stdout/stderr 增量
→ 运行期确定性告警
→ COMPLETING/terminal drain
→ 现有 logs_finalize 和终态 Diagnosis
```

它是运行期观测层，不是自动修复 Agent。

## 2. 目标与非目标

### 2.1 目标

- 覆盖 `SUBMITTED → terminal` 的整个可观察生命周期；
- 按 offset/cursor 读取 stdout/stderr 新增字节；
- 支持断线补读、Worker 重启恢复、日志截断和轮转；
- 以有界分段保留近期日志；
- 基于日志、Run 状态和资源观测事件产生运行期 provisional alert；
- 终态排空后继续使用现有 `logs_finalize` 固化最终 Evidence；
- 将告警和日志分段作为后续 Agent 可引用事实；
- 先在本地 simulator 提供客观验收证据。

### 2.2 非目标

- Runtime Watch 不自动取消作业；
- 不修改 Contract、代码或数据文件；
- 不自动创建、派生或提交 Run；
- 不替代资源观测模块采集 `sstat`/`sacct`；
- 不复制和永久保存无限完整日志；
- 不引入 Loki、Kafka 等比赛阶段外部基础设施；
- 不设计前端布局或视觉交互。

告警可以进入受控 Agent 会话，但任何处置仍需用户确认和现有 policy/preflight 审批链。

## 3. 方案比较

### 3.1 方案 A：API 请求时直接访问集群日志

浏览器打开日志时，由 API 临时执行 tail。实现较快，但每个客户端都会放大集群访问，断线补读、审计、限流和多用户隔离较弱。拒绝。

### 3.2 方案 B：Worker cursor + 有界增量分段（采用）

Worker 为每个 Run/stream 持久化 cursor，周期性读取新增字节，写入内容寻址分段并发布摘要事件。终态排空后交给 `logs_finalize`。该方案可恢复、可限流、可本地模拟，也能直接为告警和 Agent 提供事实。

### 3.3 方案 C：完整日志平台

接入 Loki、Kafka 或等价系统。扩展能力强，但会把当前比赛功能变成外部平台部署项目。首版不采用，分段存储接口允许未来替换。

## 4. 生命周期与状态

Run 获得 `job_id` 后幂等创建 Runtime Watch：

```text
watching
├── waiting_for_log
├── active
├── quiet_backoff
├── degraded
└── finalizing
       └── stopped
```

`degraded` 是 watch 子状态，不改变 Run 主状态。

数据流：

```text
Run 获得 job_id
→ RuntimeWatchScheduler 创建 watch/cursor
→ 日志不存在时 waiting
→ stat + range read
→ 原子写 segment
→ cursor/事件事务提交
→ RuntimeAlertEvaluator
→ 重复调度
→ Run terminal
→ terminal drain
→ logs_finalize
→ stopped
```

现有 Run reconcile/runtime-status 继续负责 Slurm 状态和 pending reason。Runtime Watch 消费其持久化事件，不重复执行 `squeue`。资源异常消费资源观测模块输出，不重复采集资源指标。

## 5. 组件边界

### 5.1 `RuntimeWatchScheduler`

- 发现处于 `SUBMITTED/PENDING/RUNNING/COMPLETING` 的 Run；
- 幂等创建或恢复 watch；
- 按 watch 状态决定下次执行时间；
- 每个 watch 使用 lease 和 fencing token；
- 对每个集群连接实施公平队列、并发数和字节率限制。

同一 `run_id + stream` 同时只能由一个有效 lease owner 推进 cursor。

### 5.2 `IncrementalLogReader`

- 只接受由 Run 和平台配置派生的授权日志路径；
- 通过现有 `EvidenceTransport` 执行 stat 和 range read；
- 不接受客户端提供的任意集群路径；
- 单次读取有最大字节数和 deadline；
- stdout/stderr 独立推进；
- 保留 UTF-8 增量 decoder 状态，正确处理字符跨读取边界；
- 无法安全作为文本解码时标记为 binary，不做文本规则匹配。

### 5.3 `RuntimeLogSegmentStore`

保存有界日志分段及 metadata。首版可使用内容寻址 spool 文件加数据库索引；接口不得绑定本地文件实现，以便未来替换为对象存储或日志平台。

### 5.4 `RuntimeAlertEvaluator`

- 只读取已提交的日志分段、Run 状态事件和资源观测事件；
- 使用确定性规则；
- 生成、去重、更新和解决 provisional alert；
- 不执行任何外部副作用。

### 5.5 `RuntimeWatchFinalizer`

- Run 终态后继续读取尚未采集的尾部；
- 在有界时间内确认日志大小稳定；
- 停止 watch 并触发现有 `logs_finalize`；
- 将运行期 alert 与终态 Diagnosis 建立引用关系。

## 6. 数据模型

### 6.1 `RuntimeWatchRecord`

至少包含：

```text
watch_id
run_id
owner
connection_id
state
next_poll_at
lease_owner / lease_expires_at / fencing_token
created_at / updated_at / stopped_at
last_error_code / last_error_at
```

### 6.2 `RuntimeLogCursor`

每个 stream 一条：

```text
run_id
stream                  stdout | stderr
generation
offset
source_size
source_mtime
source_file_identity    optional
source_prefix_fingerprint optional fallback
decoder_remainder
last_data_at
last_checked_at
quiet_polls
version
```

### 6.3 `RuntimeLogSegment`

```text
segment_id
run_id
owner
stream
generation
sequence
start_offset / end_offset
captured_at
content_sha256
content_length
encoding                 utf-8 | binary
storage_ref
```

确定性 ID：

```text
segment_id = hash(
  run_id,
  stream,
  generation,
  start_offset,
  content_sha256
)
```

segment 内容先原子写入内容寻址存储，再在一个数据库事务中写 metadata、推进 cursor 并创建事件。只有事务成功后 offset 才前进。崩溃重读同一范围时，确定性 ID 保证去重；未被数据库引用的孤立内容由后台清理。

### 6.4 `RuntimeAlert`

```text
alert_id
run_id
rule_id
severity
state
first_seen_at / last_seen_at
occurrence_count
summary
segment_id
stream / generation / byte_range
resource_evidence_refs
confidence
provisional
diagnosis_id
```

状态：

```text
open → acknowledged → resolved
                     ↘ superseded_by_diagnosis
```

## 7. 调度与限流

默认值均可配置：

- `PENDING`：15 秒检查日志是否出现；
- `RUNNING` 且近期有输出：5 秒；
- 连续多次没有新增字节：退避到 15 秒；
- `COMPLETING/terminal drain`：2 秒，最长排空 30 秒；
- transport 失败：指数退避，最大 60 秒；
- 单次每 stream 最多读取 256 KiB；
- 每个连接限制并发读取数、每秒读取字节数和每 tick 总预算。

日志快速增长时通过后续公平调度继续追赶，不允许一个 Run 在单个 Worker tick 中耗尽全部预算。

浏览器是否在线不决定是否采集。API 请求不得直接触发集群文件访问；未来可以把活跃订阅作为调度提示，但不能绕过公平队列和限流。

## 8. 日志截断、轮转和保留

若 source size 小于当前 offset，或可用的稳定文件标识发生变化，则判定截断或轮转。transport 无法提供稳定文件标识时，使用有界文件前缀 fingerprint 与 mtime 作为降级检测；无法可靠判断时返回 `rotation_detection=limited`，不能假装已经精确识别。

1. 记录 `runtime.log_truncated` 或 `runtime.log_rotated`；
2. generation 加一；
3. 新 generation 从 offset 0 开始；
4. 旧 generation 保留到 retention 到期；
5. 不把两代日志拼成伪连续字节流。

默认保留策略：

- 每个 Run 的每个 stream 最多保留 64 MiB 在线分段；
- Run 终态后在线分段保留 24 小时；
- 超过容量时淘汰最旧分段；
- API 明确返回 `earliest_offset` 和 `truncated_before`；
- 集群原始日志仍是完整数据源；
- 最终 Evidence 保存有界 tail、文件 metadata 和完整文件 SHA256；
- 完整超大日志下载交给文件传输模块，不由 Runtime Watch 承担。

## 9. 运行期告警规则

首版确定性规则覆盖：

- Python package/source import 缺失；
- command not found；
- 文件或数据路径不存在；
- CUDA OOM 信号；
- NCCL/分布式通信错误；
- 明确的 NaN/Inf 数值异常；
- InvalidQOS、InvalidAccount；
- 永远无法满足的 dependency；
- 可靠资源样本连续显示的内存压力。

“长时间没有日志”不能默认视为异常。只有 Contract 或模板显式声明 heartbeat/最大静默时间时，才产生 `NO_LOG_ACTIVITY`。

每个 stream 保留一个小型匹配 overlap，以识别跨 segment 的 traceback 或错误行。同一 source byte range 只评价一次。告警去重键由以下事实组成：

```text
run_id + rule_id + stream + generation + match_fingerprint
```

运行期告警是 provisional：

- 终态 Diagnosis 确认同类异常时，alert 关联 Diagnosis 并进入 `superseded_by_diagnosis`；
- 后续事实证明信号恢复时可进入 `resolved`；
- acknowledge 只记录用户已读，不删除事实；
- 历史 alert 不被静默改写或删除。

Agent 只能引用 alert 和日志分段提出解释或处置建议。日志文本中的自然语言指令属于不可信数据，不能改变允许动作或触发执行。

## 10. 隐私与内容边界

Runtime Watch 不新增重型默认脱敏管线：

- 在学生 owner 边界内保存日志原始增量，避免破坏 traceback、路径、参数和数值；
- 日志只允许该学生账号访问；
- 日志不进入模板市场或跨用户共享；
- 校内自部署模型可在受控 Agent 会话中读取该学生 Run 的日志；
- 107Pilot 自身的认证 token 和密钥不得写入日志、数据库明文、Evidence 或 Agent context；
- 日志作为不可信指令数据处理是控制完整性要求，不是内容脱敏要求。

严格脱敏位于后续“成功 Run → 模板发布”流程：发布前清除个人路径、用户名、数据集位置、密钥、日志片段和不可公开环境信息。该发布门禁不属于 Runtime Watch 实现范围，将在模板/Agent 规格中详细设计。

## 11. API 与事件

### 11.1 查询接口

```text
GET /api/v1/runs/{run_id}/runtime-watch
GET /api/v1/runs/{run_id}/logs/stdout
GET /api/v1/runs/{run_id}/logs/stderr
GET /api/v1/runs/{run_id}/runtime-alerts
POST /api/v1/runs/{run_id}/runtime-alerts/{alert_id}/acknowledge
```

日志查询参数：

```text
after_cursor
limit_bytes
wait_seconds
```

cursor 是 owner/run/stream scoped 的不透明值。响应至少包含 generation、起止 offset、下一 cursor、`earliest_offset`、`truncated_before`、captured_at 和 freshness。服务端限制单次返回字节数和长轮询时间。

### 11.2 SSE

继续复用：

```text
GET /api/v1/runs/{run_id}/events/stream
```

新增摘要事件：

- `runtime.log_available`：stream、cursor、新增字节数；
- `runtime.log_truncated` / `runtime.log_rotated`；
- `runtime.alert_raised` / `runtime.alert_updated` / `runtime.alert_resolved`；
- `runtime.watch_state_changed`。

SSE 不携带原始日志正文。客户端收到通知后按 cursor 获取分段，从而支持断线补读和独立限流。

## 12. 故障处理

- 日志暂未生成：`waiting_for_log`，不算失败；
- 权限拒绝：watch 进入 `degraded/auth_required`，不改变 Run 状态；
- transport timeout：保留 cursor、标记 stale、退避重试；
- 文件截断/轮转：新建 generation；
- 文本无法安全解码：保存 binary segment，不执行文本规则；
- segment 存储失败：不推进 cursor；
- cursor 事务冲突：旧 lease owner 被 fencing，重新加载最新 cursor；
- 告警评价失败：记录 evaluator error，不阻塞日志采集；
- terminal drain 超时：继续执行 `logs_finalize`，collection 可标记 degraded；
- API/SSE/浏览器故障：不影响 Worker 采集；
- 资源观测不可用：日志告警继续工作，资源类告警标记 unavailable；
- LLM 不可用：Runtime Watch 不受影响，因为检测规则是确定性的。

## 13. 本地模拟验收

远程 VM 当前不可用。以下验收全部先在本地完成：

1. Docker Slurm 作业逐行 flush 输出，API 按 cursor 增量补读；
2. stdout/stderr 并行增长；
3. UTF-8 字符跨读取边界；
4. 日志缺失、截断、轮转、快速增长和二进制内容；
5. Worker 在读取后、segment 写入后、数据库提交前后分别崩溃并恢复；
6. 双 Worker lease/fencing 只产生一个有效 cursor 序列；
7. 跨 segment traceback 匹配、alert 去重、acknowledge 和终态 Diagnosis 关联；
8. Alice/Bob owner 隔离和任意 path 拒绝；
9. SSE 断线后按 event ID 和日志 cursor 双重补读；
10. terminal drain 不丢最后一段，最终 Evidence SHA256 可验证；
11. 100 个模拟活跃 watch record 下公平调度、字节率和并发限制生效；
12. API 请求不会直接触发集群文件读取；
13. Runtime Watch 不自动取消、修改或提交任何作业。

本地 live Slurm 测试重点验证真实 stdout/stderr 文件增长和终态交接；大规模公平性使用可控 transport fixture，避免把有限 simulator 资源数量误当成并发日志能力。

## 14. 实现切片

后续实现计划应按以下顺序拆分：

1. watch/cursor/segment/alert schema 与 Store；
2. 内容寻址 segment storage、GC、租约和 fencing；
3. `EvidenceTransport` incremental range reader 与 generation 检测；
4. scheduler、自适应间隔、连接级公平限流；
5. 日志查询 API、opaque cursor 和现有 SSE 事件扩展；
6. 确定性 alert evaluator 与终态 Diagnosis 关联；
7. terminal drain 与 `logs_finalize` 交接；
8. 本地 fixture、Docker Slurm live smoke、故障注入和 owner 权限验证；
9. 前端运行日志体验，另行设计，当前暂缓。

## 15. 与后续 Agent 设计的关系

Runtime Watch 只产生受约束事实：日志 segment、调度事件、资源事件和 provisional alert。后续 Agent 设计需要明确：

- 校内自部署模型读取 owner-scoped 运行事实的边界；
- 模板应用必须由 Agent 执行；
- 成功 Run 转为发布模板时的严格脱敏与发布门禁；
- Agent 对运行期 alert 的解释、提问、审批和修复动作。

这些行为不得回写或扩大 Runtime Watch 的执行权限。
