# Docker Simulator Real Behavior Rebuild Plan

> 状态：v2 integrated plan  
> 日期：2026-07-15  
> 目标：以真实平台行为、权限语义和 Slurm 版本/API 语义为第一优先级，重构 107Pilot Docker Slurm simulator。  
> 上游依据：官方 `docs-main`、`算力平台及算力赛道介绍.pdf`、`training-107-competition.pdf`、`demo (2).pdf`、真实平台特征补充说明、错误 playbook、现有 107Pilot simulator。

## 1. 设计判断

Docker simulator 的首要任务不是保存某次采集到的 CPU/GPU/内存/walltime 数值，而是复现真实 107 平台中用户和 107Pilot 必须面对的稳定行为：

- 用户通过 SCOW/Web Shell、SSH 或 REST 以本人身份访问平台。
- 登录节点用于整理文件、轻量环境准备和提交作业；重计算必须进入 Slurm 作业。
- 计算节点由 Slurm 分配，作业内才有完整的 `SLURM_*` 运行时变量。
- 用户只能使用账号被授权的 account、partition、QOS 和资源组合。
- 超出 QOS、partition、TRES、association 或权限范围的请求必须由 Slurm 拒绝或进入带 Reason 的 PENDING。
- 用户只能看到和操作自己权限内的作业；Token 权限等同本人账号权限。
- stdout、stderr、`squeue`、`scontrol show job`、`sacct` 和 REST JSON 是排错证据链。
- `/public`、用户 home 是共享路径；`/tmp` 是节点本地路径，不能当跨节点持久共享目录。
- 登录节点无 GPU；GPU 只有在已申请 GPU 的计算环境中才可验证。
- Slurm 版本会影响 REST 路径、认证头、OpenAPI schema、错误响应、TRES/QOS 行为和调度细节，因此版本对齐是行为保真的一部分。

因此，模拟器不应把某个历史资源数值伪装成长期真实参数；但它必须让合法请求、非法请求、权限不足、Token 过期、日志路径缺失、GPU runtime 不可验证等行为与官方工作流一致。

## 2. 官方材料重新归纳的行为事实

### 2.1 用户主路径

官方文档和培训材料共同描述的主路径是：

1. 登录平台或 Web Shell。
2. 在共享目录准备代码、数据、脚本和日志目录。
3. 用 `sinfo`、`scontrol show part`、`sacctmgr show qos` 或页面确认资源选项。
4. 用 `sbatch` 提交批处理作业，或用 `srun --pty` 做短时交互调试。
5. 用 `squeue -u "$USER"` 查看排队/运行状态。
6. 用 `scontrol show job <job_id>` 查看 `State`、`Reason`、`ReqTRES`、`AllocTRES`、`QOS`、`NodeList`、`Command`。
7. 用 stdout/stderr 和输出文件确认结果。
8. 用 `scancel <job_id>` 取消自己的作业。

模拟器必须把这条路径作为 P0，不以 Web 侧 mock 代替 Slurm 行为。

### 2.2 资源与权限

需要复现的不是精确数值，而是权限关系：

- partition 限定节点集合、AllowAccounts、AllowQos、MaxTime、TRES。
- QOS 限定 CPU、GPU、内存、walltime、作业并发或 group/user TRES。
- account/user association 决定某用户能否使用某个 QOS。
- 页面看得到或文档列出的 QOS，不代表当前账号必然有权提交。
- `sbatch` 命令行参数会覆盖脚本中的 `#SBATCH` 同名参数。
- root 提交通常不应作为普通用户路径；真实用户路径应以非 root 用户提交。

模拟器验收时应优先测试“未授权被拒绝”和“授权后可运行”，而不是只测试 happy path。

### 2.3 REST 与 Token

官方培训材料描述 REST API：

- `slurmrestd` 提供 HTTP JSON API。
- 用户用 `scontrol token lifespan=...` 生成 JWT。
- Token 表示本人身份和本人权限，过期后应返回认证失败。
- 常用端点包括 `/slurm/v0.0.41/jobs`、`/slurm/v0.0.41/nodes`、`/slurm/v0.0.41/job/{id}`、`/slurm/v0.0.41/jobs/submit`、`/slurm/v0.0.41/diag`。
- demo 材料中 Docker 教学路径使用过 `/slurm/v0.0.44` 和 header 形式 `X-SLURM-USER-NAME`、`X-SLURM-USER-TOKEN`，但真实平台培训主线强调 `Authorization: Bearer <token>`。

模拟器必须明确暴露自身 Slurm 版本对应的 REST API 行为，不能用 wrapper 伪装不同版本。107Pilot 可以做兼容适配，但 simulator fidelity report 必须说清楚当前支持哪个 REST 版本、认证头和 OpenAPI digest。

### 2.4 平台拓扑

官方材料和补充说明给出的抽象拓扑：

- 登录节点：例如 `tradmin-02`，安装 Slurm client，不运行用户重计算。
- 管理/控制节点：`slurmctld`、`slurmdbd`、`slurmrestd`、MariaDB。
- 计算节点：`anode[01-17]` 一类节点，由 `slurmd` 执行作业。
- 共享存储：`/public`、`/public/home/<user>`、`/public/app`。
- 节点本地路径：`/tmp` 等，每个计算节点独立。
- GPU 节点类型：RTX5090、A100 等应作为 scheduler GRES/TRES 行为出现；默认 Docker 不需要伪装真实 CUDA/NVML。

Docker 中可以缩小节点数量，但要保留这些角色边界和路径语义。

### 2.5 典型排错信号

必须能在模拟器中稳定制造并取证：

- `QOSMaxWallDurationPerJobLimit`。
- `QOSMaxCpuPerUserLimit` 或等价 CPU/TRES 限制。
- account/QOS/partition 不允许。
- `Resources`、`Priority`、`Dependency`、`PartitionConfig` 等 PENDING Reason。
- 自定义 `#SBATCH -o/-e` 父目录不存在。
- 作业内 `SLURM_SUBMIT_DIR`、`SLURM_JOB_NODELIST` 等变量存在；登录节点直接执行时为空或无意义。
- `nvidia-smi` 在登录节点不可用，或在无宿主 GPU 的默认模拟器中返回 runtime unavailable。
- `Failed to initialize NVML` 一类 GPU runtime 错误可用 fixture 表示，但不应声称默认模拟器有真实 GPU。
- `sacct` 能提供完成、失败、取消等历史状态。

## 3. 模拟器目标态

### 3.1 P0：必须真实的行为

- Slurm 版本/API/认证语义可追溯，最终目标为 25.11 系列。
- 非 root 普通用户提交、查询、取消作业。
- 用户只能取消自己的作业。
- 未授权 partition/QOS/account 组合被 Slurm 拒绝。
- 超 QOS CPU/GPU/walltime/TRES 请求被 Slurm 拒绝或给出可解释 Reason。
- 合法 CPU 作业可完成并产生 stdout/stderr。
- 合法 GPU scheduler 作业能分配 fake GRES/TRES；默认 runtime 明确标记无真实 GPU。
- `squeue`、`scontrol show job`、`sacct`、REST jobs/nodes/job/cancel 路径可用。
- `ReqTRES`、`AllocTRES`、`QOS`、`Reason`、`NodeList` 可被 107Pilot 证据模块采集。
- `/public` 和用户 home 对登录/计算角色共享；`/tmp` 模拟节点本地。
- 作业脚本中的 `SLURM_*` 变量符合 Slurm 行为。
- 所有 Token、用户名、home 路径和节点标识在导出报告中可脱敏。

### 3.2 P1：应尽快补齐

- REST submit 与 CLI `sbatch` 的工作目录/权限行为一致。
- array job 并发限制和 QOS 限制可制造。
- hold/release/requeue/suspend/resume 至少有 CLI 行为测试；Web 产品可只生成命令模板。
- `srun --pty` 可在 Docker 内验证最小交互式资源分配，但 Web 后端不直接打开任意终端。
- `module avail`、`/public/app`、Conda 初始化错误用轻量 fixture 表达。
- GPU runtime fixture 支持 `nvidia-smi` success、command_not_found、no_device、NVML mismatch 三类状态。

### 3.3 不作为默认目标

- 真实 A100/RTX5090 CUDA/NVML 仿真。
- InfiniBand、高速网络、IPMI、真实 Grafana/Prometheus 运维面完整复刻。
- Web Terminal。
- 任意 shell 命令执行。
- 自动安装 Conda/Python 包。
- 自动修复或删除用户文件。

## 4. 镜像重构策略

### 4.1 版本目标

当前模拟器使用 Ubuntu 包里的 Slurm 23.11 只能作为 fallback。目标镜像必须新增：

```text
pilot107/slurm-sim:25.11-real107
```

目标要求：

- `scontrol --version` 显示 `25.11.2`，优先精确对齐已观测真实平台版本。
- `slurmrestd` 暴露真实 25.11 对应 OpenAPI schema。
- JWT plugin、Munge、slurmdbd、slurmctld、slurmd、slurmrestd 在同一版本族内构建。
- REST 端点、认证头、错误响应不通过 107Pilot wrapper 伪造。
- 构建产物写入 version manifest：Slurm version、build source、OpenAPI digest、JWT auth mode、MariaDB version、base image digest。

23.11 fallback 可继续跑现有测试，但最终不能用它宣称“与真实 107 REST/API 行为一致”。

### 4.2 构建路线

优先级：

1. 从 SchedMD Slurm 25.11.x 源码构建 Debian/Ubuntu 包或直接安装到镜像。
2. 若源码构建受依赖阻塞，评估 giovtorres/slurm-docker-cluster 25.11.4 作为基础镜像，并检查其 license、auth_jwt、slurmrestd、slurmdbd 和 compose 行为。
3. 若学校提供兼容基础镜像，导入后仍需运行同一套 behavior matrix。

镜像层建议拆分为逻辑职责，即使最初仍在一个 Dockerfile：

- `slurm-base`：Slurm binaries、Munge、JWT、users/groups。
- `slurm-controller`：slurmctld、slurmdbd、slurmrestd、MariaDB client/init。
- `slurm-worker`：slurmd、fake GRES files、runtime probe tools。
- `slurm-login`：Slurm client、Python、只读采集工具，不运行 slurmd。
- `profile-init`：account/user/QOS/association 初始化脚本。

## 5. 单一事实源

新增 profile：

```text
config/platform_profiles/simulator-real107-behavior.yaml
```

它驱动：

- `slurm.conf`。
- `gres.conf`。
- `apply-sim-real107-profile.sh`。
- 107Pilot `CapabilityProfile` simulator fixture。
- smoke/integration tests。
- README 中的行为矩阵。
- simulator fidelity report。

示意结构：

```yaml
schema: pilot107.simulator_real107_behavior.v1
slurm:
  target_version: "25.11.x"
  fallback_version: "23.11.x"
  select_type: cons_tres
  accounting: slurmdbd

users:
  - name: pilot
    account: students
    qos: [qos_stu_default, qos_stu_medium_2gpu]
  - name: limited
    account: students
    qos: [qos_stu_default]

partitions:
  - name: Students
    allow_accounts: [students]
    allow_qos: [qos_stu_default, qos_stu_medium_2gpu, qos_stu_long]
    nodes: [anode05, anode06]
    default: true
  - name: P107-RTX5090
    allow_accounts: [competition]
    allow_qos: [qos_p107_default]
    nodes: [anode01, anode02]

qos:
  - name: qos_stu_default
    policy_class: default-small
    limits:
      cpu_per_job: representative
      gpu_per_job: representative
      walltime: representative
  - name: qos_stu_medium_2gpu
    policy_class: student-gpu
    limits:
      gpu_per_job: representative

paths:
  shared_roots: [/public, /public/home]
  app_root: /public/app
  node_local_roots: [/tmp]

fidelity:
  scheduler: [partition, qos, association, tres, pending_reason, accounting]
  runtime_limitations: [no_real_gpu_by_default, no_ib, no_ipmi]
```

`representative` 表示“测试用代表值”，不是产品宣称的真实上限。测试关注合法/非法边界行为，而非该值是否等于真实平台当天配置。

## 6. Compose 与角色设计

目标 compose 至少包含：

- `login`：普通用户入口，安装 Slurm client、Python、107Pilot probe；无 `slurmd`，无 GPU device。
- `slurmctld`：控制器。
- `slurmdbd`：记账。
- `mariadb`：SlurmDBD backing store。
- `slurmrestd`：REST API 网关。
- `worker-a100` / `worker-rtx5090`：运行 `slurmd`，注册 fake GPU GRES。

路径挂载：

- `/public`：所有 login/worker 共享。
- `/public/home/pilot`：普通用户 home 的共享表现。
- 每个 worker 自己的 `/tmp`：验证 node-local 行为。
- Slurm spool、state、log 使用 Docker volume，不映射到用户 project 目录。

用户设计：

- `pilot`：默认普通用户，代表 Web app 当前用户。
- `limited`：只具备默认 QOS，用于权限不足测试。
- `otheruser`：用于“不能取消别人作业/不能看到越权信息”的测试。
- `slurm`：守护进程用户。
- root 只用于容器初始化和管理员脚本，不用于产品 happy path。

## 7. SlurmDBD / QOS / Association 初始化

`scripts/apply-sim-real107-profile.sh` 需要升级为 profile-driven：

- 等待 MariaDB、slurmdbd、slurmctld ready。
- 幂等创建 cluster。
- 幂等创建 account。
- 幂等创建 users。
- 幂等创建/修改 QOS。
- 幂等绑定 user/account/qos association。
- 设置 representative QOS/TRES/walltime 限制。
- 校验 `sacctmgr show assoc`、`sacctmgr show qos`、`scontrol show part`。
- 输出简短 verification table。
- 任一 P0 行为配置失败时非零退出。

必须保留两类测试：

- 合法请求通过：例如 `pilot + Students + qos_stu_default`。
- 非法请求被拒绝：例如 `limited + qos_stu_medium_2gpu`、不允许的 partition/QOS、超 GPU/CPU/walltime。

## 8. REST 行为设计

模拟器需要同时验证 CLI 和 REST：

- `scontrol token lifespan=...` 能生成 Token。
- Token 过期或缺失时 REST 返回认证失败。
- Token 对应用户权限，不允许越权取消别人作业。
- `GET jobs`、`GET job/{id}`、`DELETE job/{id}`、`GET nodes`、`GET diag` 可用。
- `POST jobs/submit` 如果受工作目录/权限限制暂时不能作为默认提交路径，必须在 fidelity report 标出，不可静默改用 CLI。
- 107Pilot REST adapter 必须记录 simulator REST API version，避免把 fallback 行为外推到真实 25.11。

验收报告中应包含：

```json
{
  "rest": {
    "slurm_version": "25.11.x",
    "openapi_digest": "...",
    "api_paths": ["/slurm/v0.0.xx/jobs", "/slurm/v0.0.xx/nodes"],
    "auth_modes": ["Authorization: Bearer"],
    "known_differences": []
  }
}
```

## 9. 官方行为验收矩阵

| 类别 | 官方行为 | Docker 验收 |
| --- | --- | --- |
| 登录/计算边界 | 登录节点不跑重计算，计算节点执行作业 | `login` 无 `slurmd`；作业实际在 worker hostname 运行 |
| 分区查询 | `sinfo`、`scontrol show part` 可看分区 | 输出含 AllowAccounts/AllowQos/MaxTime/TRES 或显式 unavailable |
| 作业提交 | `sbatch` 提交，命令行参数覆盖脚本参数 | 覆盖行为有测试 |
| 作业状态 | `squeue -u $USER` 看 State/Reason | PENDING/RUNNING/COMPLETING/COMPLETED/FAILED/CANCELLED 可制造 |
| 作业详情 | `scontrol show job` 是排错首选 | 采集 ReqTRES/AllocTRES/QOS/NodeList/Command/Reason |
| 取消 | 只能取消自己的作业 | `pilot` 不能取消 `otheruser` 作业 |
| QOS 权限 | 未授权 QOS 不能提交 | association 测试稳定失败 |
| QOS 限制 | 超 CPU/GPU/walltime 被拒绝或 pending | 错误/Reason 进入 evidence |
| REST Token | Token 等于本人权限且会过期 | 缺失/过期/越权都有测试 |
| GPU | 登录节点无 GPU，作业申请后才检查 | 默认 simulator 标记 runtime unavailable，不伪造真实 GPU |
| 共享路径 | `/public`、home 可跨角色访问 | login 写入，worker 作业读取 |
| 本地路径 | `/tmp` 节点独立 | worker 间 `/tmp` 不共享 |
| 日志 | stdout/stderr 是首要证据 | 默认和自定义日志路径都测试 |
| 记账 | `sacct` 查历史 | 完成/失败/取消进入 accounting |

## 10. 测试计划

### 10.1 镜像与版本测试

- `scontrol --version`。
- `slurmctld -V`、`slurmd -V`、`slurmrestd -V`。
- OpenAPI schema 可获取并计算 digest。
- JWT plugin 存在且能生成 Token。
- `sinfo`、`squeue`、`sacctmgr`、`sacct` 基础命令可用。

### 10.2 权限测试

- `pilot` 合法 CPU 作业通过。
- `pilot` 合法 GPU scheduler 作业通过，runtime GPU limitation 明确。
- `limited` 使用未授权 QOS 被拒绝。
- `otheruser` 作业不能被 `pilot` 取消。
- 缺失 Token、过期 Token、错误用户名/Token 组合被 REST 拒绝。
- root 提交不作为产品测试 happy path。

### 10.3 调度测试

- 超 walltime。
- 超 CPU。
- 超 GPU。
- partition/QOS 不匹配。
- 分区无可用节点产生 PENDING Reason。
- array job 并发限制。
- `scancel` 后状态可在 REST 或 `sacct` 中观察。

### 10.4 证据测试

- `resource-plan.json`、`submitted-script.sbatch`、`sbatch-argv.json` 存在。
- `squeue` timeline 捕获 PENDING/RUNNING/terminal。
- `scontrol show job` 保存。
- stdout/stderr 保存。
- `sacct` 保存。
- runtime basic probe 保存 `pwd`、`whoami`、`hostname`、`python -V`、Python path、`SLURM_*` allowlist。
- GPU probe 在无真实 GPU 默认模拟器中保存 limitation。

### 10.5 官方文档回归测试

为官方 FAQ 和培训材料中的重点错误建立 fixture：

- `qos-walltime-limit`。
- `qos-cpu-limit`。
- `pending-resources`。
- `partition-config`。
- `missing-log-directory`。
- `no-gpu-on-login`。
- `gpu-runtime-unavailable`。
- `nvml-mismatch-fixture`。
- `token-expired`。
- `cannot-cancel-other-user-job`。
- `slurm-env-only-inside-job`。

## 11. 行为保真报告

每次 simulator build 后生成：

```text
simulator/reports/behavior-fidelity/<timestamp>.json
```

当前执行入口：

```text
scripts/report-sim-behavior-fidelity.sh
```

最小结构：

```json
{
  "schema": "pilot107.simulator_real_behavior_fidelity.v1",
  "generated_at": "2026-07-15T00:00:00Z",
  "source_docs": [
    "docs-main",
    "training-107-competition.pdf",
    "demo (2).pdf",
    "107Pilot_真实107算力平台特征补充说明_v1.0.md"
  ],
  "image": "pilot107/slurm-sim:25.11-real107",
  "slurm_version": "25.11.x",
  "real107_observed_version": "25.11.2",
  "rest_api": {
    "paths": [],
    "auth_modes": [],
    "openapi_digest": null
  },
  "scheduler_fidelity": {
    "partition": "pass",
    "qos": "pass",
    "association": "pass",
    "tres": "pass",
    "pending_reason": "pass",
    "accounting": "pass"
  },
  "runtime_fidelity": {
    "real_gpu": "not_supported_by_default",
    "ib": "not_supported",
    "node_local_tmp": "pass",
    "shared_public": "pass"
  },
  "known_differences": []
}
```

这份报告是 107Pilot UI 和文档展示 simulator 限制的依据。

## 12. 实施顺序

### S1：锁定行为 profile

- 新增 `config/platform_profiles/simulator-real107-behavior.yaml`。
- 写 profile schema 校验。
- 从 profile 生成或校验 `slurm.conf`、`gres.conf`、QOS 初始化脚本。
- 将旧的硬编码 QOS/partition fixture 改为引用 profile。

退出条件：README、CapabilityProfile、Slurm config 和测试不再各自维护一套互相漂移的事实。

### S2：权限与 QOS 初始化

- 重写 `apply-sim-real107-profile.sh` 为 profile-driven。
- 加入 `pilot`、`limited`、`otheruser`。
- 加入 association、QOS、partition 行为 smoke。
- 保留 23.11 fallback 路径，但报告中标记版本差异。

退出条件：合法/非法/越权请求都有稳定测试。

### S3：25.11 镜像候选

- 构建 `pilot107/slurm-sim:25.11-real107`。
- 修复 JWT、slurmrestd、slurmdbd、auth、TRES、fake GRES。
- 输出 image version manifest。
- 运行 S1/S2 行为测试。

退出条件：25.11 candidate 通过核心 CLI、REST、QOS、association、accounting 测试；若失败，必须有 blocker 文档说明具体依赖和失败点。

### S4：REST 与 evidence 对齐

- REST adapter 记录 API version/auth mode/OpenAPI digest。
- REST jobs/nodes/job/cancel 与 CLI 结果做差异报告。
- 107Pilot evidence 模块在 simulator 中保存 Slurm timeline、runtime probe、GPU limitation。

退出条件：同一作业可由 CLI、REST、evidence 三路互相校验。

### S5：默认切换与官方行为确认

- 默认 compose 切到 25.11 target。
- 23.11 fallback 只保留为兼容路径。
- 生成 behavior fidelity report。
- 更新 README，明确 scheduler fidelity 与 runtime limitation。

退出条件：官方文档列出的核心工作流全部有 Docker smoke 或 integration 测试；剩余差异全部写入报告。

## 13. 与 107Pilot 模块设计的衔接

该重构方案应反向驱动既有模块：

- `PlatformSnapshot`：采集 simulator 的 `scontrol show part`、`sinfo`、`squeue`、`sacctmgr show qos`，结构必须与真实平台只读快照一致。
- `CapabilityProfile`：读取 simulator profile，但关键值仍带 `source_type=simulator`，不能覆盖 live CLI/REST。
- `Preflight`：使用同一 profile 校验 partition/QOS/TRES/path；校验结果必须与 SlurmDBD 行为一致。
- `Evidence`：保存请求快照、runtime probe、GPU limitation、stdout/stderr、`scontrol`、`sacct`、timeline。
- `Diagnosis`：用 simulator fixture 回归 QOS、Pending Reason、GPU、日志路径、Conda/Python 等规则。
- `REST adapter`：不得假设真实平台和 simulator REST 版本一致，必须记录观测版本。

最终目标是：Docker simulator 不是“看起来有几个分区”的假环境，而是 107Pilot 的行为回归基准。它允许数值代表性，但不允许权限、版本、Token、QOS、作业生命周期和证据链失真。
