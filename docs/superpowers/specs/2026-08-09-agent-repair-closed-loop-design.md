# 107Pilot 受控作业修复 Agent 闭环设计

- 日期：2026-08-09
- 状态：设计已逐节确认，等待文档审阅
- 范围：失败作业诊断、受控资源/环境修复、Python 与 Bash/Slurm 代码修复、派生 Run 验证
- 验收环境：本地 Docker Slurm 模拟环境；远程 VM 不作为本阶段前提

## 1. 背景与目标

107Pilot 已具备 Run、Evidence、规则诊断、Remediation Session、审批、派生 Run、评价、受限代码上下文和 Repair Ticket 等基础能力，但当前 Agent 尚未形成完整纵向闭环：

1. `RemediationPlanService` 已能校验结构化 LLM 计划，但主要存在于单元测试路径，尚未成为 live remediation planning 的正式入口。
2. LLM 目前主要用于解释；真正的可执行建议仍主要由确定性规则产生，无法完成基于代码上下文的补丁规划。
3. `awaiting_input` 有状态但缺少类型化输入提交、持久化和恢复接口，用户只能转到 Studio 或人工接管。
4. `RepairTicket` 明确定义为 metadata-only 的人工交接票据，不保存或应用代码 diff，不能代表 Agent 已具备代码修复能力。
5. Agent 页面能显示会话、建议和原始 JSON，但没有把 Evidence 引用、自然语言推理、输入表单、代码 diff、验证结果和前后对比串成一个清晰流程。
6. 现有派生 Run 与评价能力需要收紧成功判定：仅有退出码 0 不能等同于修复已验证。

本阶段目标是完成一个比赛可展示、可审计、可恢复的“运行修复 Agent”纵向闭环：

```text
失败 Run
  → 冻结 Evidence
  → 规则诊断与事实提取
  → LLM 结构化修复计划
  → 确定性策略校验
  → 必要时请求用户输入
  → 展示并审批 Contract/代码 diff
  → 隔离准备与固定验证
  → 创建派生 Contract 和派生 Run
  → 收集新 Evidence 并进行前后评价
  → 已验证成功 / 重新规划 / 预算耗尽 / 人工接管
```

## 2. 本阶段范围

### 2.1 必须完成

- 从失败 Run 一键创建 Evidence-bound 修复会话。
- 规则诊断负责事实和安全边界，LLM 负责解释、排序和生成结构化修复计划。
- 支持资源、环境、路径、依赖和安全重试类修复。
- 补齐 `awaiting_input` 的类型化输入闭环。
- 支持 `.py`、`.sh`、`.sbatch` 现有文本文件的受控代码补丁。
- 所有变更经用户查看和批准后方可执行。
- 代码补丁只在隔离副本中应用，不修改原始作业目录。
- 创建不可变修复产物、派生 Contract 和派生 Run。
- 使用新 Evidence、预期输出和原诊断消失情况评价修复结果。
- 完整记录模型、token、事实引用、输入、审批、执行、验证和评价事件。

### 2.2 明确不做

- 课程实验指导与自动批改。
- 四类领域模板纵向闭环。
- 初学者通用问答或开放式聊天。
- 任意 Shell、任意命令生成或无审批执行。
- 自动修改密钥、认证配置、系统配置或管理员权限。
- 自动修改依赖清单、二进制文件、Notebook、数据文件或任意扩展名源码。
- 直接修改原始 workspace、提交 Git commit 或推送用户仓库。
- 以远程 VM 作为开发和验收依赖。

不支持的代码问题继续使用现有 `RepairTicket` 作为人工交接路径；Repair Ticket 的 metadata-only 语义保持不变。

## 3. 方案选择

### 3.1 方案 A：纯规则 Agent

规则直接从诊断生成资源或 Contract 补丁。优点是稳定、便于审计；缺点是无法覆盖多样化代码错误，解释和追问能力也有限。

### 3.2 方案 B：开放式 LLM 工具 Agent

让 LLM 自主读取文件、执行命令并循环修复。覆盖面大，但难以满足比赛项目所需的可审计、审批、租户隔离和安全边界，也会把日志提示词注入转化为执行风险。

### 3.3 方案 C：受约束的混合 Agent（选定）

规则、Evidence 和 Policy Gate 是事实与执行权威；LLM 只输出受 Schema 约束的解释、输入请求和动作计划。确定性编译器把合法计划转成平台内部动作，所有有副作用的动作必须审批。

该方案既能支持代码修复，又不会向模型开放任意 Shell 或平台控制权。LLM 不可用时，非代码类问题可回退到确定性规则；代码修复则保留上下文并转 `blocked` 或 Repair Ticket。

## 4. 总体架构

```text
┌───────────────────────────────────────────────────────────┐
│ Run / Contract / Evidence / Diagnosis                     │
└───────────────────────────┬───────────────────────────────┘
                            │ immutable digests + fact IDs
┌───────────────────────────▼───────────────────────────────┐
│ Evidence Builder                                           │
│ 日志、调度状态、资源统计、预期输出、Contract、代码快照       │
└───────────────────────────┬───────────────────────────────┘
                            │ trusted structured context
┌───────────────────────────▼───────────────────────────────┐
│ Hybrid Planner                                             │
│ 规则候选 + LLM RemediationPlan + deterministic fallback    │
└───────────────────────────┬───────────────────────────────┘
                            │ untrusted structured proposal
┌───────────────────────────▼───────────────────────────────┐
│ Proposal Compiler + Policy Gate                            │
│ Schema、事实引用、allowlist、风险、预算、陈旧性、安全校验    │
└──────────────┬────────────────┬───────────────────────────┘
               │                │
       missing input       approval required
               │                │
┌──────────────▼───────┐ ┌──────▼──────────────────────────┐
│ Typed Input Loop      │ │ Human Approval                  │
└──────────────┬───────┘ └──────┬──────────────────────────┘
               └────────────────┤
                                │ approved digest
┌───────────────────────────────▼───────────────────────────┐
│ Bounded Executor                                          │
│ Contract 派生 / 隔离代码补丁 / 固定验证 / 派生 Run         │
└───────────────────────────────┬───────────────────────────┘
                                │ new Evidence
┌───────────────────────────────▼───────────────────────────┐
│ Evaluator                                                 │
│ 状态、输出、诊断、资源和修复前后对比                        │
└───────────────────────────────────────────────────────────┘
```

### 4.1 组件职责

| 组件 | 职责 | 不负责 |
|---|---|---|
| Evidence Builder | 冻结当前轮日志、调度、资源、Contract、输出和代码事实 | 猜测缺失事实 |
| Hybrid Planner | 组合规则候选和 LLM 结构化计划 | 直接执行动作 |
| Proposal Compiler | 将合法计划转换为内部 typed action | 接受自由文本命令 |
| Policy Gate | allowlist、风险、预算、引用、陈旧性和权限校验 | 采信 LLM 自报安全性 |
| Input Loop | 收集缺失但允许由用户提供的值 | 收集密钥或任意命令 |
| Remediation Orchestrator | 驱动持久化状态机、租约、版本与幂等 | 在内存中维护唯一状态 |
| Repair Workspace Service | 创建隔离副本、应用已批准补丁、生成修复产物 | 修改源 workspace |
| Bounded Validator | 执行固定验证配置 | 执行 LLM 提供的命令 |
| Evaluator | 对比源 Run 和派生 Run，决定是否验证成功 | 仅凭退出码宣告成功 |

## 5. 状态机与每轮行为

沿用现有 `RemediationState`，补齐而非另建平行会话模型：

```text
waiting_evidence
  → diagnosing
  → planning
      ├─ 缺少安全参数 → awaiting_input → planning
      ├─ 有合法建议   → awaiting_approval
      └─ 无法安全规划 → blocked
  → ready
  → preparing
  → executing
  → evaluating
      ├─ 验证成功       → succeeded
      ├─ 可恢复且有预算 → planning
      ├─ 预算耗尽       → exhausted
      └─ 不可安全继续   → blocked
```

`failed` 仅用于平台内部不可恢复错误，`cancelled` 用于用户取消。执行后产生的新失败不得复用旧 Evidence；必须为派生 Run 冻结新 Evidence，再进入下一轮。

每轮约束：

- 一轮只选择一个主要修复动作，避免把多个原因和结果混在一起。
- 默认 `max_attempts=3`、`max_submissions=2`、`max_llm_calls=3`，继续沿用已有 wall time 和 token 预算。
- 默认最多执行 4 个只读 probe；probe 结果进入新 Evidence 版本。
- 使用 provider 返回的真实 token usage 更新 `RemediationUsage`，不能用调用次数代替 token。
- 每个状态迁移、输入和执行都使用 session version、lease 与幂等键。

## 6. Evidence 与规划协议

### 6.1 EvidenceSnapshot

每轮规划绑定一个不可变 Evidence 摘要，至少包含：

- source/derived Run ID、状态、job ID 和 lineage；
- scheduler state、exit code、stdout/stderr、资源用量与限制；
- Contract ID、Contract digest 和允许修改字段；
- expected outputs 及其存在性/校验结果；
- diagnosis ID、rule ID、置信度和引用的 Evidence object IDs；
- 如为代码问题：code snapshot ID、worktree fingerprint、目标文件路径、preimage hash 和受限源码窗口；
- 快照时间、收集完整度、限制与 warning。

规则从 Evidence 生成稳定 `fact_id`。LLM 的 summary、required input 和每个 proposal 都必须引用存在于当前快照中的 fact ID。不存在引用、引用过期或证据不足时，计划不得进入审批。

### 6.2 RemediationPlan V2

将现有 `pilot107.remediation-plan/v1` 演进为向后兼容的 V2。V2 保留 summary、facts、required inputs、proposals 和 stop conditions，并增加：

- 类型化 `required_inputs`：key、type、reason、constraints、options、secret=false；
- proposal 风险说明和预期效果；
- `code_patch` 动作的 patch set 与 validation profiles 引用；
- provider、model、prompt digest、raw response digest 和 token usage；
- Evidence、Contract 和 source snapshot digests。

Provider 输出解析最多尝试两次：首次失败后只允许一次格式修复请求；仍不合法则按错误类型回退或阻止。

### 6.3 动作分级

| 级别 | 动作 | 执行策略 |
|---|---|---|
| 自动只读 | `path_probe`、`runtime_probe` | Policy Gate 通过后可自动执行，结果写回 Evidence |
| 必须审批 | `contract_patch`、`environment_select`、`dependency_plan`、`retry_run`、`code_patch` | 展示变化和风险，用户批准后执行 |
| 人工接管 | 任意 Shell、未知动作、密钥、系统管理、越界文件、无法验证的补丁 | 不执行，进入 `blocked` 或 Repair Ticket |

`dependency_plan` 只允许从平台预注册环境/包策略中选择，不允许生成安装命令。首版代码补丁不修改 `requirements.txt`、`pyproject.toml` 或锁文件。

## 7. Contract 修复边界

Contract 修改以运行时实际 Contract Schema 为唯一权威，Proposal Compiler 不维护另一套字段语义。首版 allowlist 对齐当前可控字段：

- `entry.workdir`
- `environment.kind`
- `environment.name`
- `resources.partition`
- `resources.qos`
- `resources.cpus`
- `resources.gpus`
- `resources.memory`
- `resources.time_limit`
- `success.expected_outputs`

如 Contract Schema 后续扩展 nodes、tasks 或模块 profile，必须先显式加入 allowlist、校验器和 UI diff，不能由模型自由发现后写入。

禁止修改：

- `entry.command`、argv 或任意 Shell 文本；
- 任意环境变量名和值；
- 未注册镜像、未注册环境或平台管理员字段；
- owner、身份、凭据、审计字段；
- 与当前 diagnosis 无 Evidence 关系的字段。

代码修复产生的派生工作目录由系统编译器写入派生 Contract，不由 LLM 直接指定路径。

## 8. 受控代码修复

### 8.1 支持范围

首版只允许修改源 Run 声明 workdir 中已经存在的：

- Python：`.py`
- Bash：`.sh`
- Slurm 作业脚本：`.sbatch`

不允许新建、删除、重命名文件，不允许修改权限。无明确 traceback/脚本定位时，Agent 可通过类型化输入请求用户选择候选文件；仍无法确定时转人工。

### 8.2 不复用 Repair Ticket 作为补丁容器

现有 `RepairTicket` 的语义是“Agent 到本地工具的 metadata-only handoff”，且 `ArtifactManifest` 明确不包含源码或完整 diff。为避免破坏现有安全承诺，新增独立对象：

- `CodePatchSet`：补丁、文件哈希、Evidence 引用、风险和验证配置；
- `RepairWorkspace`：隔离副本 ID、source snapshot、状态和生命周期；
- `ManagedRepairArtifact`：修复产物位置、bundle/workspace digest、patch digest 和验证摘要。

三者与现有 `ActionProposal`、`ActionDecision`、`ActionExecution` 关联。Repair Ticket 仅作为无法自动处理时的 fallback。

### 8.3 补丁生成流程

1. 从 traceback、scheduler 错误、日志和 Contract entry 信息定位候选文件与行号。
2. 使用现有 `CodeContextService` 的 run-scoped workspace、大小和路径约束，扩展 Python traceback 之外的 Bash/Slurm 定位器。
3. 冻结 source snapshot、worktree fingerprint、目标文件完整 preimage hash 和受限源码窗口。
4. LLM 输出结构化 `CodePatchSet`，不得输出验证命令。
5. Patch Gate 解析 diff 并执行所有确定性检查。
6. UI 展示逐文件 diff、理由、引用事实、风险和固定验证方式。
7. 用户批准时记录 patch、Evidence、Contract 和 source snapshot 的联合摘要。
8. Executor 创建平台管理的隔离副本，再次核对 preimage hash 后应用补丁。
9. 运行固定验证配置。通过后生成 `ManagedRepairArtifact`。
10. 系统派生 Contract，使其 workdir/产物引用指向隔离修复副本，再准备并提交派生 Run。

### 8.4 Patch Gate

首版默认限制：

- 最多修改 3 个文件；
- 最多 200 行增删；
- 单个源文件沿用 `CodeContextPolicy.max_file_bytes=64 KiB`；
- 只接受 UTF-8 文本和规范化相对路径；
- 目标文件必须属于批准时的 source snapshot；
- 禁止绝对路径、`..`、符号链接跳转、设备文件和工作目录逃逸；
- 禁止 `.git`、`.env`、密钥、凭据、认证配置和平台内部目录；
- 禁止文件模式变化、二进制 patch、新文件、删除和重命名；
- 禁止补丁携带 Shell/argv/validation command 字段；
- 日志与源码中的自然语言指令一律视为不可信数据。

静态危险模式扫描只能作为附加阻断信号，不能宣称证明代码安全。最终执行权来自边界校验、隔离验证和用户对具体 diff 的审批。

### 8.5 固定验证配置

验证配置来自平台 registry，由 Proposal Compiler 选择，LLM 只能引用合法 profile ID：

| 文件类型 | 必做验证 | 可选验证 |
|---|---|---|
| `.py` | 使用 Python parser/`compile()` 做语法检查 | 项目预声明的隔离测试 profile |
| `.sh` | 固定 argv 的 `bash -n -- <file>` | 平台预注册的 shell lint profile |
| `.sbatch` | Bash 语法 + `#SBATCH` 指令、资源字段和 capability 校验 | 模拟器中的只读 preflight |

任何会执行用户代码的测试必须在无网络、限时、限 CPU/内存、非特权的验证容器/worker 中运行。LLM 不能增加命令、参数或绕过 profile 限制。

验证失败时不创建派生 Run；用户看到失败文件、验证 profile、结构化错误和下一步选择。隔离副本可丢弃，因此不需要回滚原始 workspace。

## 9. 类型化用户输入闭环

`awaiting_input` 不再是终点。新增持久化 `InputRequest` 与 `InputResponse`：

```json
{
  "request_id": "input_...",
  "key": "environment.name",
  "type": "enum",
  "reason": "当前 Evidence 无法确定包含目标依赖的环境",
  "options": ["course-py310", "ml-py311"],
  "constraints": {"required": true},
  "secret": false,
  "evidence_fact_ids": ["fact_..."]
}
```

允许的输入类型限于 string、integer、boolean、enum、path 和 file_selection，并由服务端再次验证。禁止 secret=true、token、password、API key、自由命令或任意环境变量。

提交输入必须携带 expected session version 和幂等键。输入保存后进入新 `planning` 轮，不直接把旧建议改成可执行建议；新计划必须重新计算摘要并重新审批。

## 10. API 与持久化

### 10.1 延用现有 API

- `POST /api/v1/runs/{run_id}/remediation-sessions`
- `GET /api/v1/remediation-sessions`
- `GET /api/v1/remediation-sessions/{session_id}`
- `POST /api/v1/remediation-sessions/{session_id}/advance`
- `POST /api/v1/remediation-sessions/{session_id}/approve`
- `POST /api/v1/remediation-sessions/{session_id}/reject`
- `POST /api/v1/remediation-sessions/{session_id}/execute`
- `POST /api/v1/remediation-sessions/{session_id}/takeover`
- `POST /api/v1/remediation-sessions/{session_id}/cancel`

### 10.2 新增 API

```text
POST /api/v1/remediation-sessions/{session_id}/inputs
GET  /api/v1/remediation-sessions/{session_id}/events
GET  /api/v1/remediation-sessions/{session_id}/patches/{patch_id}
```

`inputs` 接收 request ID、typed value、expected version 和 request key。`events` 提供稳定时间线；`patches` 返回授权范围内的结构化 diff 和验证摘要。继续规划复用 `advance`，不再创建一套重复的 `/agent/sessions` 资源。

### 10.3 新增持久化对象

| 对象 | 关键内容 |
|---|---|
| EvidenceSnapshot | 当前轮 Evidence、Contract、diagnosis 与 code snapshot digests |
| InputRequest/InputResponse | 类型、约束、Evidence 引用、版本、脱敏值 |
| CodePatchSet/CodePatchFile | preimage hash、diff、行数、风险、validation profiles |
| ApprovalBinding | actor、session version、Evidence/Contract/patch 联合摘要 |
| RepairWorkspace | 隔离位置、source snapshot、准备/验证状态 |
| ManagedRepairArtifact | artifact digest、patch digest、validation summary |
| ValidationResult | profile、状态、结构化输出、耗时和限制 |

现有 `AgentTurn`、`ActionProposal`、`ActionDecision`、`ActionExecution`、`EvaluationResult` 和 `RemediationEvent` 继续作为主审计链。新增对象不另建平行状态机。

所有查询和写入执行 owner 校验；所有写操作使用乐观版本和幂等键。服务重启后，worker 可从持久化状态恢复。API 返回值不得包含 provider key、秘密环境变量、未脱敏日志或工作目录之外的路径。

## 11. 前端交互

Agent 使用引导式修复会话，不增加通用聊天入口。

### 11.1 用户流程

1. 在失败 Run 详情页或 Agent workspace 选择 Run，点击“诊断并修复”。
2. 会话页显示 Evidence 收集、诊断和规划时间线。
3. 诊断卡显示结论、置信度、fact IDs 和可展开证据。
4. 缺少信息时显示按 Schema 渲染的输入表单。
5. Contract 修复显示字段级 before/after；代码修复显示逐文件 unified diff。
6. 审批卡明确展示动作、风险、Evidence 版本、验证 profile 和预计资源变化。
7. 批准后展示隔离准备、静态/测试验证、派生 Contract、提交、运行和评价进度。
8. 完成后展示源 Run 与派生 Run 的状态、资源、诊断和输出对比。
9. 失败时提供“继续规划”“转 Repair Ticket”“人工接管”，而不是只显示原始 JSON。

### 11.2 必须补齐的 UI 断点

- 渲染 `session.turns`、自然语言摘要、facts、citations 和 stop conditions。
- 为 `awaiting_input` 增加类型化表单、校验错误和提交反馈。
- 为 `code_patch` 增加 diff viewer、文件级折叠、风险和验证结果。
- 审批按钮显示其绑定摘要；Evidence 过期时禁用并提示重新规划。
- 将 executions/evaluations 从原始 JSON 升级为阶段时间线与前后对比卡。
- 保留原始 JSON 作为审计详情，而不是主交互界面。

## 12. 评价与成功定义

评价必须基于派生 Run 的新 Evidence。会话只有同时满足以下条件才能进入 `succeeded`：

```text
derived Run == SUCCEEDED
AND Evidence collection complete
AND expected outputs verified
AND source primary diagnosis no longer present
AND approved Contract/patch and validation records traceable
```

代码修复还必须存在通过的 Patch Gate、固定验证结果和 ManagedRepairArtifact digest。

若派生 Run 退出码为 0，但 Evidence 不完整或 expected outputs 无法验证，结果为现有的 `execution_success_unverified`，会话进入 `blocked`，不得显示“修复成功”。

资源优化建议除上述条件外还展示请求资源、实际用量、排队时间和运行时间的前后变化。优化指标没有改善不代表作业失败，但必须明确标为“运行正确、优化效果未证实”。

## 13. 异常与降级

| 情况 | 行为 |
|---|---|
| LLM 不可用 | 资源/路径问题回退规则；代码修复进入 blocked，可恢复或转票据 |
| LLM Schema 非法 | 一次格式修复，仍失败则 fallback/blocked |
| Evidence、Contract 或源码变化 | 旧审批失效，重新冻结并规划 |
| 补丁不可应用 | 不改源目录、不提交 Run，记录结构化失败 |
| 固定验证失败 | 保留结果供下一轮，未通过不得提交 |
| 执行后端不可用 | 保留已批准计划，标记环境异常，不误判修复失败 |
| 达到 attempt/submission/token/time 预算 | `exhausted`，禁止无限循环 |
| 权限或路径越界 | fail closed，记录 Policy Gate 原因 |
| Evidence 不完整 | 只读 probe、请求输入或 blocked，不猜测 |
| 日志/源码提示词注入 | 作为数据展示，不影响允许动作和验证 profile |

## 14. 测试策略

### 14.1 单元测试

- RemediationPlan V2 解析、一次格式重试和 token 计量。
- fact ID 引用、动作 allowlist、Contract patch allowlist。
- 输入 Schema、禁止秘密值和版本冲突。
- Patch parser、路径规范化、文件/行数限制、preimage hash 和联合审批摘要。
- Python、Bash、SBATCH 固定验证器。
- 状态迁移、预算耗尽、陈旧审批和幂等执行。
- success、execution_success_unverified、failed 和 inconclusive 评价。

### 14.2 集成测试

- Evidence → diagnose → plan → approval → derived Contract/Run → evaluation。
- awaiting_input → submit typed input → replan → approval。
- Python code context → code patch → isolated workspace → validation → derived Run。
- Bash/Slurm patch → directive validation → derived Run。
- provider failure → deterministic fallback/blocked。
- worker/API 重启后会话恢复及租约接管。

### 14.3 API 与权限测试

- owner 隔离和跨用户 patch/evidence 拒绝。
- expected version 冲突与重复 request key。
- 过期 patch、过期 Evidence 和重复 execute。
- diff、日志和错误响应的敏感信息脱敏。
- 非法路径、符号链接、二进制文件和任意命令字段拒绝。

### 14.4 浏览器端到端测试

- 从失败 Run 创建 Agent Session。
- 查看 Evidence 引用并提交 required input。
- 查看并批准 Contract diff。
- 查看并批准 Python/Bash/Slurm diff。
- 跟踪隔离验证和派生 Run。
- 查看修复前后比较和严格成功/未验证状态。
- Evidence 变化后 UI 自动禁用旧审批。

## 15. 比赛验收场景

全部场景先在本地 Docker Slurm 模拟环境验收：

1. **OOM**：Agent 引用资源 Evidence，提出内存调整，批准后派生 Run 成功。
2. **超时/资源浪费**：调整 time/CPU，并展示请求量与实际用量前后比较。
3. **非法 partition/QoS**：根据 capability facts 选择合法值，不允许模型自由填写。
4. **环境/依赖错误**：进入 `awaiting_input`，从预注册环境中选择后成功。
5. **路径错误**：修正受控 workdir，或在 Evidence 指向代码时进入补丁流程。
6. **瞬时错误**：在预算内提出一次安全重试，并保留 lineage。
7. **Python 错误**：根据 traceback 定位 `.py`，生成 diff、审批、固定验证并重新运行成功。
8. **Bash/Slurm 错误**：修复 `.sh`/`.sbatch` 语法或 `#SBATCH` 指令并成功。
9. **不安全补丁**：越界路径、敏感文件、新文件、任意命令或超限 diff 被阻止。
10. **过期审批**：源文件、Contract 或 Evidence 改变后，旧批准不能执行。
11. **退出 0 但输出缺失**：评价为 `execution_success_unverified`，不能宣告成功。
12. **LLM/执行环境异常**：会话可解释、可恢复、可转人工，不丢审计链。

每个验收场景保存：session event ledger、provider/model/token 记录、Evidence/fact 引用、输入、审批摘要、Contract/代码 diff、验证结果、派生 Run、输出检查和前后对比报告。

## 16. 实施切片与完成条件

### Slice 1：接通现有修复主链

- 将 `RemediationPlanService` 接入 live planning。
- 统一规则建议与 LLM 计划的 Proposal Compiler/Policy Gate。
- 补齐 `awaiting_input` API、持久化和 UI。
- 收紧审批摘要、token 统计和成功评价。

完成标志：OOM、环境、路径和重试场景可在本地模拟器形成完整闭环。

### Slice 2：Python 代码修复

- 新增 CodePatchSet、RepairWorkspace、ManagedRepairArtifact。
- 实现 Python 定位、Patch Gate、隔离应用和固定验证。
- 接入派生 Contract/Run 和前后评价。

完成标志：一个真实 Python 失败作业经人工批准 diff 后运行成功，原目录未被修改。

### Slice 3：Bash/Slurm 代码修复

- 扩展 `.sh`、`.sbatch` 定位和策略。
- 增加 Bash 与 SBATCH 固定验证器。
- 完成脚本错误 E2E。

完成标志：脚本语法和 SBATCH 指令错误均能安全修复并验证。

### Slice 4：比赛演示与证据

- 完成 Agent 时间线、输入表单、diff 和比较 UI。
- 固化 12 个本地模拟验收场景和 competition smoke evidence。
- 验证服务重启、越权、陈旧审批和 provider 降级。

完成标志：不依赖远程 VM，可以从 Web 完整演示“失败 → 诊断 → 代码/资源修复 → 审批 → 派生 Run → 严格验证”。

## 17. 当前缺口到目标能力的映射

| 当前情况 | 目标补充 | 优先级 |
|---|---|---|
| LLM 主要解释，结构化 planner 未接 live | Hybrid Planner + Proposal Compiler 正式接链 | P0 |
| `awaiting_input` 无输入提交路径 | typed input API、持久化、表单和 replan | P0 |
| Contract patch 字段可能多处定义 | 以 Contract Schema + 单一 allowlist 编译 | P0 |
| Repair Ticket 仅 metadata handoff | 独立 CodePatchSet/RepairWorkspace/Artifact | P0 |
| 代码上下文只用于解释/票据 | 扩展为精确快照、补丁和陈旧性绑定 | P0 |
| 无补丁应用与固定验证 | Patch Gate + isolated apply + validator registry | P0 |
| UI 以 proposal/raw JSON 为主 | Evidence、输入、diff、验证、比较时间线 | P1 |
| 评价可能缺少严格输出闭环 | 状态 + Evidence + outputs + diagnosis 联合判定 | P0 |
| 远程 VM 当前不可用 | 本地模拟器成为开发与比赛验收主路径 | P0 前提 |

## 18. 外部前提与风险

- 本地 Slurm simulator 的 submit、worker、Evidence collection 主链必须健康；Agent 不应掩盖执行底座故障。
- 当前本地 command gateway 与 controller 版本兼容性问题属于执行底座前提，应在 Agent E2E 前单独恢复，但不改变本设计边界。
- LLM 能处理的代码错误范围不可无限承诺；首版以 traceback/脚本定位明确、补丁规模受限的错误为目标。
- 项目预声明测试可能执行用户代码，因此必须使用隔离验证 worker；没有合法测试 profile 时只做静态验证，并在 UI 明示验证强度。
- 真实平台接入时只替换 workspace/execution adapter，不放宽动作、路径、审批和验证策略。

## 19. 设计不变量

1. Evidence 和规则是事实来源，LLM 不是执行权威。
2. 所有 mutation 必须展示并经用户审批。
3. 不向 LLM 开放任意 Shell、argv、秘密或管理员能力。
4. 源 workspace 永不被 Agent 原地修改。
5. 审批绑定 Evidence、Contract、source snapshot 和 patch digest；任一变化即失效。
6. 代码只能通过允许扩展名、Patch Gate、隔离副本和固定验证进入派生 Run。
7. 派生 Run 退出 0 不等于修复成功；必须验证 Evidence、输出和原诊断消失。
8. 所有动作可审计、可幂等恢复、受 owner 和预算约束。
9. Repair Ticket 保持人工交接语义，不伪装成自动代码修复。
10. 远程 VM 不可用不阻塞本阶段交付和验收。
