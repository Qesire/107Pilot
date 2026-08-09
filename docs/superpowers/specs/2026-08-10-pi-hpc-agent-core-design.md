# 107Pilot Pi HPC Agent Core 总体设计

- 日期：2026-08-10
- 状态：设计已逐节确认；实现未开始
- 核心选择：嵌入 `pi-agent-core`，107Pilot 保留持久编排、身份、工具、工作区和 Slurm 执行权
- 环境边界：先在本地 Docker Slurm simulator 验证；远程 VM 当前不可用且不作为前置条件
- 对应研究：`docs/research/2026-08-10-pi-hpc-agent-architecture-research.md`
- 本轮不包含：前端页面设计、课程自动批改、四类领域模板内容建设、初学者通用知识库建设

## 1. 背景

LLM 编码 Agent 能显著帮助学生创建、迁移、运行和修复实验工程，但学校反馈 Slurm 登录节点无法承受所有用户各自常驻 Claude、Hermes 或类似大型 Agent。把完整 Agent 转移到学生个人电脑虽然保护登录节点，却会引入高延迟 SSH 工具循环、文件状态漂移、认证恢复困难和无法统一审计等问题。

107Pilot 当前已经拥有：

- Contract、Run、Slurm backend 和幂等提交；
- owner-bound SSH relay、ControlMaster 和文件传输设计；
- Evidence、Diagnosis、Runtime Watch 和资源观测；
- 持久 Worker/outbox、lease、heartbeat 和恢复；
- Remediation Session、审批、派生 Run 和代码上下文；
- Template Market、发布审核和采用链路。

缺少的是统一、可持久、可创建工程的 Agent 内核，以及把本地文件操作、集群控制和重计算分配到正确执行位置的协议。

## 2. 产品定位

107Pilot 不定位为：

- 在登录节点托管完整 Claude/Hermes/Pi CLI；
- 浏览器到远端 shell 的通用代理；
- 要求每个学生在个人电脑自行配置的 SSH 编码工具；
- 无边界的自动软件开发外包系统。

107Pilot Agent 定位为：

> 面向 HPC 实验生命周期的集中式、证据驱动、可恢复 Agent 控制面。它把自然语言目标编译为受用户身份、工作区、资源预算、平台事实和 Slurm 调度约束的工程变更与实验运行。

Agent 必须同时支持创建与修复。P0 创建能力的完成标准不是空目录或脚手架，而是一个经过最小验证、可以生成 Contract 并提交 Slurm 的实验工程。

## 3. 已确认的架构决策

1. 正式内核采用 `pi-agent-core`，不直接部署完整 Pi CLI。
2. Pi 只负责一个短时 Turn 内的推理、工具循环和事件；107Pilot 数据库是持久真源。
3. 模型与 Agent Turn 运行在 107Pilot 应用侧；登录节点只作为受限中继。
4. CUDA、MPI、真实 module、长时构建和实验执行进入 Slurm allocation。
5. 持久的是 AgentSession，不是每用户 Agent 进程；休眠会话对应零常驻 Pi 进程。
6. 集群工作区是真源；Agent 默认编辑应用侧隔离副本。
7. Agent 支持 `blank | template | existing | failed_run` 四种工程来源。
8. 代码和配置可进入 AgentWorkspace；数据集、checkpoint 和 5GB 以上权重只进入 metadata。
9. Pi 不获得通用宿主 shell、SSH、Slurm token、MFA 或用户凭据。
10. 长工具调用返回 durable AgentTask，当前 Turn 立即释放，任务完成后由事件恢复。
11. 小型验证可在用户预先批准的 ResourceEnvelope 内自动运行；正式实验和 ChangeSet 发布仍需明确确认。
12. 用户项目中的 Pi extensions、packages、MCP servers 和 skills 默认不加载。
13. 普通 owner-scoped Agent 上下文使用校内自部署模型，不做过度脱敏；从成功 Run 发布共享模板时必须严格脱敏。
14. Pi 精确锁版；先用薄适配层，不立即维护大型 fork。

## 4. 方案比较

### 4.1 每用户登录节点 Agent

完整 Agent 直接读取远端工作区并运行 shell。环境一致，但进程、上下文、索引、子进程和轮询全部落在共享登录节点，无法满足学校负载约束。拒绝。

### 4.2 每用户个人电脑 Agent

学校侧成本低，但把安装、模型、SSH、MFA、文件同步和持久会话问题全部交给学生，也使 107Pilot 无法提供统一 Evidence、预算和恢复。可作为高级用户个人选择，不作为产品主线。

### 4.3 Agent brain 作为 Slurm 作业

保护登录节点，但对话受排队影响，等待用户时浪费 allocation，且计算节点模型网络未知。Agent brain 不采用该位置；只有环境相关工具进入 Slurm。

### 4.4 集中式短时 Turn（采用）

应用侧保存会话、运行 Pi 和代码镜像；登录面只执行受限控制/同步；计算节点执行验证与实验。该方案可以统一身份、Evidence、预算和恢复，并让资源占用只随活动 Turn 数增长。

## 5. Pi 采用边界

### 5.1 Pi 负责

- LLM streaming；
- 单 Turn tool-call loop；
- message/tool 事件顺序；
- steering 和 follow-up；
- `beforeToolCall`、`afterToolCall` 和 stop hooks；
- Turn 内 Agent state。

### 5.2 107Pilot 负责

- AgentSession、AgentTurn 和领域状态机；
- 用户/Slurm 身份和授权；
- ModelProfile、prompt、context 和工具选择；
- Policy Gate、审批与资源预算；
- 工作区快照、隔离副本、ChangeSet 与安全发布；
- Slurm AgentTask、Run、Evidence 和 Runtime Watch；
- 幂等、lease、fencing、恢复和审计；
- 工具 schema 和 Pi 版本治理。

### 5.3 明确不复用的 Pi 默认行为

- 不启用内建 `bash`、`write`、`edit` 直接访问宿主机；
- 不启用 Pi SSH extension；
- 不自动发现 `.pi/`、项目 extension 或项目 package；
- 不把 Pi JSONL 文件作为业务真源；
- 不让 Pi 自己存储模型密钥或集群凭据；
- 不允许模型通过自由文本选择可执行程序或远端目标。

## 6. 总体架构

```text
Browser
   │
107Pilot Python API
   │ message + outbox
   ▼
Agent Orchestration Worker
   │ claim AgentTurn lease
   ▼
pilot-agentd
  TypeScript / pi-agent-core
   ├── Campus LLM Gateway
   └── Internal Tool Gateway
             ├── Context / Platform / Evidence
             ├── Workspace / Sandbox / ChangeSet
             ├── Template / Contract / Run
             └── SSH / REST / Slurm adapters
                              │
                         AgentTask / Run
                              │
                       Slurm compute nodes
```

信任关系：

- Python control plane 是身份、状态和副作用权威；
- `pilot-agentd` 是无凭据、无工作区挂载的推理执行器；
- Workspace Sandbox 是短时隔离执行面；
- SSH/REST adapters 只接受经过策略校验的 typed operation；
- 计算节点只运行 Slurm 已分配的验证或实验任务。

## 7. 运行与性能模型

### 7.1 持久会话、短时 Turn

```text
AgentSession persisted
→ message/event wakes session
→ AgentTurn queued
→ worker loads bounded state
→ Pi runs
→ checkpoint persisted
→ Pi instance released
```

- 学生空闲时没有对应 Pi 进程；
- 同一学生默认最多一个活动 Turn；
- 全局活动 Turn 数由部署队列限制；
- AgentTask PENDING/RUNNING 时不保留 Pi Turn；
- 浏览器断线不影响 Turn 或 Task；
- 模型推理由校内集中式 gateway 承担。

### 7.2 AgentSession 状态

```text
idle
→ queued
→ running
    ├─ waiting_user
    ├─ waiting_approval
    ├─ waiting_slurm_task
    ├─ paused_auth
    ├─ idle
    └─ failed
```

领域工作流拥有更细状态，但必须映射到该运行状态。

### 7.3 三类执行位置

| 类别 | 位置 | 例子 |
|---|---|---|
| 轻量上下文与代码操作 | 应用侧 AgentWorkspace/Sandbox | 搜索、读取、diff、语法检查、小型测试 |
| 集群控制与有界读取 | SSH/REST relay | 作业查询、短日志、提交、取消、文件同步 |
| 环境相关或重计算 | Slurm compute allocation | CUDA、MPI、module、编译、训练、模拟、正式运行 |

### 7.4 上下文控制

- 每 Turn 只加载与当前 Profile 和状态有关的上下文；
- 代码搜索返回片段，不加载完整仓库；
- 日志使用 Runtime Watch cursor 和相关窗口；
- PlatformSnapshot、文件索引和模板 metadata 可缓存；
- 达到阈值时产生结构化 checkpoint；
- 原始 Evidence 和历史消息继续持久保存，但不全部进入模型。

具体 Pi CPU、内存和吞吐不先验承诺，必须通过本地 benchmark 得出应用节点推荐配置。

## 8. 统一实验工程入口

### 8.1 `ExperimentProjectSession`

```yaml
origin: blank | template | existing | failed_run
goal:
owner:
source_workspace:
profile:
state:
workspace_snapshot_id:
agent_workspace_id:
resource_envelope_id:
active_tasks:
outcome:
```

四种 origin 共用工作区、验证、ChangeSet、Contract 和 Run 协议，不创建四套 Agent。

### 8.2 从零创建

```text
用户自然语言目标
→ 收集最少必要信息
→ ProjectBlueprint
→ 隔离 AgentWorkspace
→ 创建代码/配置/测试/说明
→ sandbox smoke
→ Slurm environment validation
→ ChangeSet + Contract
→ 用户确认
→ 发布与正式运行
```

`ProjectBlueprint` 至少包含：

```yaml
goal:
project_type:
expected_inputs:
expected_outputs:
file_plan:
runtime_environment:
dependency_plan:
resource_assumptions:
validation_plan:
scientific_assumptions:
unknowns:
```

### 8.3 P0 创建完成标准

一个 P0 工程至少包含：

- 可读的项目结构；
- 实验入口代码；
- 参数配置；
- 环境或 module 声明；
- Slurm 脚本或 Contract；
- 最小 test/smoke；
- 输入输出说明；
- README/运行说明；
- 至少一次隔离或 Slurm 环境验证；
- 未验证科学假设和剩余风险。

P0 不承诺自动完成大型生产系统、任意公网依赖安装、数据集下载、超大模型构建或科学结论证明。

## 9. WorkspaceSnapshot 与 AgentWorkspace

### 9.1 真源

集群原始工作区是真源。应用侧副本是有基线的编辑和验证空间，不允许模型直接覆盖远端。

### 9.2 `WorkspaceSnapshot`

至少包含：

```text
snapshot_id
owner / connection_id
canonical_workspace
git_head / branch / dirty
file manifest: relative path / size / mode / mtime / digest
editable / readonly / metadata-only classification
project instructions and build hints
created_at / source capabilities
```

Git 不是硬依赖；非 Git 项目使用 manifest 和 digest 检测冲突。

### 9.3 文件分类

- 代码、配置、脚本、小型文本日志：允许镜像；
- 数据集、checkpoint、大型压缩包、5GB 以上权重：metadata-only；
- `.git` 内部、私钥、证书、socket、credentials：排除；
- symlink 必须解析并验证仍在 owner allowed roots 内，否则拒绝。

### 9.4 `AgentWorkspace`

```text
agent-workspaces/<owner>/<session_id>/
├── source/
├── metadata/
├── artifacts/
└── state/
```

每个工作区有空间、inode、CPU、内存、活动时间和 TTL 配额。休眠时可以回收 source/index，只保留基线、patch、checkpoint 和 artifacts。

### 9.5 工作区工具

- `workspace_list`
- `workspace_search`
- `workspace_read`
- `workspace_status`
- `workspace_apply_patch`
- `workspace_create_file`
- `workspace_revert_file`
- `workspace_diff`
- `workspace_create_changeset`

所有路径使用工作区相对路径；写入只发生在 `source/`。

### 9.6 `sandbox_exec`

通用宿主/登录节点 shell 被禁止，但应用侧短时 Sandbox 可以执行编码所需命令：

- 非 root；
- 默认无网络；
- 只挂载当前 AgentWorkspace；
- 无 SSH、Slurm token、MFA 或宿主凭据；
- 有 CPU、memory、pids、walltime、输出和磁盘配额；
- 适合 lint、语法检查、小型单元测试和格式化；
- 真实 HPC 环境不满足时转 AgentTask。

## 10. WorkspaceChangeSet 与发布

### 10.1 数据模型

```yaml
WorkspaceChangeSet:
  changeset_id:
  owner:
  session_id:
  base_snapshot_id:
  changed_files:
  created_files:
  deleted_files:
  unified_diff:
  local_validation:
  cluster_validation:
  assumptions:
  risk_level:
  approval_digest:
  publish_state:
```

### 10.2 审批语义

隔离工作区内的小步修改不逐次审批。用户批准的是完整 ChangeSet。以下内容必须突出：

- 删除文件；
- 环境、启动、依赖和 Slurm 脚本变更；
- 大规模重写；
- 未通过验证；
- 科学参数或输出语义变化。

### 10.3 冲突检测

发布前重新获取远端目标摘要：

- 与基线一致：允许发布；
- 任一目标已变化：进入 `workspace_conflict`；
- Agent 可以基于新快照重新生成 patch；
- 不允许自动覆盖用户或另一个会话的修改。

### 10.4 可恢复发布

1. 上传到同一文件系统的 `.107pilot/changesets/<id>` 暂存目录；
2. 再次验证 owner、路径、基线和 approval digest；
3. 为被修改文件创建有 TTL 的可恢复备份；
4. 写入 publish journal；
5. 逐文件原子替换；
6. 校验最终摘要；
7. 标记已发布并保留撤销入口。

多文件发布中途失败时，Worker 按 journal 恢复；不得把部分成功伪装成完成。

## 11. 工具协议

### 11.1 工具描述

每个工具除参数 schema 外必须声明：

```yaml
name:
side_effect: none | workspace | cluster
execution: immediate | durable_task
placement: app_sandbox | cluster_relay | slurm_compute
approval: never | policy | always
idempotency: required | optional
max_duration:
max_output:
required_capabilities:
required_connection:
evidence_output:
```

### 11.2 ToolInvocation

```yaml
ToolInvocation:
  invocation_id:
  idempotency_key:
  owner:
  session_id:
  turn_id:
  profile_id:
  tool_name:
  arguments:
  capability_token:
  deadline:
  base_state_version:
```

Tool Gateway 必须重新验证 owner、profile、工具、预算、状态版本和参数，不能信任 Pi 已完成校验。

### 11.3 工具分组

只读上下文：

- `platform_get_snapshot`
- `workspace_list/search/read`
- `template_search`
- `contract_get`
- `run_get`
- `run_log_read`
- `evidence_read`

隔离工作区：

- patch/create/revert/diff/changeset；
- `sandbox_exec`。

集群与发布：

- `validation_schedule`
- `experiment_plan`
- `experiment_submit`
- `task_get`
- `task_cancel`
- `changeset_publish`

不提供通用 `ssh`、`sbatch`、`srun` 或远端 shell 工具。

### 11.4 动态最小工具集

每个 Turn 只加载当前 Profile/状态所需工具。例如等待审批时不加载 workspace write 和 submit；诊断时不加载 template publication。工具减少既降低 prompt 占用，也缩小越权面。

## 12. AgentTask 与异步 Slurm

### 12.1 数据模型

```yaml
AgentTask:
  task_id:
  owner:
  session_id:
  originating_turn_id:
  tool_call_id:
  kind: validation | build | environment_probe | experiment
  state:
  resource_request:
  slurm_run_id:
  progress:
  input_request:
  result_evidence_refs:
  error:
  ttl:
  idempotency_key:
  created_at:
  updated_at:
```

### 12.2 生命周期

```text
created
→ admitted
→ submitting
→ pending
→ running
├─ input_required
├─ completed
├─ failed
└─ cancelling → cancelled
```

对 Pi 返回的归一化状态可以使用：

```text
working | input_required | completed | failed | cancelled
```

### 12.3 非阻塞规则

```text
Pi calls validation_schedule
→ AgentTask durably created
→ result returns task_id + terminate=true
→ current Turn completes
→ existing Worker/Runtime Watch advances task
→ terminal EvidenceBundle created
→ agent.task_ready event wakes next Turn
```

Pi 不得用频繁 `task_get` 维持等待循环。数据库和 Worker 是状态真源，通知仅用于降低延迟。

### 12.4 幂等与取消

- AgentTask 必须先持久化再返回；
- Slurm submit response 不确定时通过 marker/job 查询对账；
- 重试不能创建第二个同义 job；
- task 始终绑定 owner/session/auth context；
- cancel 是协作式操作，保留 requested/final 两种事实；
- terminal result 形成 Evidence，不把无限 stdout 直接送回模型。

## 13. AgentResourceEnvelope

```yaml
AgentResourceEnvelope:
  owner:
  session_id:
  max_validation_tasks:
  max_concurrent_tasks:
  cpu_walltime:
  gpu_validation_tasks:
  gpu_walltime:
  max_nodes:
  allowed_partitions:
  allowed_qos:
  expires_at:
```

预算内小型验证可以自动迭代。以下动作仍需单独确认：

- 正式实验提交；
- 超预算或提高 GPU/节点/walltime；
- ChangeSet 发布到集群真源；
- 删除或覆盖重要制品；
- 共享模板发布。

## 14. 统一 Agent Profiles

```yaml
PilotAgentProfile:
  profile_id:
  goal:
  accepted_entrypoints:
  state_schema:
  context_providers:
  allowed_tools:
  resource_policy:
  approval_policy:
  completion_criteria:
  fallback_behavior:
```

模型不能自行切换到权限更高的 Profile。跨领域操作由 Orchestrator 创建关联工作流并重新计算工具集。

### 14.1 `experiment_builder`

支持 blank/template/existing/failed_run，从目标理解、创建或编辑、验证，到 ChangeSet、Contract 和 Run。它是主要的通用实验编码 Agent。

### 14.2 `template_application`

支持：

```text
用户选择模板 → Agent 实例化
用户描述任务 → Agent 搜索/选择/实例化
```

Agent 使用模板 schema、平台事实和项目上下文；没有合适模板时明确 `no_suitable_template` 并转 experiment builder，不伪造匹配。

### 14.3 `run_diagnosis_repair`

从失败/异常 Run 进入，读取 Runtime Watch、资源观测、日志和代码快照，在隔离工作区修复并提交验证，最终产生 ChangeSet、Contract 或派生 Run。

### 14.4 `template_publication`

从成功 Run 提取可复用模板，严格清除用户路径、账号、Run ID、数据集、私有参数和制品引用，将变化值提升为 schema 参数，并通过发布 gate。

### 14.5 `platform_coach`

后续轻量 Profile，用于 Slurm 解释和平台问答，并可将对话转为模板应用或实验创建。当前不建设完整初学者知识库。

## 15. 领域工作流与完成标准

通用状态：

```text
scoping
→ inspecting
→ planning
→ editing
→ validating
├─ waiting_task
├─ waiting_user
├─ ready_for_review
└─ blocked

ready_for_review
→ waiting_approval
→ publishing
→ completed
```

每个 Profile 必须产生结构化 `AgentOutcome`：

```yaml
status: completed | blocked | partial
artifacts:
  project_blueprint_id:
  changeset_id:
  contract_id:
  run_id:
  template_draft_id:
evidence_refs:
validation_summary:
user_decisions:
remaining_risks:
next_actions:
```

仅生成自然语言回答不等于完成。代码任务至少应达到“形成经验证的待批准 ChangeSet”或基于 Evidence 明确记录无法继续的阻塞条件。

## 16. Agent Context

`AgentContextAssembler` 按层构造每 Turn 输入：

```text
system: Profile、工具、安全和平台规则
task: 用户目标、已确认约束、状态、剩余预算
project: 相关文件片段、项目树、构建说明、基线摘要
runtime: Contract、Run、日志窗口、资源统计、Evidence
memory: 决定、失败尝试、待办、最近 checkpoint
```

项目 README/AGENTS.md 等作为有来源的低优先级指导：

- 不得覆盖 107Pilot 系统规则；
- 不得增加工具或权限；
- 不得改变 owner、allowed roots、预算或审批；
- 不自动执行其中命令。

## 17. 模型不可用与确定性降级

- 平台查询、文件管理、模板 schema 填充、规则诊断、Task/Run 推进继续工作；
- 模板应用进入确定性参数收集；
- 已创建的传输、AgentTask 和 Run 不受影响；
- 需要新代码推理或补丁时进入 `blocked:model_unavailable`；
- 不把确定性 fallback 伪装成智能代码修复完成。

## 18. `pilot-agentd` 部署

### 18.1 正式路径

独立 TypeScript 服务直接嵌入 `pi-agent-core`。完整 Pi RPC 子进程只用于 A0 spike，不进入生产路径。

### 18.2 共享服务

- `pilot-agentd` 是共享 Worker 池，不按学生常驻；
- Turn 请求包含可恢复 state 和 version；
- 实例可以水平扩展，无需 sticky session；
- Python Worker 持有 Turn lease 和公平队列；
- 一个实例的并发数由 CPU、memory 和模型 gateway 限制。

### 18.3 容器边界

`pilot-agentd`：

- 非 root、只读根文件系统、drop capabilities；
- 不挂载学生工作区或 Docker socket；
- 无 SSH/Slurm/MFA 凭据；
- 只访问 campus LLM 和 Internal Tool Gateway；
- 固定 Pi/provider/prompt/schema 版本；
- 禁止运行时 npm install 和资源自动发现。

Workspace Sandbox 使用单独短时容器。两者不能共享权限扩大路径。

## 19. Python—TypeScript Turn 协议

### 19.1 `AgentTurnRequest`

```yaml
protocol_version:
session_id:
turn_id:
owner:
profile_id:
state_version:
model_profile:
system_prompt_version:
messages:
context_bundle:
available_tools:
resource_envelope:
capability_token:
deadline:
```

### 19.2 事件流

```text
turn_started
message_delta
tool_call_requested
tool_call_started
tool_call_progress
tool_call_completed
checkpoint
turn_completed
turn_failed
```

事件使用 `turn_id + sequence` 去重；浏览器只订阅已持久事件。

### 19.3 Capability token

短期 token 绑定：

- owner；
- session/turn；
- profile；
- 工具集合；
- resource envelope；
- expiry。

token 不是集群凭据，不能被换取 SSH 私钥、MFA 或通用远端执行。

## 20. 持久数据与恢复

### 20.1 `AgentSession`

```text
session_id / owner / profile / state / state_version
messages / context_checkpoint
active_tasks / approvals / resource_usage
outcome / created_at / updated_at
```

### 20.2 `AgentTurn`

```text
turn_id / session_id / owner
input_digest / state_version
pi_version / model_profile / prompt_version / tool_schema_version
lease_owner / lease_expires_at / fencing_token
event_sequence / final_checkpoint / outcome
created_at / started_at / finished_at
```

### 20.3 恢复规则

- Worker 先持久化 Turn 和 lease，再调用 agentd；
- 事件先写 durable store，再发布浏览器；
- agentd 崩溃后 Turn 进入 interrupted；
- 无副作用时可以重跑；
- 已产生工具副作用时从最后完整 tool result 恢复；
- ToolInvocation 使用稳定幂等键；
- 旧 state version 或 fencing token 的写回被拒绝；
- 已提交 Slurm/传输任务只对账，不重复创建。

## 21. Schema 与版本治理

仓库新增共享 JSON Schema：

```text
schemas/agent/v1/
├── turn-request.schema.json
├── event.schema.json
├── tool-invocation.schema.json
├── tool-result.schema.json
├── agent-task.schema.json
└── changeset.schema.json
```

Python 和 TypeScript 都以 schema 做运行时校验，并运行跨语言 golden/contract tests。每个 Turn 记录 Pi 精确版本、ModelProfile、prompt digest、Profile version、tool schema version 和 PlatformSnapshot ID。

Pi 升级流程：

1. 更新精确依赖和 lockfile；
2. 运行 fake provider tool trajectory tests；
3. 运行 session restore、abort、streaming 和 schema tests；
4. 运行本地 Slurm 纵向 smoke；
5. 对比事件轨迹和 token/context 行为；
6. 通过后更新允许版本。

如果适配器和 hooks 足以实现需求，不维护 fork；只有缺失无法外置的关键语义时才评估受控 fork。

## 22. 现有代码迁移

| 当前模块 | 迁移方式 |
|---|---|
| `core/agent.py` | 保留为确定性解释/旧 API 兼容层；不立即删除 |
| `core/code_context.py` | 扩展/适配为 WorkspaceSnapshot context provider |
| `services/remediation_service.py` | 接入 `run_diagnosis_repair` Profile，不重写既有领域约束 |
| `adapters/ssh_relay.py` | 继续作为 owner-bound cluster relay |
| Worker/outbox | 驱动 AgentTurn/AgentTask，复用 lease 和 recovery |
| Evidence/Diagnosis | 作为事实输入与 AgentTask 输出 |
| PlatformSnapshot | 构造动态 CapabilitySnapshot |
| Runtime Watch | 提供运行中日志与异常唤醒 |
| Template Market | 提供应用和发布领域服务 |
| Contract/Run | 提供最终提交、对账和 lineage |
| 文件传输 | 提供 SFTP baseline、可选 rsync、manifest 和大文件路径 |

新增建议边界：

```text
services/pilot-agentd/
src/pilot107/agent/
schemas/agent/v1/
```

Pi Node 依赖和 lockfile 与现有前端依赖分离。

## 23. 实现阶段

### A0：Pi compatibility spike

- 校内 OpenAI-compatible provider；
- fake provider；
- custom tools only；
- state round-trip；
- abort/restore；
- Python 与 Pi 最小协议。

### A1：只读 Agent Turn

- Session/Turn stores；
- pilot-agentd；
- platform/workspace read/run/log/evidence tools；
- browser/worker/agentd restart recovery。

### A2：从零创建与隔离编辑

- ExperimentProjectSession blank origin；
- ProjectBlueprint；
- AgentWorkspace；
- patch/diff/ChangeSet；
- sandbox_exec；
- 本地可运行多文件工程。

### A3：异步 Slurm 验证

- AgentTask；
- validation schedule；
- PENDING/RUNNING/terminal；
- Turn release/resume；
- Evidence injection；
- ResourceEnvelope。

### A4：安全发布与正式运行

- remote conflict；
- recoverable publish；
- Contract；
- explicit approval；
- Run + Runtime Watch。

### A5：修复和模板 Profiles

- failed Run diagnosis/repair；
- template application；
- successful Run publication sanitization。

## 24. 本地模拟环境

```text
pilot-api
pilot-worker
pilot-agentd
fake-campus-llm
workspace-sandbox
cluster-access-sim
slurm-sim
```

远程 VM 不可用期间，所有 P0 完成声明必须来自该环境的客观证据。

## 25. 本地验收矩阵

| 场景 | 预期 |
|---|---|
| blank 创建 Python 实验 | 代码、测试、Contract、成功 Slurm Run |
| blank 创建多文件工程 | 只在 AgentWorkspace 修改并生成 ChangeSet |
| 已有代码语法错误 | Agent 定位、修改、验证 |
| sandbox 缺依赖 | 转 Slurm 验证或 DependencyPlan |
| Slurm 长 PENDING | Pi Turn 已释放，Task 仍推进 |
| agentd 在调用前退出 | lease 过期后安全重试 |
| agentd 在 submit 后退出 | 对账且不重复提交 |
| 用户并发修改远端文件 | `workspace_conflict` |
| SSH/MFA 失效 | `paused_auth`，恢复后继续 |
| 5GB 权重 | metadata-only，不进入 context/mirror |
| 项目提示要求越权 | Tool Gateway 拒绝 |
| 模型不可用 | 确定性能力继续，代码创建明确 blocked |
| 两 owner 并发 | workspace/task/evidence 不串线 |
| 浏览器断线 | 重连后读取 durable events |
| Worker/agentd 重启 | 从 checkpoint/Task 恢复 |

## 26. 性能验收

- 空闲会话对应零常驻 Agent 进程；
- 资源使用随活动 Turn 数而不是总会话数增长；
- 等待 AgentTask 时不占用 Pi Turn；
- 应用节点不运行 Slurm 重计算；
- 登录节点没有 Pi/Node/Python Agent 常驻进程；
- workspace 搜索优先使用应用侧索引；
- 远端短命令数量和字节量受预算约束；
- 大文件不会被误复制或送入模型；
- 达到资源上限时公平排队而不是过载；
- fake model 并发压测与真实校内模型兼容测试分别报告。

## 27. 首条比赛演示闭环

```text
学生：“创建一个读取参数并进行数值计算的 Slurm 实验”
→ Agent 生成 ProjectBlueprint
→ 创建多文件工程
→ Sandbox 测试
→ Slurm smoke
→ Evidence 验证
→ 用户批准 ChangeSet 和 Contract
→ 正式运行
→ Dashboard/Runtime Watch 展示状态、日志和资源
→ Agent 解释结果
```

随后人为引入代码错误，演示相同 AgentSession/Workspace/Task 基础上的诊断与修复，证明创建和修复是同一个实验工程生命周期。

## 28. 与既有设计的关系

- `2026-08-09-agent-repair-closed-loop-design.md`：保留其 Evidence、审批、隔离修复和派生 Run 约束；开放式 Agent loop 被本规格的 Pi kernel 和 typed tools 取代。
- `2026-08-09-file-discovery-transfer-design.md`：继续作为工作区同步、大文件和集群连接权威设计；Pi 不增加第二条 SSH/文件通道。
- `2026-08-09-resource-observability-design.md`：PlatformSnapshot 和资源观测作为 Agent context，不由 Pi 重复采集。
- `2026-08-10-runtime-watch-design.md`：日志 cursor、运行期异常和 terminal drain 作为 Agent 唤醒与 Evidence 来源。
- 后续模板 Agent 设计必须实现为本规格 Profile，而不是独立 LLM 服务。

如与旧修复设计中的“代码类型或工具限制”冲突，本规格只在隔离 AgentWorkspace 和 Sandbox 内扩大创建/编辑能力；远端发布、审批、owner、Evidence 和 Slurm 边界不放宽。

## 29. 非目标

- 本轮不设计前端布局；
- 不在远程 VM 上进行验收；
- 不实现每用户常驻 Agent；
- 不允许模型直接使用任意 SSH/shell；
- 不自动加载第三方 Agent 扩展；
- 不自动下载大型数据集或模型；
- 不把运行成功等同于科学正确；
- 不在本轮完成课程自动批改、完整初学者知识库或四领域模板内容。

## 30. 风险与待确认事项

1. 校内模型的 tool calling、streaming 和 context 兼容性需要 A0 真实验证。
2. 应用节点容量未知，需以 benchmark 决定默认并发和 context 上限。
3. 真实 `/public` 挂载未知；正式逻辑必须保持 SFTP baseline 和 capability negotiation。
4. Sandbox runtime 的生产权限需确认；不可用时可以保留文件创建并把验证全部转 Slurm。
5. Pi 上游变化快，需要精确锁版和契约测试。
6. 真实身份凭据刷新仍沿用现有 `paused_auth`/用户恢复，不引入长期凭据存储。
7. 科学有效性只能通过显式领域检查和用户判断逐步增强。

## 31. 完成门槛

本规格的 Agent P0 只有同时满足以下条件才算完成：

1. 不在登录节点运行 Agent brain；
2. 休眠会话无常驻 Pi 进程；
3. Pi 只能调用 107Pilot allowlisted tools；
4. 能从 blank 创建可运行最小实验工程；
5. 能接管 existing/failed_run 并修复重要代码错误；
6. 能异步提交 Slurm 验证并在 Turn 释放后恢复；
7. 能生成可审阅 ChangeSet 和 Contract；
8. 发布前检测冲突且可恢复；
9. 能进入正式 Run、Runtime Watch 和 Evidence；
10. owner、预算、审批、幂等和重启恢复均有本地测试；
11. 5GB 以上权重不进入 Agent context 或代码镜像；
12. 所有完成结论可由本地 simulator 证据复现。
