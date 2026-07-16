# Phase 0A：本地可控 Docker 主线计划

## 1. 目标

立即推进本地可控 Docker Slurm 闭环，作为所有后端、Worker、Evidence、Capsule 和 Web 流程的主开发环境。

## 2. Compose 目标服务

```text
mariadb
slurmdbd
slurmctld
slurmrestd
login-node-sim
worker-1
worker-2
pilot107-api
pilot107-worker
pilot107-web
```

当前实现状态：

| 层级 | 状态 | 本地路径 |
|---|---|---|
| Python 工程骨架 | 已启动 | `pyproject.toml`、`src/pilot107` |
| 核心状态模型 | 已实现第一版 | `src/pilot107/core/states.py` |
| SafePath 授权 | 已实现第一版 | `src/pilot107/core/paths.py` |
| 资源预检模型 | 已实现第一版 | `src/pilot107/core/resources.py` |
| REST 语义校验 | 已实现第一版 | `src/pilot107/core/rest_semantics.py` |
| Slurm 后端契约 | 已实现第一版 | `src/pilot107/adapters/slurm.py` |
| REST Slurm 后端 | 已实现基础版 | `RestNativeSlurmBackend` |
| Command Slurm 后端 | 已实现基础版 | `CommandSubmitBackend` |
| API/Worker/Web 边界 | 已建立目录 | `apps/` |
| Docker Slurm Compose | 已建立配置骨架 | `simulator/compose/compose.yml` |
| Slurm 模拟镜像 | 已构建并自检 | `pilot107/slurm-sim:local` |
| Docker Slurm 核心集群 | 已启动并通过 smoke job | `scripts/start-sim-core.sh` |
| Docker simulator live backend | 已通过 smoke job | `DockerSimulatorCommandBackend` |
| 最小 Run 持久化 | 已通过三类 Run smoke | `RunStore`、`RunService` |
| 最小 Worker 对账 | 已通过三类 Run smoke | `RuntimeReconcileWorker` |
| Evidence MVP | 已通过成功 Run smoke | `EvidenceStore`、`DockerSlurmEvidenceCollector`，包含 submission/slurm/logs |
| Evidence 查询读模型 | 已通过成功 Run smoke | `EvidenceQueryService` |
| 最小 HTTP API | 已通过 Run/Evidence GET smoke | `Pilot107HttpApi` |
| Web MVP | 已通过 Web smoke 和 Compose apps profile | `pilot107.web.server` + static UI |
| Docker 多用户 Evidence 权限 | 已通过 live smoke | Alice/Bob/symlink/cross-run 隔离 |
| 失败/取消 Run Evidence | 已通过 live smoke | FAILED/CANCELLED submission/slurm/logs/manifest |

## 3. 用户与目录模拟

Linux 身份：

```text
alice       普通用户 A
bob         普通用户 B
pilot107    Web/Worker 服务用户
slurm       Slurm 服务用户
```

目录：

```text
/public/home/alice
/public/home/bob
/public/app
/pilot107/evidence-derived
```

权限建议：

```text
/public/home/alice   alice:alice   0700 或 0750
/public/home/bob     bob:bob       0700 或 0750
/public/app          root:users    0755
```

应用容器要求：

```yaml
user: "固定的非 root UID:GID"
read_only: true
cap_drop:
  - ALL
security_opt:
  - no-new-privileges:true
```

## 4. Submit Backend

模拟环境同时实现：

```text
RestNativeSubmitBackend
CommandSubmitBackend
```

第一批代码已先实现 `InMemorySlurmBackend`，用于 API/Worker 在 Docker Slurm 接入前对稳定后端契约开发。它不替代 Docker 主线，只用于本地单元测试和接口收敛。

第二批代码已实现：

- `RestNativeSlurmBackend`：面向 `slurmrestd` 的 JSON HTTP 适配器；
- `CommandSubmitBackend`：面向 Docker 模拟集群内部的受控命令适配器；
- `CommandSubmitBackend` 不走 shell，只生成 argv，并对 Slurm 参数做安全字符校验；
- command backend 仅定位为模拟环境能力，不表示真实 107 command proxy 已获授权。

第三批已完成 Docker Slurm 核心闭环：

- `mariadb + slurmdbd + slurmctld + worker-1 + worker-2 + login-node-sim + slurmrestd` 可启动；
- `sinfo` 可见 `debug` 分区和两个 idle worker；
- `alice` 可以通过 `sbatch` 提交作业；
- `sacct` 可读到 `COMPLETED 0:0`；
- stdout 写入 `/public/home/alice/slurm-<job_id>.out`。
- `apps/Dockerfile` 可构建 `pilot107/api:local` 和 `pilot107/worker:local`。
- Compose apps profile 可启动 `pilot107-api` 和 `pilot107-worker`，二者均通过 healthcheck。
- API service builder 可通过 `PILOT107_API_BACKEND` 装配 `none/in-memory/rest-native/command/docker-compose-command`。
- REST transport 已支持 `bearer` 与 `slurm_headers` 两种认证头策略。
- API 已有最小 Recipe/Contract 入口，内置 `recipe_python_cpu@1.0.0`，可从 Contract 创建 VALIDATED Run。
- Python `DockerSimulatorCommandBackend` 可直接驱动 Docker simulator 提交并查询成功作业。
- SQLite `RunService` 可持久化成功 Run，并对账到 `SUCCEEDED 0:0`。
- `RuntimeReconcileWorker` 可从 SQLite 查询 active Run，并由 Worker 对账到 `SUCCEEDED 0:0`。
- `pilot107.worker.service` 已提供常驻 Worker entrypoint、环境变量配置和 health 文件。
- collection task 已使用原子 acquire/lease，避免多 Worker 抢占同一采集任务。
- Worker transition smoke 已覆盖 `FAILED 42:0`、`CANCELLED` 和重启恢复后的 `SUCCEEDED 0:0`。
- Evidence smoke 已覆盖成功 Run 的 submission snapshot、`execution_wrapper.generated`、`sacct`、`scontrol show job`、stdout/stderr tail、environment summary、outputs inventory、derived result summary、sha256 和 manifest。
- EvidenceObject 索引已从 manifest 同步写入 SQLite，覆盖 category、logical path、sha256、size 和 mime type。
- Evidence query smoke 已覆盖成功 Run 的目录树、objects 查询和 collection task 状态返回。
- API evidence smoke 已覆盖 `GET /api/v1/runs/{run_id}/evidence` 的真实 HTTP 查询和 objects 返回。
- API run get smoke 已覆盖 `GET /api/v1/runs/{run_id}` 的真实 HTTP 查询。
- API cancel smoke 已覆盖 `POST /api/v1/runs/{run_id}/cancel` 的真实 HTTP 取消。
- API submit smoke 已覆盖 trusted-header 鉴权下的 `POST /api/v1/runs/prepare` 和 `POST /api/v1/runs/{run_id}/submit` 真实 HTTP 提交。
- Worker service smoke 已覆盖 service packaging 下的 Docker simulator 对账、Evidence 采集和健康文件写入。
- Apps profile smoke 已覆盖应用镜像构建后的 API/Worker 容器启动和 healthcheck。
- Web MVP smoke 已覆盖静态 UI、同源 `/api/v1` 代理、Recipe 查询、Contract 创建、Run prepare/submit。
- Apps profile smoke 已覆盖应用镜像构建后的 API/Worker/Web 容器启动、healthcheck 和 Web 代理 Recipe 查询。
- Web interactions smoke 已覆盖 Compose API/Worker/Web 下的 Contract validate/create、Run prepare/submit、Worker 对账到 `SUCCEEDED`、Evidence objects 查询。

前端演示默认使用显式 `demo` backend：

```text
pilot107-web
→ same-origin /api/v1 proxy
→ pilot107-api demo backend
→ SQLite RunStore
→ pilot107-worker demo backend + DemoEvidenceCollector
→ Evidence tree/tasks/objects
```

`demo` backend 只用于前端交互和比赛界面打磨，不替代 Docker Slurm command backend smoke，也不改变 REST 兼容性结论。
- API container submit smoke 已覆盖 API 容器内 `in-memory` backend 的 prepare/submit。
- API container contract smoke 已覆盖 Recipe 查询、Contract validate/create、Contract prepare Run 和 submit。
- REST auth probe 已生成 simulator matrix：当前 `rest_auth/jwt` 存在，但控制面 `auth_jwt.so` 缺失，live REST submit 仍 blocked。
- Evidence permissions smoke 已覆盖 Alice/Bob 越权拒绝、symlink escape 拒绝和跨 Run 查询隔离。
- Evidence transitions smoke 已覆盖失败和取消 Run 的 Evidence 完整采集。
- Capsule smoke 已覆盖从完整 Evidence 构建 Raw Capsule、写入 `manifest/provenance/checksums` 并 verify。

REST 端口已可达，但认证仍需专项收敛，不阻塞 command backend 主线。

`CommandSubmitBackend` 只在模拟集群内部使用，不代表真实 107 command proxy 已获授权。白名单：

```text
sbatch
squeue
scontrol show job
sacct
scancel
```

## 5. 测试矩阵

| 场景 | 预期 |
|---|---|
| REST + 合法共享 workdir | 成功 |
| REST + 不存在 workdir | 结构化失败 |
| REST + 无权限 workdir | 结构化失败 |
| REST + 本地 `/tmp` 输出 | 警告或拒绝 |
| REST 超时但实际提交成功 | 对账，不重复提交 |
| REST 不可用 | 可切换模拟 command backend |
| command backend 输入 Shell 注入 | 被拒绝 |
| Alice 读取 Bob 目录 | 拒绝 |
| symlink 指向 Bob 目录 | 拒绝 |
| Capsule 跨 Run 读取 | 拒绝 |

## 6. 存储预算

100GB 足够。预算：

| 内容 | 建议预算 |
|---|---:|
| Slurm 基础镜像及层 | 5-10GB |
| Web/API/Worker 镜像 | 3-8GB |
| MariaDB 数据 | 1-3GB |
| 前端依赖和构建缓存 | 2-5GB |
| Python/uv/pip 缓存 | 3-8GB |
| 测试 fixture | 2-5GB |
| Evidence 与 Capsule 示例 | 10-20GB |
| 日志和失败注入数据 | 5GB |
| 备份和发布包 | 5-10GB |
| 安全余量 | 20GB |

硬限制：

- 单个 Run Evidence 默认上限；
- 单个 Capsule 上限；
- 日志保留天数；
- Docker build cache 清理；
- 数据库和 volume 备份轮换；
- 大文件只做 inventory，不复制。
