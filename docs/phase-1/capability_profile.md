# CapabilityProfile Ingest

> 状态：first implementation  
> 日期：2026-07-12  
> Source Authority：docs-main + real107 read-only probe + static competition profile

## 1. 目的

`CapabilityProfile` 用于把“平台事实”从散落文档、probe 结果和模拟环境配置中抽象出来，供 API、Contract、Preflight、前端资源选择和诊断说明共用。

它不替代 Slurm，也不把一次 probe 结果当成永久真相。它的定位是：

```text
docs-main 平台文档
+ real107 只读 probe
+ competition Docker profile
→ CapabilityProfile
→ API / Contract / Preflight / UI diagnostics
```

## 2. 已纳入的平台特征

### docs-main 平台文档

来自 `/home/knowingthesea/文档/docs-main` 的核心事实：

- 平台页面、当前授权和实时命令输出是资源事实的最终依据；
- 学生常用作业流使用 `Students` 分区；
- docs-main 示例默认 QoS 为 `qos_stu_default`；
- 资源、节点、GPU 型号、分区、QoS 和额度会随授权变化；
- `/public` 是共享存储语义，`/tmp`、`/usr`、`/var`、`/opt` 是节点本地路径语义；
- FAQ 中的 Slurm 错误需要保留为结构化诊断依据，例如 QoS walltime、CPU 限额、partition/QoS 不匹配和 pending reason。

已内置的学生 QoS 上限：

| QoS | CPU | GPU | Memory | Walltime |
|---|---:|---:|---:|---:|
| `qos_stu_default` | 4 | 1 | 16G | 4h |
| `qos_stu_small` | 8 | 1 | 32G | 8h |
| `qos_stu_medium` | 16 | 1 | 64G | 24h |
| `qos_stu_medium_2gpu` | 24 | 2 | 128G | 24h |
| `qos_stu_large` | 48 | 4 | 240G | 12h |
| `qos_stu_long` | 16 | 1 | 64G | 72h |
| `qos_stu_cpu_long` | 32 | 0 | 128G | 72h |

`qos_stu001` 已作为真实平台 observed QoS 名称保留，但没有完整的可用内存限额。

### 2026-07-26 SSH 环境清单

只读 SSH 清单补充了当前平台事实：`/public` 与 `/home` 都是共享挂载，但用户家目录
位于 `/home/scc/<user>`，不是 `/public/home/<user>` 的同路径别名；用户家目录权限为
`0700`。计算节点的代表性资源是 128 CPU、8 GPU（RTX5090 节点 500G 内存，A100 节点
1000G 内存）。`Students` 额外允许 `qos_stu_large`。`qos_stu_medium` 与
`qos_stu_medium_2gpu` 的当前 walltime 为 24 小时。

证据：

```text
artifacts/probes/real107-environment-ssh-20260726T061226Z/inventory/
```

这些是时点观测，仍须以当前账户 association 和平台页面为准。

分配到 `Students/qos_stu_medium_2gpu` 的 1-GPU probe 已在 `anode16` 完成。它观测到
`NVIDIA A100-SXM4-80GB`、驱动 `580.159.03`、80 GiB 显存，账务记录为 1 CPU、1 GPU、
4 GiB。系统 Python 3.12 未预装 PyTorch；因此 GPU Contract 不应把系统 Python 中的
`torch` 可用性当作平台 CUDA 可用性的前提。

```text
artifacts/probes/real107-compute-ssh-20260726T063524Z/
```

### real107 read-only probe

已支持从真实 probe 输出目录加载：

```text
artifacts/probes/real107/real107-probe-output/<job_id>/
├── configuration_snapshot.json
└── probe_report.json
```

当前已验证目录：

```text
artifacts/probes/real107/real107-probe-output/21039
```

该 profile 保留：

- REST endpoint；
- Slurm REST API version；
- OpenAPI digest；
- 默认分区和默认 QoS；
- 真实分区列表；
- AllowQos；
- HTTP 非 2xx 但 payload 可用的 partial response 语义。

注意：本次真实 probe 显示账户默认分区为 `CPU-6530`、默认 QoS 为空；这不能外推为比赛模拟的默认提交 profile。比赛演示主线仍使用 `Students/qos_stu_medium_2gpu/gpu:A100:1` 作为 carrier profile。

### competition Docker profile

默认 API service 使用 `docker-real107-sim`：

```text
profile_id = docker-real107-sim
default_partition = Students
default_qos = qos_stu_medium_2gpu
rest.api_version = v0.0.41
rest.partial_payload_with_errors = true
```

该 profile 是比赛可控模拟，不声称等同真实 107：

- 默认 Docker simulator 使用 source-built Slurm `25.11.2` target image；
- REST API 使用 `v0.0.41`，JWT 探针已在 simulator 上验证；
- fake GPU GRES 可用于调度行为验证；
- 运行时 GPU 真实性仍缺失：没有真实 CUDA/NVML/A100/RTX5090 设备或 GPU cgroup 绑定。
- 真实分配作业的 A100/CUDA 事实由单独的短时 compute probe 记录，不能由 Docker fake
  GRES 外推。

## 3. API 与配置

新增 API：

```text
GET /api/v1/platform/capabilities
```

返回字段包括：

```text
profile_id
source_authority
captured_at
freshness_seconds
shared_roots
local_roots
default_partition
default_qos
partitions[]
qos[]
rest
dynamic_facts[]
limitations[]
```

服务配置：

```text
PILOT107_CAPABILITY_PROFILE_PATH=/path/to/probe-output-or-profile.json
```

该值可以是 real107 probe 输出目录、合并 JSON，或
`pilot107.simulator_real107_behavior.v1` YAML。Docker Compose 默认显式指向
镜像内的 `config/platform_profiles/simulator-real107-behavior.yaml`；未设置时
才使用同一 YAML 的 Docker profile fallback。

### 3.1 工作目录不是 CapabilityProfile 的一部分

`PILOT107_ALLOWED_ROOTS` 是部署授权边界，不能从平台共享挂载推导，也不能把
测试用户的私有目录当作通用默认值。服务进程在未配置时使用空列表并拒绝文件
系统提交（fail closed）。

部署可使用 `{user}` 表示已认证 Slurm 用户的私有工作区，例如：

```text
PILOT107_ALLOWED_ROOTS=/public/home/{user}
```

该 token 只会对通过用户名校验的作业 owner 展开，并同时在 API/Worker 的
preflight、命令提交后端、evidence collector 和 competition command gateway 生效。因此 Bob 不能以
自己的身份提交到 Alice 的目录。若平台使用 `/home/scc/<user>`，应显式配置
`/home/scc/{user}`；不得沿用 Docker fixture 的 `/public/home/alice`。

## 4. 当前边界

- `CapabilityProfile` 已进入 API/Worker service builder、HTTP API 和直接提交 preflight；
- Contract service 的 `capability`（以及兼容名称 `real107-sim`/`cpu-only`）profile 会使用 `CapabilityProfile.partition_qos()`；
- recipe 版本不再把分区/QoS 作为不可变内容写死；catalog 仅在内存中叠加当前 profile，且兼容旧版已经持久化的 capability overlay；
- Contract validate 和 direct Run prepare 已使用 `CapabilityProfile.qos_limits()` 校验 CPU/GPU/memory/walltime 上限；
- Web 资源选择和 Diagnostics 面板已消费 `/api/v1/platform/capabilities`；
- 真实 107 submit/cancel/file read 仍未探测；
- OpenAPI digest 已可从真实 probe 加载，但尚未做定时刷新任务；
- 前端仍未消费真实 Diagnosis API；当前 Diagnostics 面板展示的是平台能力画像、动态事实和限制。

## 5. 下一步

```text
Diagnosis Store + Rule Engine
→ Evidence Context Builder
→ Agent explain API
→ 后续 M1-R probe 刷新生成新的 profile artifact
```
