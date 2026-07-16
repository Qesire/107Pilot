# Real 107 Compatibility Report

> 状态：M1-R probe partial  
> 更新日期：2026-07-12  
> Source Authority：real_cluster_probe + docs-main + training material
> 当前定位：M1-R 非阻塞兼容探测，不阻塞 Docker 主线和比赛 M1 部署。

## 1. Summary

| 项 | 当前结论 | 状态 | 下一步 |
|---|---|---|---|
| 真实 107 REST 地址 | probe 确认 `http://107.ustc.edu.cn:6820` 可达 | M1-R observed | 后续确认应用节点到该地址的网络策略 |
| Slurm API 版本 | probe 确认 `v0.0.41`，OpenAPI `3.0.3`，Slurm `25.11.2` | M1-R observed | Docker/REST adapter 保持版本可配置 |
| 认证方式 | probe 使用 `single_user_jwt_bearer`，token 来源为 `scontrol token` | M1-R observed | 继续禁止 token 入日志、Capsule、浏览器 localStorage |
| 文件访问 | probe 确认用户 home `/public/home/pb23061276` 和 allowed root；应用节点能否挂载真实 `/public` 未确认 | M1-R partial | M2 前确认服务账号/ACL/文件 API |
| 长期 Worker | 真实平台是否允许长期 Worker 未确认 | M1-R optional | 不作为比赛依赖 |
| 校内 LLM | 未确认 | P2 optional | 确认 API 可用性 |
| SCOW 协同 | 已知 SCOW 提供通用入口，但 API/深链未确认 | P1 unknown | 获取官方能力 |
| 分区与节点 | probe 返回 7 个分区、19 个节点记录；`anode17`、`tradmin-01` 为 `DOWN,NOT_RESPONDING` | M1-R observed | Docker fixture 增加 DOWN/MIXED 状态 |
| SlurmDB/TRES | `/partitions` 返回 HTTP 500，错误为 `Slurmdb query failed / Connection refused`，但 payload 中仍包含分区摘要 | M1-R partial | 客户端和模拟环境覆盖 HTTP 非 2xx 但含可用 payload 的语义 |
| CapabilityProfile | 已可从真实 probe 输出加载 `real107-probe` profile，并默认提供 `docker-real107-sim` competition profile | first implementation | 前端资源选择和诊断面板尚未消费 |

新增已观测事实：

```text
2026-07-12:
  real_107_sbatch_without_explicit_qos:
    result: rejected
    error: "Invalid qos specification"
  known_valid_user_job_profile_from_reference_workflow:
    partition: Students
    qos: qos_stu_medium_2gpu
    gres: gpu:A100:1
  real_107_configuration_snapshot_probe:
    job_id: 21039
    result: partial
    observed_at: "2026-07-12T00:48:34.146312+00:00"
    target: "http://107.ustc.edu.cn:6820"
    api_version: "v0.0.41"
    openapi_digest: "374c1eefce2239ceafe624a45305ffe9b722ea63ef7927903e23c7e87ede9541"
    slurm_release: "25.11.2"
    failed_probe: partitions
    failed_probe_reason: "Slurmdb query failed / Connection refused"
```

结论：

- 真实 107 的作业提交策略需要显式匹配用户 association/QoS；
- Docker simulator 当前未完整模拟真实 107 的 mandatory QoS policy；
- 真实 REST 只读查询已经可用，但 `/partitions` 可能以 HTTP 500 返回带 errors/warnings 的部分 payload；
- `configuration_snapshot.json` 中 `cluster.qos=[]` 只能表示本次 probe 未获得完整 QoS 列表，不能解释为真实平台没有 QoS；
- 这不阻塞 Docker competition 主线，但属于 M1-R 兼容性偏差，后续资源预检和 Recipe profile 必须纳入 `Students/qos_stu_medium_2gpu` 等真实 profile。

## 1.1 Probe Package

已准备并完成一次真实 107 只读 `ConfigurationSnapshot` probe：

```text
scripts/real107_probe/probe_real107_snapshot.py
scripts/real107_probe/real107_configuration_snapshot_probe.sbatch
scripts/real107_probe/README.md
scripts/package-real107-probe.sh
docs/phase-1/real107_configuration_snapshot_probe.md
```

本地已生成可上传包：

```text
artifacts/probes/pilot107-real107-probe-20260712T004659Z.tar.gz
artifacts/probes/pilot107-real107-probe-20260712T004659Z.tar.gz.sha256
```

当前状态：

```text
package_ready = true
real_107_executed = true
result_zip = artifacts/probes/real107-probe-output.zip
extracted_result = artifacts/probes/real107/real107-probe-output/21039/
result_status = partial
```

可作为 API service profile 输入：

```text
PILOT107_CAPABILITY_PROFILE_PATH=artifacts/probes/real107/real107-probe-output/21039
```

已验证加载摘要：

```text
profile_id = real107-probe
default_partition = CPU-6530
default_qos = null
partitions = CPU-6530, CPU-8358P, GPU-RTX5090, GPU-A100, P107-RTX5090, P107-A100, Students
partial_payload_with_errors = true
```

本次结果：

| Probe | HTTP | 状态 | 结论 |
|---|---:|---|---|
| `ping` | 200 | ok | `tradmin-01` primary UP；`tradmin-02` backup DOWN；cluster `training`；Slurm `25.11.2` |
| `partitions` | 500 | failed/partial payload | SlurmDBD/TRES 查询连接拒绝；仍返回 7 个分区摘要 |
| `nodes` | 200 | ok | 19 个节点记录：14 IDLE、3 MIXED、2 DOWN/NOT_RESPONDING |
| `jobs` | 200 | ok | 可见 14 个 Students 分区作业：10 RUNNING、4 PENDING |
| `openapi` | 200 | ok | OpenAPI `3.0.3`，info version `Slurm-25.11.2` |

已观测分区与允许 QoS：

| 分区 | 节点范围 | 节点数 | AllowQos |
|---|---|---:|---|
| `CPU-6530` | `anode[01-15]` | 15 | `qos_cpu-6530` |
| `CPU-8358P` | `anode[16-17]` | 2 | `qos_cpu-8358p` |
| `GPU-RTX5090` | `anode[01-15]` | 15 | `qos_gpu-rtx5090` |
| `GPU-A100` | `anode[16-17]` | 2 | `qos_gpu-a100` |
| `P107-RTX5090` | `anode[01-15]` | 15 | `qos_p107-rtx5090` |
| `P107-A100` | `anode[16-17]` | 2 | `qos_p107-a100` |
| `Students` | `anode[05-17]` | 13 | `qos_stu001,qos_stu_default,qos_stu_small,qos_stu_medium,qos_stu_medium_2gpu,qos_stu_long,qos_stu_cpu_long` |

与补充说明对齐后的设计含义：

- `P107-*`、`GPU-*`、`CPU-*` 和 `Students` 分区共享底层节点范围，不应被 UI 表述为独立物理集群；
- 资源预检必须同时检查 partition allow_qos、用户 association、单作业资源、用户组额度和已有作业；
- Docker simulator 应至少覆盖 `Students + qos_stu_medium_2gpu` carrier profile，以及 `PENDING/RUNNING/IDLE/MIXED/DOWN` 状态；
- REST 客户端必须保留原始 `errors`、`warnings`、HTTP status 和 payload 摘要，不应把 HTTP 500 统一映射为“无数据”。

## 2. Required Fields

```yaml
deployment_host:
slurm_api_url:
slurm_api_version:
auth_mode:
credential_lifetime:
shared_fs_visible:
allowed_roots:
service_unix_user:
user_mapping_method:
long_running_worker_allowed:
llm_api_available:
known_restrictions:
```

## 3. Network And Deployment

待确认：

- 107Pilot 运行在哪台校园网服务器或比赛 VM；
- 是否允许 HTTPS 反向代理；
- 后端到 `slurmrestd` 是否走 HTTP 或 HTTPS；
- 如果只能 HTTP，网络路径是否限制在可信内网；
- 是否允许长期运行 API、Worker、SQLite/PostgreSQL；
- 是否允许访问平台 Grafana/Prometheus 入口。

## 4. Slurm REST

必须 probe：

- API version；
- AuthStrategy；
- EndpointSet；
- OpenAPI schema digest；
- jobs list 语义；
- cancel 语义；
- submit smoke；
- HTTP 200 with errors/warnings；
- token expired 行为。

## 5. Identity And Credential

待确认：

- 用户级 Slurm JWT 如何获得；
- token 有效期；
- 是否允许服务端短期缓存；
- Worker 如何在 Run 期间继续查询；
- 是否有 trusted auth proxy；
- 是否存在 SCOW/统一认证可复用。

## 6. File And Evidence

待确认：

- Web/Worker 所在机器能否读取 `/public`；
- Web/Worker 所在机器能否读取 `/home`；
- 服务 Unix 用户是谁；
- 是否支持 ACL 或服务组；
- 是否有平台文件 API；
- 用户能否授权 `.107pilot/runs/<run_id>`；
- `/public` 与 `/home` 是否同一 mount 或别名；
- `/tmp` 是否节点本地。

## 7. Current Decision Status

当前结论：

- 不再要求真实 107 完整接入后才进入主开发；
- Docker 主线和比赛 M1 部署可以立即推进；
- 本报告仅服务 M1-R 只读探测、可选 smoke job 和未来 M2 集成。
