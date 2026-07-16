# Phase 0A 当前偏差评估

日期：2026-07-12

## 1. 评估基线

对照文档：

- `docs/phase-1/implementation_plan.md`
- `docs/phase-0/docker_mainline_plan.md`
- `/home/knowingthesea/文档/107/design_v1.4/00_设计文档索引.md`
- `/home/knowingthesea/文档/107/design_v1.4/04_Slurm控制模块.md`
- `/home/knowingthesea/文档/107/design_v1.4/07_Run与Worker模块.md`
- `/home/knowingthesea/文档/107/design_v1.4/08_Evidence模块.md`
- `/home/knowingthesea/文档/107/design_v1.4/11_API设计.md`
- `/home/knowingthesea/文档/107/design_v1.4/14_接口设计最终校验.md`

当前阶段仍属于 Phase 0A：本地可控 Docker 主线。

## 2. 当前已达成

| 项目 | 状态 | 说明 |
|---|---|---|
| Python 工程骨架 | 已完成第一版 | `src/pilot107`、`apps`、`tests`、`scripts` 已建立 |
| 核心状态模型 | 已完成第一版 | Run 主状态和子状态已分离 |
| SafePath | 已完成第一版 | 单元测试覆盖越权和 symlink escape |
| ResourcePlan | 已完成第一版 | 基础资源预检已实现 |
| REST 语义校验 | 已完成第一版 | 能识别 payload `errors`/`warnings` |
| REST auth strategy | 已完成第一版 | `UrllibHttpTransport` 支持 `bearer` 与 `slurm_headers` |
| REST auth probe | 已完成，2026-07-13 收敛 | `scripts/probe-sim-rest-auth.sh` 报 supported；从 Ubuntu `slurm-wlm` 源码构建 `auth_jwt.so`（`--with-jwt` + `libjwt-dev`）烤入镜像，`scontrol token` 可签发 JWT，`GET /slurm/v0.0.40/nodes` 带 `X-SLURM-USER-NAME`/`X-SLURM-USER-TOKEN` 返回 200。限制：simulator 锁定 v0.0.40（Slurm 23.11.4 最大 API 版本，`v0.0.41` 路径 404），不可外推到真实 107 Slurm 25.11.2 |
| REST submit probe | 已完成，2026-07-13 收敛 | `scripts/probe-sim-rest-submit.sh` 报 submitted（不再 skipped）；通过 `RestNativeSlurmBackend` 走真实 submit/get/cancel；`scripts/smoke_sim_rest_live.sh` 11 场景 live 矩阵全绿 |
| 真实 107 read-only probe | 已完成一次，结果 partial | job 21039 回收 `configuration_snapshot.json` 和 `probe_report.json`；REST `v0.0.41`、OpenAPI `3.0.3`、Slurm `25.11.2`，`/partitions` 因 SlurmDBD/TRES 连接拒绝返回 HTTP 500 但带可用分区 payload |
| Slurm 后端契约 | 已完成第一版 | `InMemorySlurmBackend`、`RestNativeSlurmBackend`、`CommandSubmitBackend` |
| Docker Slurm 镜像 | 已完成第一版 | `pilot107/slurm-sim:local` |
| Docker Slurm 核心集群 | 已跑通 | `mariadb/slurmdbd/slurmctld/worker/login/slurmrestd` |
| API/Worker 应用镜像 | 已完成第一版 | `apps/Dockerfile` 可构建 `pilot107/api:local` 和 `pilot107/worker:local` |
| Web MVP | 已完成第一版 | `pilot107.web.server` 提供静态 UI 和同源 `/api/*` 代理，支持 Recipe/Contract/Run/Evidence 演示入口 |
| API/Worker/Web 应用镜像 | 已完成第一版 | `apps/Dockerfile` 可构建 `pilot107/api:local`、`pilot107/worker:local` 和 `pilot107/web:local` |
| Compose apps profile | 已跑通第一版 | `pilot107-api`、`pilot107-worker` 和 `pilot107-web` 可启动并通过 healthcheck |
| 前端完整交互后端闭环 | 已完成第一版 | 新增 `demo` backend 和 Demo Evidence collector；Web 代理可完成 Contract、Run、Worker 对账、Evidence 查询 |
| CapabilityProfile ingest | 已完成第一版 | `docker-real107-sim` competition profile、真实 107 probe 目录加载、docs-main 学生 QoS 上限、REST partial payload 语义和 `/api/v1/platform/capabilities` 已实现；Web 资源选择和 Diagnostics 面板已消费 |
| QoS-aware Preflight | 已完成第一版 | CPU/GPU/memory/walltime 数值上限已通过 `CapabilityProfile.qos_limits()` 进入 Contract validate 和 direct Run prepare 的 BLOCK/WARN |
| Diagnosis Store + Rule Engine | 已完成第一版 | `diagnoses` 表、Evidence snippet selector、Diagnosis API、Worker 自动触发诊断已完成；规则覆盖 QoS/partition、command not found、Python package missing、timeout、OOM、nonzero exit |
| API service builder | 已完成第一版 | API 容器可通过 `PILOT107_API_BACKEND` 配置 `none/in-memory/rest-native/command/docker-compose-command` |
| 最小 Recipe/Contract API | 已完成第一版 | 内置 `recipe_python_cpu@1.0.0`，支持 Recipe 查询、Contract validate/create/get/preflight、Contract prepare Run |
| command smoke job | 已跑通 | `alice` 作业可 `COMPLETED 0:0`，stdout 可读 |
| live Docker backend | 已跑通 | `DockerSimulatorCommandBackend` 可提交并查询 `alice` 成功作业 |
| 最小 Run 持久化 | 已跑通三类 Run | `RunService` 可提交、取消，并持久化终态 |
| 最小 Worker 对账 | 已跑通三类 Run | `RuntimeReconcileWorker` 可从 SQLite 拉取 active Run 并对账到终态 |
| Worker service packaging | 已完成第一版 | `pilot107.worker.service` 支持环境变量配置、常驻/once/until-idle、health 文件、Compose worker command/healthcheck |
| Evidence MVP | 已跑通成功/失败/取消 Run | `submission/slurm/logs/environment/outputs/derived/manifest` 已由 Worker 采集 |
| Evidence 查询读模型 | 已跑通成功 Run | `EvidenceQueryService` 返回任务状态和 Evidence 目录树 |
| 最小 HTTP API | 已跑通 prepare/submit/get/cancel/evidence | `GET /healthz`、`POST /api/v1/runs/prepare`、`POST /api/v1/runs/{run_id}/submit`、`GET /api/v1/runs/{run_id}`、`GET /api/v1/runs/{run_id}/evidence`、`POST /api/v1/runs/{run_id}/cancel` |
| 最小 API 鉴权 | 已完成第一版 | 支持 trusted header 身份、缺失身份 401、非法身份 403、提交 owner 绑定、跨用户 Run/Evidence/Cancel 拒绝 |
| Docker 多用户 Evidence 权限 | 已跑通 | Alice OK、Bob path denied、symlink escape denied、cross-run query isolated |
| 失败/取消 Run Evidence | 已跑通 | FAILED/CANCELLED 均生成 submission/slurm/logs/environment/outputs/derived/manifest |

当前校验：

```text
bash scripts/check_phase0_core.sh        PASS, 99 tests
sh simulator/compose/scripts/check-compose-config.sh  PASS
bash scripts/probe-sim-rest-auth.sh      PASS, generated blocked auth matrix
bash scripts/probe-sim-rest-submit.sh    PASS, generated skipped submit matrix
bash scripts/smoke-sim-web-mvp.sh        PASS, Web proxy Recipe/Contract/Run vertical slice
bash scripts/smoke-sim-web-interactions.sh PASS, Web proxy end-to-end Run SUCCEEDED and Evidence collected
bash scripts/build-app-images.sh         PASS, built pilot107/api:local, pilot107/worker:local and pilot107/web:local
bash scripts/check-app-images.sh         PASS, app image imports ok
bash scripts/smoke-sim-apps-profile.sh   PASS, API/Worker/Web containers healthy
bash scripts/smoke-sim-api-container-submit.sh PASS, API container in-memory prepare/submit
bash scripts/smoke-sim-api-container-contract.sh PASS, API container Recipe/Contract prepare/submit
bash scripts/check-sim-core.sh           PASS, worker-[1-2] idle
bash scripts/smoke-sim-command-job.sh    PASS, alice COMPLETED 0:0
bash scripts/smoke-sim-backend-job.sh    PASS, backend smoke alice SUCCEEDED 0:0
bash scripts/smoke-sim-run-service.sh    PASS, persisted run SUCCEEDED 0:0
bash scripts/smoke-sim-worker.sh         PASS, worker reconciled run SUCCEEDED 0:0
bash scripts/smoke-sim-worker-service.sh PASS, worker service reached SUCCEEDED and collected Evidence
bash scripts/smoke-sim-worker-transitions.sh PASS, FAILED/CANCELLED/restart recovery
bash scripts/smoke-sim-evidence.sh       PASS, Evidence MVP artifacts, objects and tasks succeeded
bash scripts/smoke-sim-evidence-query.sh PASS, Evidence tree, objects and task states returned
bash scripts/smoke-sim-api-evidence.sh   PASS, HTTP Evidence endpoint returned tree and objects
bash scripts/smoke-sim-evidence-permissions.sh PASS, Alice/Bob/symlink/cross-run isolation
bash scripts/smoke-sim-evidence-transitions.sh PASS, FAILED/CANCELLED evidence complete
bash scripts/smoke-sim-api-run-get.sh    PASS, HTTP Run summary returned
bash scripts/smoke-sim-api-cancel.sh     PASS, HTTP cancel returned CANCELLED
bash scripts/smoke-sim-api-submit.sh     PASS, trusted-header HTTP prepare/submit reached SUCCEEDED
bash scripts/smoke-sim-capsule.sh        PASS, raw capsule built and verified
```

## 3. 阶段门偏差

Phase 0A 通过条件对照：

| 阶段门 | 当前状态 | 偏差 |
|---|---|---|
| 一个独立 API | 已完成第一版 | 已有最小 HTTP 服务、Recipe/Contract、Run prepare/submit/query/cancel、Evidence endpoint、trusted-header 鉴权、应用镜像、可配置 backend 和 Compose healthcheck |
| 一个独立 Worker | 已完成第一版 | 已有最小轮询对账、恢复验证、采集执行、collection task lease、service entrypoint、健康文件、应用镜像和 Compose worker command/healthcheck |
| 一个可重复 Docker Slurm | 基本完成 | 已可启动并 smoke job，通过 |
| 一个成功 Run | 已完成第一版 | 已通过 Worker live smoke 持久化并对账到 `SUCCEEDED 0:0` |
| 一个失败 Run | 已完成第一版 | 已通过 Worker transition smoke 对账到 `FAILED 42:0` |
| 一个取消 Run | 已完成第一版 | 已通过 Docker `scancel` live smoke 进入 `CANCELLED` |
| 一个可验证 Capsule | 已完成第一版 | Raw Capsule 可从 Evidence 构建，`manifest/checksums` 可 verify |
| 一个可验证 Evidence MVP | 已完成第一版 | 成功/失败/取消 Run 已采集 submission、Slurm detail/accounting、日志 tail、environment、outputs、sha256 和 manifest |
| Evidence API 读模型 | 已完成第一版 | 查询服务和 HTTP route 均已返回目录树 |
| 一次 API/Worker 重启恢复 | 已完成 Worker 第一版 | 重建 Store/Service/Worker 后可从 SQLite 继续对账 |
| Docker 多用户权限测试通过 | 已完成第一版 | Alice 授权采集、Bob 路径拒绝、symlink escape 拒绝、跨 Run 查询隔离均已 live smoke |
| API 身份与 owner 边界 | 已完成第一版 | trusted header 身份、owner 绑定、跨用户 Run/Evidence/Contract/Cancel 拒绝均已覆盖 |

结论：当前 Phase 0A 的本地运行骨架已基本闭合：Docker Slurm、Run 持久化、Worker 对账和 lease、Worker service packaging、API/Worker/Web 应用镜像、API service builder、Compose apps profile、Web MVP、最小 Recipe/Contract API、三类 Run、Evidence MVP、EvidenceObject 索引、derived result summary、Raw Capsule MVP、多用户 Evidence 权限、失败/取消 Run Evidence、`/api/v1` Evidence endpoint、Run prepare/submit/query/cancel endpoint、API trusted-header 鉴权、wrapper/environment/outputs、CapabilityProfile ingest、QoS-aware Preflight、Diagnosis Store + Worker 自动诊断、前端真实 Diagnosis API 展示、Agent explain API `provider=none/campus`、前端 Explain 展示、校园/USTC LLM smoke 脚本、DockerSlurmEvidenceCollector 第一段 EvidenceTransport 迁移、competition profile EvidenceTransport fallback 实机 smoke、success/failure/cancel Evidence+Capsule smoke、100 并发 read/validate/prepare 承载测试已完成；剩余主缺口转为真实 LLM 网关实测、REST 专项收敛、M1 HTTPS/reverse proxy、两机部署脚本和演示剧本。

## 4. 设计偏差

| 偏差 | 严重度 | 说明 | 处理建议 |
|---|---|---|---|
| REST 认证未收敛 | 已解决（2026-07-13 REST 专项收敛） | 路径 A 完成：从 Ubuntu `slurm-wlm` 源码构建 `auth_jwt.so`（SchedMD 无 apt 仓库、`download.slurm.sh` NXDOMAIN），仅 COPY 单个 `.so` 到镜像；`slurm.conf`/`slurmdbd.conf` 增 `AuthAltTypes=auth/jwt`；`jwt_hs256.key` 烤入镜像（弃用 bind-mount）；slurmrestd 增 `SLURM_JWT=daemon`；live 验证 `scontrol token` + `GET /slurm/v0.0.40/nodes` 返回 200（anode16/anode17，Slurm 23.11.4）。`RestAuthStyle.SLURM_HEADERS` 确认为唯一可用形态（无 Bearer）。bad token → HTTP 500（error_number 7000），仅 no-token → 401。accounting 走 `/slurmdb/v0.0.40/jobs`（`/slurm/v0.0.40/accounting` 404）。详见 `docs/phase-1/rest_convergence_report.md` | 残余限制：simulator 锁定 v0.0.40（Slurm 23.11.4），不可外推真实 107；唯一 job name marker 仍硬编码 `pilot107-run`，per-run 唯一 marker 暂缓；真实 107 submit/cancel/file read 仍未 probe（M1-R 非阻塞） |
| command smoke 未走应用后端 | 已解决 | 已新增 `DockerSimulatorCommandBackend` 和 live backend smoke | 后续 RunService/Worker 复用该 backend |
| accounting owner 瞬态空值 | 已解决第一版 | live smoke 发现终态刚落库时 `sacct` owner 可能短暂不可用，已改为 retryable transport error 而不是直接判越权 | 后续如再出现 accounting 延迟，Worker 继续重试 |
| API/Worker 未实现 | 已解决第一版 | Worker 最小对账、恢复、采集、collection task lease、service entrypoint、应用镜像已完成；API Recipe/Contract、Evidence endpoint、Run prepare/submit/query/cancel、trusted-header 鉴权、应用镜像和 service builder 已完成 | 下一步补 REST 专项或 M1 部署脚本 |
| API 容器提交后端 | 已解决第一版 | API 容器可配置 backend，且已用 in-memory backend 完成容器内 prepare/submit smoke | REST live submit 仍受 simulator REST auth 影响 |
| Web 前端缺失 | 已解决第一版 | 已有 Web MVP、同源 API 代理、Recipe/Contract/Run/Evidence 页面和 Compose `pilot107-web` healthcheck | 后续补真实 Slurm 演示模式、Capsule 下载/构建入口和 HTTPS 反代 |
| 前端交互不能完成 | 已解决第一版 | 原因是 `in-memory`/`rest-native` 不适合 API/Worker 分进程前端演示；已新增跨进程 `demo` backend 和 Demo Evidence collector | 等前端设计包后只替换/扩展 UI，不再依赖后端临时假链路 |
| Evidence/Capsule 未完整 | 低 | Evidence MVP、EvidenceObject 索引、derived summary、Raw Capsule verify/export、查询读模型、HTTP route、多用户权限和失败/取消 evidence smoke 已完成 | 后续随诊断和脱敏 Export Capsule 扩展 |
| Slurm 版本低于真实平台资料 | 中 | simulator 使用 Ubuntu Slurm 23.11.4，资料参考真实平台为 25.11 | 标记为模拟环境差异；后续 REST matrix 不可把 23.11 行为外推到真实 107 |
| 真实 107 QoS/association 覆盖 | 已解决第一版 | simulator 已暴露 107 风格分区和 AllowQos，新增 `apply-sim-real107-profile.sh`、`smoke-sim-real107-profile.sh`，并验证 `Students/qos_stu_medium_2gpu --gres=gpu:A100:1` 可提交完成；应用 preflight 新增真实 107 profile 校验 | 残余限制：Ubuntu Slurm 23.11 accounting 未暴露 `gres/gpu` TRES，`GrpTRES=gres/gpu` 不能由 live SlurmDBD 强制，只能由 profile/preflight 模拟 |
| 真实 REST partial payload 覆盖 | 已解决第一版 | `check_slurm_rest_semantics` 新增 partial payload 模式，测试覆盖 HTTP 非 2xx/有 errors 但 `partitions` payload 可用时降级为 warning；CapabilityProfile ingest 已保留 `partial_payload_with_errors`，前端 Diagnostics 已展示该语义 | 后续 Diagnosis API 引用该语义 |
| 并发提交脚本覆盖 | 已解决 | 复核时发现同一 workdir 下固定 `pilot107-submit.sbatch` 会被并发提交互相覆盖 | 已改为基于 `idempotency_key` 的唯一脚本文件名，并发 smoke 已复验 |
| worker 容器使用 privileged/cgroup host | 中 | 为启动 `slurmd` 做的基础设施例外；应用容器仍必须非 root、drop caps | 文档保留说明；不得复制到 `pilot107-api/worker/web` |
| `slurmdbd.conf` 有重复来源 | 中 | Compose 目录仍有旧 `simulator/compose/slurm/slurmdbd.conf`，实际使用镜像内置 0600 文件 | 后续清理或标注 unused，避免维护漂移 |
| REST 版本/API 能力未形成 CapabilityProfile | 已解决第一版 | 支持从真实 probe 输出加载 REST endpoint、API version、OpenAPI digest、partial payload 语义；默认 competition profile 暴露 `v0.0.41`，前端已消费 | 后续实现自动刷新任务 |
| 多用户权限仅部分验证 | 已解决第一版 | Docker live smoke 已验证 Alice/Bob/symlink/cross-run 隔离；API 已验证 owner 边界 | 后续扩展到更完整 RBAC/审计模型 |
| SQLite/DB 模型未落地 | 已解决第一版 | 已有 `runs/run_events/collection_tasks` 最小表 | 下一步由 Worker 使用该存储 |
| Competition EvidenceTransport 默认策略 | 已解决第一版 | 真实验证显示非 root 应用容器直接读取 Docker volume 下的用户 Slurm 日志会触发权限问题；已将 docker volume transport 改为显式 opt-in，competition 默认 command-gateway fallback，并通过专项 smoke | 后续如要启用 direct volume，需要先设计 ACL/组权限或授权文件代理 |
| Gateway rate limit 在批量采集下放大失败 | 已解决第一版 | collection retry 已增加指数退避；competition gateway 默认限流提升到 6000/min；100 并发 read/validate/prepare 已通过 0 错误 | 后续 workflow 级并发仍需单独压测 Slurm 作业队列和 Worker 吞吐 |

## 5. 下一步决策

推荐不立刻硬啃 REST submit。理由：

- 当前比赛主线已经有可工作的 command smoke；
- REST 认证是 Slurm 23.11 simulator 配置问题，不应阻塞 API/Worker/Evidence 主线；
- 设计允许模拟环境中存在受控 `CommandSubmitBackend`；
- Phase 0A 阶段门最大的缺口是应用闭环，而不是 Slurm 控制面本身。

下一步应优先补齐应用闭环的最小纵切：

```text
Docker Slurm command backend
→ RunService + SQLite
→ 最小 Worker 对账
→ Evidence MVP
→ 最小 API
```

REST 认证保持为并行专项：

```text
slurmrestd auth probe
→ simulator-only JWT 或 local auth
→ REST list/query
→ REST submit matrix
```

状态：已完成第一版 matrix。当前 `probe-sim-rest-submit` 在无可用 REST auth 时输出 `skipped`，一旦 auth probe 出现 supported 策略，将继续尝试 `/slurm/v0.0.41/job/submit`。

## 6. 下一批实施计划

### 0A-Next-1：让应用代码真正调用 Docker simulator

目标：

- 实现 `DockerComposeCommandRunner` 或等价 runner；
- 通过 `docker compose exec -T login-node-sim ...` 执行白名单 Slurm 命令；
- `CommandSubmitBackend` 可对 live simulator 提交、查询、取消。

验收：

- 单元测试仍通过；
- 新增 live smoke：通过后端类提交 `alice` 作业，得到 `COMPLETED 0:0`。

状态：已完成。当前验证为 `backend smoke job 5 alice SUCCEEDED 0:0`。

### 0A-Next-2：最小 Run 持久化

目标：

- SQLite + WAL；
- `runs`、`run_events`、`collection_tasks` 最小表；
- `RunService.submit/get/cancel`。

验收：

- 成功 Run 从 `SUBMITTED` 对账到 `SUCCEEDED`；
- 失败 Run 对账到 `FAILED`；
- 取消 Run 对账到 `CANCELLED`。

状态：已完成第一版。成功、失败和取消 Run 均已有 live smoke。

### 0A-Next-3：最小 Worker

目标：

- 后台轮询 active runs；
- 调用 backend `get_job` 和 accounting；
- 写事件；
- 可重启后继续处理。

验收：

- Worker 停止/重启后能继续对账已有 Run。

状态：已完成第一版。已实现最小 active Run 轮询、三类 Run live smoke、重启恢复、collection task 原子 acquire/lease、worker service entrypoint、Compose 常驻 worker 和 healthcheck。

### 0A-Next-4：Evidence MVP

目标：

- 建立 `runs/<run_id>/submission/slurm_submit_response.json`；
- 采集 `scontrol/sacct`；
- 采集 stdout/stderr tail 和最终 sha256；
- 输出最小 manifest。

验收：

- Web/API 尚未完整时，CLI/脚本可验证 evidence 文件存在且引用 job_id。

状态：已完成第一版。成功、失败、取消 Run 均已通过 Evidence live smoke，且包含 submission、slurm、logs、environment、outputs 和 manifest；EvidenceObject 索引、derived summary、Evidence 查询读模型、HTTP route、Raw Capsule verify/export 均已完成第一版。

### 0A-Parallel：REST 认证专项

目标：

- 确认 Slurm 23.11 `slurmrestd` simulator 可用认证模式；
- 若可行，接入 simulator-only JWT；
- 若不可行，记录 REST simulator limitation，不阻塞 command 主线。

## 7. 设计文档树复核结论

复核时间：2026-07-10。

对照 `design_v1.4` 的 20 份模块文档，当前实现与文档树整体方向一致：

- 已遵守“比赛主线优先 Docker Slurm，高保真模拟不阻塞真实 107”的基线；
- 已保持 `SlurmControlBackend`、`EvidenceStore/Collector`、API 读模型分离，没有合并成万能后端；
- Run 主状态与 Evidence collection state 已分离；
- Worker 已成为状态对账和 Evidence 采集入口，Web/API 未直接扫描证据文件；
- submission/slurm/logs/manifest 已进入统一 Evidence Store；
- `GET /api/v1/runs/{run_id}/evidence` 已有最小 HTTP route；
- M1 HTTPS 决策已写入部署计划：浏览器入口 HTTPS，reverse proxy 终止 TLS，内部 API 走 localhost/私网 HTTP。

仍存在的主要设计偏差：

| 设计项 | 当前偏差 | 影响 | 建议 |
|---|---|---|---|
| API `Base URL /api/v1` | 已解决第一版，`/api/v1/runs/{id}/evidence` 已可用，旧本地路径暂保留 | 低 | 后续新增 Run API 必须挂到 `/api/v1` |
| Run API | 已有 `POST /api/v1/runs/prepare`、`POST /api/v1/runs/{id}/submit`、`GET /api/v1/runs/{id}` 和 `POST /api/v1/runs/{id}/cancel` | 已能驱动模拟环境最小运行闭环，且已支持 trusted-header owner 绑定 | 后续补 Contract API |
| Worker 租约 | 已完成第一版，collection task 使用原子 acquire/lease，完成/失败时校验 lease owner；Worker service 支持 worker_id、batch、interval、lease env 配置 | 仍缺更完整运维指标 | M1 前补镜像化和服务日志策略 |
| EvidenceObject 表 | 已完成第一版，`evidence_objects` 从 manifest upsert，API Evidence 返回 `objects` | 仍缺更丰富的来源/诊断字段 | 后续随诊断模块扩展 |
| Evidence 阶段覆盖 | 已有 submission/slurm/logs/environment/outputs/derived 且覆盖成功/失败/取消 | Phase 4 MVP 已完成 | 后续扩展成功判据和冲突记录 |
| 脚本三层证据 | 已有 user/submitted/wrapper 三层证据 | 低 | 后续 submit API 要继续保持三层分离 |
| Capsule | 已完成 Raw Capsule manifest/checksums verify/export 第一版 | 尚缺脱敏 Export Capsule 和外部发送流程 | 比赛版先保留 Raw Capsule MVP |
| Docker 多用户端到端 | 已完成 Alice/Bob/symlink/cross-run Evidence live smoke | API trusted-header owner 校验已完成第一版 | 后续扩展 RBAC、审计日志和反向代理集成 |
| REST submit | REST backend 有单测，simulator REST auth 未收敛 | 不阻塞 command 主线，但影响 REST matrix | 继续作为并行专项 |

## 8. 下一步计划

建议下一步优先级如下。

### P0：Docker 多用户 Evidence 权限 live 测试

目标：

- 验证 Alice 不能通过 Evidence collector 读取 Bob 的日志；
- 验证 symlink 指向 Bob 目录时被拒绝；
- 验证 API 查询只返回指定 Run 的 Evidence Store，不跨 Run；
- 验证 Docker simulator 中 `/public/home/alice`、`/public/home/bob` 权限设置符合比赛演示需要。

验收：

```bash
bash scripts/smoke-sim-evidence-permissions.sh
```

预期：

```text
alice evidence ok
bob path denied
symlink escape denied
cross-run query denied/not found
```

状态：已完成第一版。当前验证为：

```text
evidence permission smoke alice evidence ok
bob path denied
symlink escape denied
cross-run query isolated
```

### P1：失败/取消 Run Evidence smoke

目标：

- 失败 Run 也生成 submission/slurm/logs/manifest；
- 取消 Run 也生成 terminal accounting/log evidence，缺失 stderr 时记录 warning 而不是失败；
- manifest 中保留 `run_state`、`exit_code`、artifact sha256。

验收：

```bash
bash scripts/smoke-sim-evidence-transitions.sh
```

状态：已完成第一版。当前验证为：

```text
evidence transition smoke failed=...:FAILED:42:0 cancelled=...:CANCELLED:None
```

### P2：API 路径契约修正

目标：

- HTTP API 支持 `/api/v1/runs/{run_id}/evidence`；
- 保留 `/healthz`；
- 错误结构逐步贴近 `11_API设计.md`；
- 后续 Run 提交/查询/取消都挂到 `/api/v1`。

验收：

```bash
bash scripts/smoke-sim-api-evidence.sh
```

状态：已完成第一版。当前验证 URL 为：

```text
http://127.0.0.1:<port>/api/v1/runs/<run_id>/evidence
```

### P3：最小 Run HTTP API

状态：已完成第一版。`GET /api/v1/runs/{run_id}` 和 `POST /api/v1/runs/{run_id}/cancel` 均已通过 live smoke。

已完成目标：

- `POST /api/v1/runs/{run_id}/cancel`；
- 先不做完整 Contract/prepare，保留 smoke-only submit 脚本入口或内部服务入口。

验收：

- HTTP 可查询 Run 状态；
- HTTP 可取消一个长作业；
- `RunStore` 写入 `CANCELLED`。

当前验证：

```text
api cancel smoke run_... job=... state=CANCELLED
```

### P4：Evidence/Capsule 后续

目标：

- 已补 `execution_wrapper.generated`；
- 已补 environment summary；
- 已补 outputs inventory；
- 已补最小 Capsule manifest verify/export。

进入条件：

- P0/P1 权限和失败/取消 evidence 均通过。
