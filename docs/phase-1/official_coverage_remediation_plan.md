# Official Coverage Remediation Plan

> 状态：repair plan  
> 日期：2026-07-15  
> 上游依据：`docs/official_coverage_audit.md`、官方 `docs-main`、真实 107 probe、当前 Docker Slurm simulator

## 1. 修复目标

本轮修复不把 107Pilot 扩展成 Web Terminal 或任意远程 Shell，而是补齐一条受控、可追溯的事实链路：

```text
官方文档与实时平台事实
→ Platform Observation Layer
→ CapabilityProfile
→ Resource / Workdir Preflight
→ Submit / Run
→ Runtime Evidence
→ Slurm Timeline
→ Diagnosis
→ Simulator Regression
```

核心目标：

- 补齐官方 CLI 平台快照，不能只依赖 REST probe。
- 为每个关键平台事实保留来源、采集时间、可用性和冲突状态。
- 增强运行时证据，能回答“在哪个节点、哪个工作目录、哪个 Python、什么 GPU 状态下运行”。
- 修复模拟器和 107Pilot profile 的漂移，避免预检允许但 Slurm simulator 拒绝。
- 明确产品边界：支持只读采集和命令模板，不支持任意 Shell 执行。

## 2. 产品边界

正式纳入产品能力：

- Slurm 作业提交、查询、取消。
- 平台能力快照。
- 分区、QOS、节点、资源限制展示。
- 工作目录和共享路径预检。
- 作业运行环境取证。
- stdout、stderr、Slurm 状态、输出文件取证。
- 规则化故障诊断。
- 安全的交互式命令或排错命令模板生成。

仅作为引导工作流：

- `srun --pty` 交互式会话。
- `tar`、`cp`、`mv` 等文件操作。
- `nano`、`vim` 等编辑器。
- 用户自定义环境安装命令。

明确排除：

- 任意 Shell 命令执行接口。
- 用户传入字符串后由后端执行。
- Web Terminal。
- 未经白名单约束的删除、移动、压缩操作。
- 自动执行破坏性修复命令。

## 3. 新增 Platform Observation Layer

新增平台观测层，将采集、解析、脱敏、持久化和 profile 合并从 `core/platform.py` 中解耦。

建议新增模块：

```text
src/pilot107/core/platform_snapshot.py
src/pilot107/adapters/platform_cli.py
src/pilot107/adapters/platform_parsers.py
src/pilot107/services/platform_snapshot_service.py
src/pilot107/api/routes/platform_snapshot.py
```

现有 `src/pilot107/core/platform.py` 继续负责输出最终 `CapabilityProfile`，但不直接执行命令。

## 4. 数据模型

### 4.1 ObservedValue

新增统一事实包装类型：

```text
ObservedValue[T]
- value: T | null
- availability: known | unavailable | permission_denied | unsupported | stale
- source_type: cli | rest | official_docs | simulator
- source_name: string
- captured_at: datetime
- expires_at: datetime | null
- raw_artifact: string | null
- warning: string | null
```

用途：

- 区分“值为 0”和“没有探测到”。
- 区分权限不足、平台不支持和静态文档来源。
- 让 Web/API 能显示来源和新鲜度。

### 4.2 PlatformSnapshot

新增领域模型：

```text
PlatformSnapshot
- snapshot_id
- scope: login_node | compute_job | simulator
- captured_at
- collector_version
- command_results
- partitions
- nodes
- qos
- runtime
- storage
- limitations
- redaction_report
```

### 4.3 PartitionSnapshot

至少包含：

- `PartitionName`
- `AllowAccounts`
- `AllowQos`
- `Nodes`
- `State`
- `MaxTime`
- `TRES`
- `Default`
- CPU、内存、GPU 摘要
- 原始状态与归一化状态
- 数据来源

### 4.4 NodeSnapshot

至少包含：

- 脱敏后的节点标识。
- 所属分区。
- 原始状态。
- 归一化状态：`idle`、`mixed`、`allocated`、`completing`、`down`、`draining`、`unknown`。
- CPU 总量和已分配量。
- 内存。
- GRES/GPU 类型与数量。
- 不可用原因。
- 数据来源。

### 4.5 Defaults

默认值必须并列保留，不能互相覆盖：

```text
defaults:
  docs_default:
    partition: Students
    qos: qos_stu_default
  competition_carrier_default:
    partition: Students
    qos: qos_stu_medium_2gpu
  user_selected_default:
    partition: ...
    qos: ...
```

## 5. CLI Platform Snapshot

### 5.1 默认采集命令

基础模式：

```text
hostname
pwd
whoami
date -Is
python -V
which python
scontrol show part
sinfo
squeue -u <current-user>
```

权限允许时：

```text
sacctmgr list qos
df -P -h /public <home>
```

扩展环境模式，默认关闭：

```text
conda env list --json
python -m pip list --format=json
python -m pip freeze
```

`pip freeze` 输出大且可能暴露环境细节，不能默认采集。

### 5.2 安全约束

采集器必须满足：

- 固定命令白名单。
- 结构化 argv。
- 禁止 `shell=True`。
- 每个命令有超时。
- 限制 stdout/stderr 最大长度。
- 设置 `LC_ALL=C`。
- 环境变量采用允许列表。
- 单一命令失败不导致整个快照失败。
- 不保存 JWT、REST token、cookie 或完整环境变量。

### 5.3 原始输出与结构化输出

对关键 Slurm 命令同时保存：

- 官方形式的脱敏 raw 输出，例如 `scontrol show part`。
- 机器解析友好的格式化输出，例如 `sinfo -h -o ...`。

raw 文件必须是脱敏后的版本，未脱敏文本只允许存在于采集进程内存中。

### 5.4 Test-only SSH Observation Lane

当前已获得真实 107 平台 SSH 登录权限，但该权限仅供测试，不能假设未来用户或比赛部署环境会开放。因此 SSH 只能作为一次性或阶段性事实采集通道，不能成为 107Pilot 产品运行依赖。

定位：

- SSH 是 `real_cluster_probe` 的增强输入源。
- SSH 采集结果可进入 `PlatformSnapshot` 和后续 `CapabilityProfile` 合并。
- Web/API/Worker 不能依赖 SSH 才能提交、查询、诊断或展示平台能力。
- 若 SSH 失效，系统应退回到 REST probe、官方文档 profile 和用户上传/管理员提供的 snapshot。

可额外探知的信息：

| 信息类型 | SSH 可采集内容 | 产品处理方式 |
| --- | --- | --- |
| Slurm CLI 原始事实 | `scontrol show part`、`sinfo`、`squeue -u "$USER"` | 作为 LIVE CLI source 写入 PlatformSnapshot |
| QOS / association | `sacctmgr show qos`、`sacctmgr show assoc`，若有权限 | 有权限则结构化解析；无权限记录 `permission_denied` |
| Slurm 配置可见性 | `scontrol show config`，只读 | 只保留必要字段；敏感路径或主机名脱敏 |
| 节点和 GRES | `scontrol show nodes`、`sinfo -o ...` | 解析 CPU、内存、GRES、状态、不可用原因 |
| 登录节点环境 | `hostname`、`pwd`、`whoami`、`date -Is`、`python -V`、`which python` | 标注 scope=`login_node`，不得推断为 compute runtime |
| 存储事实 | `df -P -h /public "$HOME"`、`quota` 若可用 | 记录容量/权限；失败不阻塞 |
| 软件环境 | `module avail`、`conda env list --json`，若存在 | 默认只采集摘要；完整包清单需显式开启 |
| GPU/驱动事实 | 仅在已分配 GPU 的 compute job 中执行 `nvidia-smi` | 登录节点无 GPU 不视为平台无 GPU |
| REST token 辅助 | `scontrol token lifespan=600` | token 只在内存中用于 REST GET，不落盘 |

SSH 采集禁止事项：

- 不执行用户自定义命令。
- 不运行长作业。
- 不修改 Slurm 配置、QOS、account、association。
- 不调用 `scancel` 取消非测试作业。
- 不读取用户项目文件。
- 不持久保存 token、cookie、私钥、完整环境变量。
- 不把 SSH hostname、用户名、完整 home path 原样写入公开报告。

建议新增一个只读脚本：

```text
scripts/real107_probe/probe_real107_cli_snapshot.py
```

运行方式：

```text
python3 probe_real107_cli_snapshot.py --out-dir real107-cli-snapshot
```

输出：

```text
real107-cli-snapshot/
├── manifest.json
├── parsed/
│   ├── partitions.json
│   ├── nodes.json
│   ├── qos.json
│   ├── associations.json
│   ├── runtime.login_node.json
│   └── storage.json
├── raw/
│   ├── scontrol-show-part.txt
│   ├── scontrol-show-nodes.txt
│   ├── scontrol-show-config.txt
│   ├── sinfo.txt
│   ├── squeue.txt
│   ├── sacctmgr-show-qos.txt
│   └── sacctmgr-show-assoc.txt
├── warnings.json
└── redaction-report.json
```

采集结果应同时生成一份差异报告：

```text
real107-cli-vs-rest-diff.json
```

对比维度：

- CLI partitions vs REST `/partitions`。
- CLI nodes vs REST `/nodes`。
- CLI QOS / association vs docs-main QOS table。
- CLI `MaxTime` vs docs-main QOS walltime。
- CLI GRES/TRES vs simulator profile。

SSH 采集结论必须带有生命周期：

```text
source_type: cli
source_name: real107-ssh-test-only
captured_at: ...
expires_at: captured_at + 24h
warning: "SSH access is temporary test access and is not assumed for production."
```

后续系统设计必须支持三种无 SSH 运行模式：

- 使用已回收的脱敏 `PlatformSnapshot` artifact。
- 使用 REST-only probe。
- 使用官方文档 profile + 明确的 stale/unknown 标记。

### 5.5 快照目录

```text
platform/snapshots/<snapshot_id>/
├── manifest.json
├── parsed/
│   ├── partitions.json
│   ├── nodes.json
│   ├── qos.json
│   ├── runtime.json
│   └── storage.json
├── raw/
│   ├── scontrol-show-part.txt
│   ├── sinfo.txt
│   ├── squeue.txt
│   └── runtime.txt
├── warnings.json
└── redaction-report.json
```

## 6. CapabilityProfile 合并策略

按事实类型仲裁，而不是使用全局优先级。

调度运行事实：

```text
实时 CLI > 实时 REST > 模拟器声明 > 静态文档
```

官方政策与资源限制：

```text
实时 sacctmgr / 调度配置
> 当前官方文档
> 内置静态 profile
> 模拟器 profile
```

发生冲突时生成结构化冲突：

```text
CapabilityConflict
- field
- candidate_values
- selected_value
- selection_reason
- severity
```

典型冲突：

```text
Students.MaxTime:
- CLI: 01:00:00
- docs-main: qos_stu_medium_2gpu allows 12h
- result: conflict
- consequence: simulator or live partition may reject a request that profile preflight allowed
```

Web/API 必须显示来源和采集时间。

## 7. Runtime Evidence 补完

> 下一实现切片见 `docs/phase-1/official_coverage_next_slice_design.md`。当前 Evidence
> 已并行写入 `run/request/*`、collector-side `run/environment/basic.json` 和
> `run/timeline/events.jsonl`；后续必须把 runtime probe 前移到 Slurm wrapper
> 的用户脚本之前，并补齐 `run/slurm/squeue-timeline.jsonl`。

### 7.1 提交前请求快照

提交前生成并保存：

```text
run/request/resource-plan.json
run/request/submitted-script.sbatch
run/request/sbatch-argv.json
run/request/preflight-report.json
run/request/capability-profile-ref.json
```

即使 Slurm 提交失败，也应保留这些证据。

### 7.2 Compute Runtime Probe

在 Slurm wrapper 中、用户程序运行前执行轻量 probe。

默认采集：

- `pwd`
- `whoami`
- `date -Is`
- `hostname`
- `python -V`
- Python executable path
- 当前 Conda 环境名
- Slurm 作业相关环境变量允许列表
- 当前工作目录是否位于共享路径

输出：

```text
run/environment/basic.json
```

### 7.3 GPU Probe

当 `ResourcePlan` 请求 GPU 时自动执行：

```text
nvidia-smi --query-gpu=name,uuid,driver_version,memory.total --format=csv,noheader
```

GPU UUID 默认脱敏。

可选 PyTorch probe：

```text
torch.__version__
torch.version.cuda
torch.cuda.is_available()
torch.cuda.device_count()
```

probe 失败不能导致用户作业失败，应记录：

```text
gpu_probe:
  status: unavailable
  reason: command_not_found | no_device | nvml_error | permission_denied
```

### 7.4 环境清单分级

| Level | 内容 | 默认 |
| --- | --- | --- |
| basic | Python 路径、版本、Conda 环境名 | 是 |
| packages | `pip list --format=json` | 否 |
| reproducible | `pip freeze`、完整环境摘要 | 否 |

## 8. Slurm 状态时间线

新增：

```text
run/timeline/events.jsonl
run/slurm/squeue-timeline.jsonl
```

事件类型：

```text
PREFLIGHT_STARTED
PREFLIGHT_PASSED
SUBMISSION_STARTED
JOB_ACCEPTED
JOB_PENDING
PENDING_REASON_CHANGED
JOB_RUNNING
RUNTIME_PROBE_COMPLETED
JOB_COMPLETING
JOB_TERMINAL
EVIDENCE_COLLECTION_COMPLETED
DIAGNOSIS_COMPLETED
```

`squeue` 不高频轮询，只在以下时机保存：

- 首次进入 `PENDING`。
- Pending Reason 变化。
- 进入 `RUNNING`。
- 用户主动请求诊断。
- 作业长时间未启动。

## 9. 诊断规则扩展

诊断规则从纯文本匹配升级为多证据判断，条件可引用：

- Slurm job state。
- Pending reason。
- stderr 正则。
- `scontrol` 字段。
- resource request。
- GPU probe。
- Python environment。
- output file status。

必须新增规则：

| Rule ID | 主要信号 |
| --- | --- |
| `SLURM.QOS_WALLTIME_LIMIT` | `QOSMaxWallDurationPerJobLimit` 或请求时间超过 QOS / partition 限制 |
| `SLURM.QOS_CPU_LIMIT` | `QOSMaxCpuPerUserLimit` 或 CPU 请求超过限制 |
| `SLURM.PENDING_RESOURCE_OR_QOS` | Pending reason 为资源、QOS、优先级或 association 限制 |
| `RUNTIME.NVIDIA_SMI_NO_GPU` | 未请求 GPU 却执行 `nvidia-smi`，或作业未分配设备 |
| `RUNTIME.NVML_DRIVER_MISMATCH` | `Failed to initialize NVML`、driver/library mismatch |
| `RUNTIME.LOG_PATH_MISSING` | 自定义 `#SBATCH -o/-e` 父目录不存在 |
| `RUNTIME.CONDA_NOT_INITIALIZED` | `conda: command not found`、`CondaError`、未加载初始化脚本 |
| `RUNTIME.PYTORCH_CPU_ONLY` | PyTorch 存在但 CUDA 不可用，且作业请求了 GPU |

每个诊断结论必须引用具体证据，不能只输出错误标签。

## 10. Simulator 修复

本节的当前执行规范见：

```text
docs/phase-1/docker_simulator_real_behavior_rebuild_plan.md
```

旧版 `docs/phase-1/simulator_behavior_fidelity_rebuild_plan.md` 保留为设计历史；后续 Docker 模拟器重构以 v2 方案为准。

### 10.0 保真度优先级

模拟器的第一目标不是复刻真实平台的具体数值，而是复刻平台固有、Web 接入后也不能轻易改变的行为和权限语义。

优先保证真实的行为：

- 用户、account、association、QOS entitlement。
- partition/QOS 允许关系。
- 非法 partition/QOS 被 Slurm 拒绝。
- 合法 carrier profile 能通过 Slurm 接受。
- REST auth、API version、错误响应与 simulator 所在 Slurm 版本一致，并明确不可外推到真实 107。
- shared `/public` 与 node-local `/tmp` 的可见性差异。
- pending/running/completed/cancelled/failed 状态与 pending reason。
- Evidence 读取权限和路径授权。

具体 CPU、GPU、内存、walltime、节点数量和 GPU 型号数值只作为代表性 fixture。真实 Web 接入时这些值应来自当前平台页面、CLI/REST snapshot 或管理员说明；模拟器不应把某个历史数值伪装成长期事实。

因此，模拟器配置的验收标准是：

- 行为语义可信。
- 数值不与 107Pilot profile 自相矛盾。
- 数值来源和限制清楚标注。
- 无法真实模拟的 runtime 能力，例如 CUDA/NVML/真实 GPU，必须明确返回 unavailable 或 limitation。

### 10.1 单一事实源

新增：

```text
config/platform_profiles/simulator-real107-behavior.yaml
```

统一定义：

- 分区。
- QOS。
- 代表性的 CPU/GPU/内存/walltime fixture。
- shared roots。
- local roots。
- 默认值。
- simulator 无法实现的能力。

该 YAML 同时驱动：

- `CapabilityProfile` QOS/partition 构建。
- simulator `slurm.conf` 生成或校验。
- SlurmDBD QOS 初始化。
- 测试 fixture。
- 文档能力表。

### 10.2 补齐缺失脚本

实现：

```text
scripts/apply-sim-real107-profile.sh
```

要求：

- 等待 MariaDB、SlurmDBD、slurmctld 就绪。
- 幂等创建 cluster、account、user、QOS。
- 根据统一 YAML 写入代表性限制，重点验证权限和 QOS/association 行为。
- 多次执行不会创建重复记录。
- 执行后打印简短校验表。
- 非零退出表示配置未成功应用。

### 10.3 修复 MaxTime 冲突

`Students` 分区 `MaxTime` 不必精确等于真实平台，但不能比 107Pilot profile 中暴露的代表性 QOS 低到产生自相矛盾的行为。

不能继续出现：

```text
107Pilot preflight: allowed
Slurm simulator: rejected by partition MaxTime=1h
```

### 10.4 保真度分级

Scheduler Fidelity：

- partitions
- QOS
- CPU
- memory
- walltime
- node state
- SlurmDBD
- pending reason
- accounting

Runtime Fidelity：

- 无真实 A100/RTX5090。
- 无真实 CUDA driver。
- 无真实 NVML。
- 无真实高速网络。
- 无真实 GPU cgroup binding。

无宿主 GPU 的默认模拟器中，GPU probe 返回“runtime unavailable”是预期结果，不能伪装成真实 GPU 可用。

## 11. API 与 UI

建议新增或确认 API：

```text
GET  /api/v1/platform/snapshot
POST /api/v1/platform/snapshot/refresh
GET  /api/v1/platform/capabilities
GET  /api/v1/runs/{run_id}/evidence
GET  /api/v1/runs/{run_id}/timeline
GET  /api/v1/runs/{run_id}/diagnosis
```

`refresh` 只能调用内置只读采集器，不能接收任意命令。

平台能力页展示：

- 当前推荐 partition/QOS。
- 官方文档默认值。
- 比赛 carrier 默认值。
- 实时分区状态。
- QOS 限制。
- 节点状态摘要。
- GPU 类型。
- shared/local roots。
- 数据来源。
- 采集时间。
- 过期状态。
- 冲突与限制。

来源标签：

```text
LIVE CLI
LIVE REST
OFFICIAL DOCS
SIMULATOR
UNKNOWN
```

运行证据页展示：

1. 提交请求。
2. 预检结果。
3. 状态时间线。
4. Slurm 证据。
5. Python/Conda 环境。
6. GPU 环境。
7. stdout/stderr。
8. 输出文件。
9. 诊断结论。
10. 推荐操作。

## 12. 实施顺序

### Phase 1: 事实模型与平台快照

交付：

- `ObservedValue`
- `PlatformSnapshot`
- CLI 白名单采集器
- `scontrol` / `sinfo` / `squeue` parser
- 快照目录与脱敏报告
- CapabilityProfile 合并与冲突记录

退出条件：

- 真实平台和模拟器能生成结构一致的快照。
- `MaxTime`、`TRES`、`AllowAccounts` 有值或明确标记不可用。
- 无字段以空字符串冒充未知。

### Phase 2: Runtime Evidence

交付：

- `resource-plan.json`
- basic runtime probe
- GPU probe
- `squeue` timeline
- events timeline
- evidence refs

退出条件：

- 任意作业均能回答 Python 路径、版本、工作目录。
- GPU 作业有 GPU 证据或明确跳过警告。
- Pending 作业能保存等待原因。

### Phase 3: Simulator Consistency

详细设计见：

```text
docs/phase-1/docker_simulator_real_behavior_rebuild_plan.md
```

交付：

- 单一 YAML profile。
- QOS 初始化脚本。
- partition/QOS 行为不自相矛盾。
- SlurmDBD account/user/QOS/association 行为初始化。
- Slurm 25.11 real107-matched simulator image candidate。
- 版本/API/auth/OpenAPI 差异报告。
- README 修复。
- scheduler/runtime fidelity 声明。

退出条件：

- 107Pilot 预检和 Slurm simulator 不再对同一请求给出相反结果。
- 绕过 107Pilot 的非法请求仍会被 Slurm simulator 拒绝。
- 代表性数值可变，不作为真实平台承诺；行为和权限语义必须稳定。
- 默认模拟器要么已经切到 25.11 target image，要么存在明确 blocker 文档说明为什么暂时只能使用 23.11 fallback。
- 不能在 23.11 fallback 上宣称与真实 107 的 REST/API/auth 行为完全一致。

### Phase 4: Diagnosis And UI

交付：

- 新诊断规则。
- 多证据诊断。
- 平台来源与新鲜度展示。
- 证据时间线。
- 冲突警告。

退出条件：

- 官方 FAQ 中的重点错误均有回归样例。
- 每个诊断结论至少引用一项具体证据。

## 13. 测试方案

### 13.1 单元测试

- `scontrol show part` 多版本输出解析。
- `sinfo` 节点状态归一化。
- `squeue` pending reason 解析。
- Slurm 时间格式解析。
- TRES/GRES 解析。
- QOS 权限不足返回 `permission_denied`。
- 用户名、HOME、节点名、token 脱敏。

### 13.2 采集器安全测试

- 不使用 shell。
- 不接受用户自定义命令。
- 命令超时有效。
- 输出大小限制有效。
- token 不进入文件。
- 环境变量采用允许列表。
- 单一命令失败不会破坏整个快照。

### 13.3 模拟器集成测试

- 合法 CPU 作业正常提交。
- 超 QOS CPU 作业被拒绝。
- 超 QOS GPU 作业被拒绝。
- 超 walltime 作业被拒绝。
- 不允许的 partition/QOS 组合被拒绝。
- Pending reason 进入时间线。
- stdout/stderr 和 `scontrol` 被收集。
- 无真实 GPU 模拟器中 GPU 作业生成明确限制警告。
- QOS 初始化脚本重复执行仍保持幂等。
- README 引用脚本全部存在。

### 13.4 诊断回归测试

为每个新增规则建立 fixture：

```text
tests/fixtures/diagnosis/
├── qos-walltime-limit/
├── qos-cpu-limit/
├── pending-resource/
├── nvml-mismatch/
├── no-gpu-requested/
├── missing-log-directory/
├── conda-not-initialized/
└── pytorch-cpu-only/
```

### 13.5 真实平台只读验收

只执行只读采集：

- 不提交长作业。
- 不修改 QOS。
- 不保存 token。
- 允许权限不足。
- 保留脱敏后的命令状态。
- 生成 CLI 与 REST 差异报告。

若具备临时 SSH 测试权限，额外要求：

- SSH 只作为测试输入源，不写入产品运行依赖。
- 每个 SSH 来源事实均标注 `real107-ssh-test-only`。
- 快照设置短 TTL，并在过期后显示 stale。
- SSH 失效时，API、Web、提交和诊断仍可通过 REST/docs/simulator profile 工作。
- SSH 采集脚本只执行固定白名单只读命令。
- 采集报告明确记录哪些命令因权限不足失败。

## 14. 最终验收矩阵

| 审计缺口 | 验收标准 |
| --- | --- |
| 无 CLI 平台快照 | 存在 CLI 快照、脱敏 raw 输出和解析结果 |
| 缺少 `MaxTime/TRES/AllowAccounts` | 有值或显式不可用状态 |
| 无 `squeue` 证据 | Pending/Running 状态进入 timeline |
| 环境摘要过薄 | 包含工作目录、用户、时间、Python 路径和版本 |
| GPU 作业无运行时信息 | 有 `nvidia-smi` 结果或明确警告 |
| 默认值有歧义 | 文档默认、carrier 默认、用户默认分别展示 |
| 模拟器 QOS/association 行为不真实 | user/account/QOS/association 由初始化脚本建立，并有 smoke 测试 |
| README 引用不存在脚本 | 脚本存在且有集成测试 |
| partition MaxTime 与 profile 自相矛盾 | 不再出现 profile 允许但 simulator 因过低 partition MaxTime 拒绝 |
| FAQ 错误规则不完整 | 所列规则均有 fixture 和诊断输出 |
| 无“已尝试步骤”记录 | 存在结构化事件时间线 |
| 平台事实无来源 | 每个关键值均带来源和采集时间 |
| 临时 SSH 权限被误认为长期能力 | SSH 来源事实带 test-only source、短 TTL，且系统无 SSH 仍可运行 |

## 15. 暂不扩大范围

以下内容不阻塞本轮官方覆盖补完：

- 完整 Web Terminal。
- Web UI 内执行 `nano`、`vim`。
- 真实 GPU Docker 仿真。
- InfiniBand 或多节点通信仿真。
- Prometheus/Grafana 运维系统。
- 自动安装 Conda 和 Python 包。
- 文件上传、压缩和传输平台。
- 自动修改用户 sbatch 中所有潜在错误。
