# Real 107 ConfigurationSnapshot Probe

> 状态：executed once, partial result ingested  
> 定位：M1-R 非阻塞兼容探测，不是比赛 M1 运行依赖。

## 目标

生成真实 107 的只读兼容证据：

```text
configuration_snapshot.json
probe_report.json
```

用于确认：

- Slurm REST 可达性；
- API version；
- Bearer JWT auth；
- partitions；
- nodes 摘要；
- 当前 token 可见 jobs；
- OpenAPI digest；
- 用户 home/allowed root 推断。

## 包内容

```text
scripts/real107_probe/
├── probe_real107_snapshot.py
├── real107_configuration_snapshot_probe.sbatch
└── README.md
```

生成上传包：

```bash
bash scripts/package-real107-probe.sh
```

输出：

```text
artifacts/probes/pilot107-real107-probe-<timestamp>.tar.gz
artifacts/probes/pilot107-real107-probe-<timestamp>.tar.gz.sha256
```

## 在真实 107 上运行

```bash
tar -xzf pilot107-real107-probe-<timestamp>.tar.gz
cd pilot107-real107-probe-<timestamp>
sbatch real107_configuration_snapshot_probe.sbatch
```

当前模板按已观察到的 107 真实作业规范设置：

```text
#SBATCH -p Students
#SBATCH --qos=qos_stu_medium_2gpu
#SBATCH --gres=gpu:A100:1
```

说明：

- probe 本身不使用 GPU；
- 这些资源指令只是为了满足真实 107 当前 Slurm association/QoS 提交策略；
- 如果校方提供 CPU-only QoS，应替换为更轻量的 carrier profile；
- Docker simulator 当前没有完整模拟真实 107 的 association/QoS 必填策略，这是 M1-R 兼容探测发现的偏差。

默认在 Slurm job 内执行：

```bash
scontrol token lifespan=600
```

并使用短期 token 访问：

```text
http://107.ustc.edu.cn:6820/slurm/v0.0.41/...
```

## 安全边界

- 只调用 HTTP GET；
- 不调用 REST submit；
- 不调用 scancel；
- 不读取用户项目文件；
- 不保存 JWT；
- 不保存 Authorization header；
- 不写 DB；
- 不进入 Evidence/Capsule；
- 输出 raw response 的摘要和 redacted 字段，不输出 token、cookie、JWT。

## 结果解释

`configuration_snapshot.json` 是稳定结构化事实：

```text
SourceAuthority = real_cluster_probe
AuthStrategy = single_user_jwt_bearer
EndpointSet.slurm_rest_url = http://107.ustc.edu.cn:6820
```

`probe_report.json` 是诊断报告：

```text
status = ok | partial | auth_required | failed
```

任何失败都不阻塞 Docker 主线和比赛 M1 部署。

## 已回收结果：Job 21039

用户已在真实 107 上运行修正版 carrier job，并回收结果：

```text
artifacts/probes/real107-probe-output.zip
artifacts/probes/real107/real107-probe-output/21039/configuration_snapshot.json
artifacts/probes/real107/real107-probe-output/21039/probe_report.json
```

结果摘要：

```text
summary.status = partial
observed_at = 2026-07-12T00:48:34.146312+00:00
target = http://107.ustc.edu.cn:6820
api_version = v0.0.41
auth_strategy = single_user_jwt_bearer
token_source = command
openapi = 3.0.3
slurm_release = 25.11.2
openapi_digest = 374c1eefce2239ceafe624a45305ffe9b722ea63ef7927903e23c7e87ede9541
```

Probe 结果：

| Probe | HTTP | 状态 | 说明 |
|---|---:|---|---|
| `ping` | 200 | ok | 返回 `tradmin-01` primary UP、`tradmin-02` backup DOWN |
| `partitions` | 500 | failed | 错误为 `Slurmdb query failed / Connection refused`，但 payload 仍包含 7 个分区摘要 |
| `nodes` | 200 | ok | 返回 19 个节点记录，包含 compute nodes 和管理/登录节点记录 |
| `jobs` | 200 | ok | 返回当前可见 jobs，含本次 `pilot107-probe` job 21039 |
| `openapi` | 200 | ok | 返回 OpenAPI v3 文档摘要 |

重要解释：

- `configuration_snapshot.json` 中 `cluster.qos=[]` 不表示真实平台没有 QoS；本次 probe 只从 REST 摘要中获得 partition allow_qos，未获得完整 `sacctmgr list qos` 等 accounting 结果；
- `/partitions` 的 HTTP 500 来自 SlurmDBD/TRES 查询失败，但分区主体数据仍然可读，后续 adapter 必须支持“partial payload with errors/warnings”；
- `Students` 分区允许多个学生 QoS，包括 `qos_stu_medium_2gpu`，这解释了旧版 carrier job 因 QoS 不匹配被拒绝；
- 本 probe 没有执行 submit/cancel/file read，因此不能证明真实平台 M2 多用户集成已经可用。

已观测分区：

```text
CPU-6530       anode[01-15]  AllowQos=qos_cpu-6530
CPU-8358P      anode[16-17]  AllowQos=qos_cpu-8358p
GPU-RTX5090    anode[01-15]  AllowQos=qos_gpu-rtx5090
GPU-A100       anode[16-17]  AllowQos=qos_gpu-a100
P107-RTX5090   anode[01-15]  AllowQos=qos_p107-rtx5090
P107-A100      anode[16-17]  AllowQos=qos_p107-a100
Students       anode[05-17]  AllowQos=qos_stu001,qos_stu_default,qos_stu_small,qos_stu_medium,qos_stu_medium_2gpu,qos_stu_long,qos_stu_cpu_long
```

对 Docker simulator 的反向要求：

- 增加真实 107 风格的 mandatory partition/QoS/association 测试 fixture；
- 增加 `Students/qos_stu_medium_2gpu/gpu:A100:1` carrier profile；
- 增加 `MIXED`、`DOWN,NOT_RESPONDING` 和 `PENDING` 队列场景；
- 增加 REST partial response fixture：HTTP 500 + `errors`/`warnings` + 可用 `partitions` payload。
