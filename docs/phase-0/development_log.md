# Phase 0A 本地开发日志

## 2026-07-10

### 已完成

- 建立 `107pilot` 本地工程骨架；
- 建立 API、Worker、Web、Adapter、Core 的目录边界；
- 实现 Run 生命周期状态与 Evidence/Diagnosis/Capsule 子状态分离；
- 实现 SafePath 授权，覆盖越权路径与 symlink escape；
- 实现 ResourcePlan、ArraySpec 和基础资源预检；
- 实现 Slurm REST payload 的语义级成功/失败判断；
- 实现 `SlurmBackend` 契约和 `InMemorySlurmBackend`；
- 覆盖提交、查询、取消、幂等提交、用户隔离、资源预检、REST 语义和路径授权测试；
- 将核心测试脚本切换为标准库 `unittest`，避免第一天开发依赖外部下载。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
```

结果：

```text
Ran 17 tests
OK
```

### 下一步

1. 实现 Docker Slurm Compose 骨架；
2. 建立 alice/bob/pilot107/slurm 用户和 `/public/home/*` 权限模拟；
3. 接入 `RestNativeSubmitBackend` 的 HTTP 客户端与语义校验；
4. 增加 `CommandSubmitBackend` 白名单命令渲染与注入拒绝测试；
5. 启动最小 API 服务，先暴露 preflight、submit、get job、cancel。

## 2026-07-10 第二批

### 已完成

- 实现 `RestNativeSlurmBackend`，覆盖 submit、get job、cancel 的基础 REST 路径；
- 实现 `UrllibHttpTransport`，支持 Bearer token、JSON body、HTTP 错误体解析；
- 实现 `CommandSubmitBackend`，覆盖 `sbatch`、`squeue`、`sacct`、`scancel` 基础流程；
- command backend 使用 argv 调用，不使用 shell；
- command backend 对 `partition`、`qos`、`gpu_type`、`array`、`job_id` 等字段做安全字符校验；
- command backend 在 submit 前执行 SafePath workdir 授权；
- 新增 REST backend 与 command backend 单元测试；
- 建立 Docker Slurm 模拟集群 Compose 骨架；
- 建立 Slurm、slurmdbd、cgroup、MariaDB 初始化和 `/public` 初始化配置。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
sh simulator/compose/scripts/check-compose-config.sh
```

结果：

```text
Ran 29 tests
OK
compose config ok
```

### 下一步

1. 补 Slurm simulator image 的 Dockerfile 或接入可用基础镜像；
2. 在镜像内建立 `alice`、`bob`、`pilot107`、`slurm` 用户；
3. 启动 `mariadb + slurmdbd + slurmctld + worker + slurmrestd`；
4. 用 `CommandSubmitBackend` 跑第一条 `sleep` job；
5. 用 `RestNativeSlurmBackend` 查询、提交、取消，并记录 REST submit 兼容性差异。

## 2026-07-10 第三批

### 已完成

- 构建本地 `pilot107/slurm-sim:local` 镜像；
- 镜像内置 Slurm 23.11.4、MariaDB server/client、MUNGE、`slurmrestd`、`sbatch/squeue/sacct/scancel`；
- 镜像内建立 `alice`、`bob`、`pilot107`、`slurm` 用户；
- 修复 MUNGE 前台启动导致 socket 失效的问题；
- 将 MariaDB 服务改为使用本地 simulator 镜像，避免依赖外部 `mariadb:11.4` 镜像拉取；
- 将 `slurmdbd.conf` 内置进镜像并设置为 0600；
- 为 worker 容器配置 cgroup 权限，使 `slurmd` 可稳定启动；
- 启动完整核心集群：`mariadb`、`slurmdbd`、`slurmctld`、`worker-1`、`worker-2`、`login-node-sim`、`slurmrestd`；
- `sinfo` 显示 `debug` 分区下 `worker-[1-2]` 为 `idle`；
- `scontrol ping` 显示 primary controller 为 `UP`；
- 以 `alice` 提交第一条 `sleep` smoke job，Job 1 完成，`sacct` 为 `COMPLETED 0:0`，stdout 位于 `/public/home/alice/slurm-1.out`。

### 当前校验

```bash
bash scripts/build-slurm-sim-image.sh
bash scripts/check-slurm-sim-image.sh
bash scripts/start-sim-core.sh
bash scripts/check-sim-core.sh
bash scripts/smoke-sim-command-job.sh
```

已验证：

```text
debug* up 1:00:00 2 idle worker-[1-2]
Slurmctld(primary) at slurmctld is UP
alice smoke job COMPLETED 0:0
```

### 未完成

- `slurmrestd` TCP 端口可达，但当前 `rest_auth/local` 仍返回 `Authentication failure`；
- 下一步需要专项配置 REST 认证，优先尝试 `rest_auth/jwt` 与 simulator-only JWT key；
- REST 不影响当前 command backend 与 Docker Slurm 作业闭环。

## 2026-07-10 第四批

### 已完成

- 新增 `DockerComposeExecutor`，用结构化 argv 调用 Docker Compose service；
- 新增 `DockerSimulatorCommandBackend`，在 `login-node-sim` 内完成脚本写入、`sbatch`、`squeue`、`sacct`、`scancel`；
- 后端提交不再依赖手写 shell smoke，而是由 Python backend 类驱动 Docker Slurm；
- 容器内 workdir 使用 `realpath -m` 做授权根校验；
- 新增单元测试覆盖 Docker Compose argv、容器内脚本 staging、越权路径拒绝、终态 accounting 解析和 cancel 用户身份；
- 新增 live smoke：`scripts/smoke-sim-backend-job.sh`。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/check-sim-core.sh
bash scripts/smoke-sim-backend-job.sh
```

结果：

```text
Ran 34 tests
OK
worker-[1-2] idle
Slurmctld(primary) at slurmctld is UP
backend smoke job 5 alice SUCCEEDED 0:0
```

### 下一步

1. 建立最小 SQLite Run 持久化；
2. 实现 `RunService.submit/get/cancel`；
3. 实现最小 Worker 对账；
4. 用 live backend 跑成功、失败、取消三类 Run；
5. 再接 Evidence MVP。

## 2026-07-10 第五批

### 已完成

- 新增 SQLite `RunStore`；
- 建立 `runs`、`run_events`、`collection_tasks` 最小表；
- SQLite 启用 WAL 和 foreign key；
- 新增 `RunRecord`、`RunEvent`；
- 新增 `RunService.submit/get/reconcile_once/cancel`；
- 提交流程记录 `VALIDATED -> SUBMITTING -> SUBMITTED`；
- 对账流程可把 Slurm terminal snapshot 写回 Run；
- terminal Run 自动创建 `terminal_accounting` 和 `logs_finalize` collection task 占位；
- 新增 RunService 单元测试；
- 新增 `scripts/smoke-sim-run-service.sh`，通过 Docker simulator live backend 提交并持久化成功 Run。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/smoke-sim-run-service.sh
```

结果：

```text
Ran 38 tests
OK
run service smoke run_6e3e7412c1f5426e9229234b2234e2ac job=7 state=SUCCEEDED exit=0:0
events=run.created,run.submitting,run.submitted,run.snapshot,run.snapshot,run.snapshot
tasks=runtime_status,terminal_accounting,logs_finalize
```

### 下一步

1. 实现最小 Worker 循环；
2. Worker 从 SQLite 查询 active runs；
3. Worker 调用 backend `get_job` 并写回状态；
4. 增加失败 Run 和取消 Run live smoke；
5. 验证 Worker 停止/重启后可继续对账。

## 2026-07-10 第六批

### 已完成

- 新增 `ACTIVE_JOB_RUN_STATES` 和 `TERMINAL_RUN_STATES`，统一运行态/终态判断；
- `RunStore` 新增 `list_active_job_runs()`，按更新时间拉取需要对账的 Slurm Run；
- 新增 `RuntimeReconcileWorker`；
- Worker 单次 `tick()` 会查询 active runs、调用 `RunService.reconcile_once()`、返回本轮 checked/terminal/error 统计；
- Worker 对 Slurm backend 临时错误不做终态转换，保留 Run 供后续 tick 继续对账；
- 新增 Worker 单元测试；
- 新增 `scripts/smoke-sim-worker.sh`，通过 Docker simulator live backend 提交作业，并由 Worker 完成状态对账。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/smoke-sim-worker.sh
```

结果：

```text
Ran 41 tests
OK
worker smoke run_7e1e1dbbe62a42968545b98fac35f8d6 job=8 state=SUCCEEDED exit=0:0 checked=10
events=run.created,run.submitting,run.submitted,run.snapshot,run.snapshot,run.snapshot
tasks=runtime_status,terminal_accounting,logs_finalize
```

### 下一步

1. 增加失败 Run live smoke；
2. 增加取消 Run live smoke；
3. 验证 Worker 停止/重启后可继续对账已有 Run；
4. 再进入 Evidence MVP：`sacct/scontrol/stdout/stderr/manifest`。

## 2026-07-10 第七批

### 已完成

- 新增 `scripts/smoke-sim-worker-transitions.sh`；
- 验证失败 Run：作业脚本 `exit 42` 后，Worker 对账为 `FAILED 42:0`；
- 验证取消 Run：`RunService.cancel()` 走 Docker simulator `scancel`，Run 进入 `CANCELLED`；
- 验证 Worker 重启恢复：提交后丢弃原 `RunStore/RunService/Worker` 对象，再从同一个 SQLite DB 重建 Worker，仍可继续对账到 `SUCCEEDED 0:0`；
- 新增 `RunStore.list_active_job_runs()` 单元测试，确认终态 Run 和未提交 Run 不会被 Worker 重新拉取。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/smoke-sim-worker-transitions.sh
```

结果：

```text
Ran 42 tests
OK
worker transition smoke failed=run_b3a15e408c4945d2bb2cee2394d7c376:9:FAILED:42:0 cancelled=run_3fea675274684d00b81c0b9bcfc1b3b7:10:CANCELLED:None recovered=run_147db3655aca4d0ea1d24c3342aeda39:11:SUCCEEDED:0:0
```

### 下一步

1. 进入 Evidence MVP；
2. 对每个 terminal Run 采集 `sacct`、`scontrol show job`、stdout/stderr tail；
3. 生成最小 manifest 和 sha256；
4. 将 `collection_tasks` 从占位推进到可执行状态机。

## 2026-07-10 第八批

### 已完成

- 新增 `CollectionTaskRecord` 和 due task 查询；
- `RunStore` 支持 collection task `running/succeeded/failed_retryable/failed_permanent` 状态推进；
- 根据 collection task 聚合刷新 `runs.collection_state`；
- 新增 `EvidenceStore`，按 `runs/<run_id>/...` 写入本地 Evidence Store；
- 新增 `DockerSlurmEvidenceCollector`；
- `terminal_accounting` 采集 `sacct` 和 `scontrol show job`；
- `logs_finalize` 采集 stdout/stderr metadata、tail 和 sha256；
- manifest 写入 `manifest/manifest.json`，并为已存在 Evidence 文件记录 sha256、size、content type 和 `evidence://` 引用；
- `RuntimeReconcileWorker` 接入 `collection_tasks`，每轮可同时做 Run 对账和 Evidence task 执行；
- 新增 `scripts/smoke-sim-evidence.sh`，完成 Docker Slurm 成功 Run 的 Evidence MVP live smoke。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/check_phase1_docs.sh
cd simulator/compose && sh scripts/check-compose-config.sh
bash scripts/smoke-sim-evidence.sh
```

结果：

```text
Ran 46 tests
OK
Phase 0 planning document skeleton is present.
compose config ok
evidence smoke run_080faf542de340cbbe57d56ef7fda5ab job=16 state=SUCCEEDED collection=succeeded artifacts=4
tasks=logs_finalize:succeeded,runtime_status:succeeded,terminal_accounting:succeeded
```

### 下一步

1. 补提交证据：`submission/slurm_submit_response.json` 和用户脚本快照；
2. 补 Evidence API 查询目录树；
3. 补 Docker 多用户 Evidence 端到端权限测试；
4. 再进入最小 API。

## 2026-07-10 第九批

### 已完成

- `runs` 表新增 `submit_response_json`，并在启动时自动补列；
- `RunRecord` 新增 `submit_response`；
- `RunService` 提交成功后保存 `SubmitReceipt.raw_response`；
- 提交后新增 `submission_snapshot` collection task；
- `DockerSlurmEvidenceCollector` 新增 `submission_snapshot` 采集器；
- 写入 `submission/slurm_submit_response.json`；
- 写入 `submission/user_script.original.sh`；
- 写入 `submission/submitted_script.resolved.sh`；
- Evidence manifest 现在覆盖 submission、slurm 和 logs 全部已采集文件。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/check_phase1_docs.sh
cd simulator/compose && sh scripts/check-compose-config.sh
bash scripts/smoke-sim-evidence.sh
```

结果：

```text
Ran 46 tests
OK
Phase 0 planning document skeleton is present.
compose config ok
evidence smoke run_37162c07ef924394a3211b9295d9f092 job=18 state=SUCCEEDED collection=succeeded artifacts=7
tasks=logs_finalize:succeeded,runtime_status:succeeded,submission_snapshot:succeeded,terminal_accounting:succeeded
```

### 下一步

1. 补 Evidence API 查询目录树；
2. 补 Docker 多用户 Evidence 端到端权限测试；
3. 补失败/取消 Run evidence smoke；
4. 再进入最小 API。

## 2026-07-10 第十批

### 已完成

- 新增 `EvidenceQueryService`，作为 `GET /runs/{run_id}/evidence` 的读模型；
- Evidence 查询返回 Run 基本信息、`collection_state`、collection task 状态和 Evidence 目录树；
- 目录树对文件返回 logical path、size、sha256 和 content type；
- 新增 Evidence 查询单元测试；
- 新增 `scripts/smoke-sim-evidence-query.sh`；
- live smoke 先完成 Docker Slurm Run 和 Evidence 采集，再通过查询服务验证关键文件路径。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/check_phase1_docs.sh
cd simulator/compose && sh scripts/check-compose-config.sh
bash scripts/smoke-sim-evidence-query.sh
```

结果：

```text
Ran 47 tests
OK
Phase 0 planning document skeleton is present.
compose config ok
evidence query smoke run_4e19efad936d4286a3d75af76d0030bb job=20 collection=succeeded files=8
tasks=submission_snapshot:succeeded,runtime_status:succeeded,terminal_accounting:succeeded,logs_finalize:succeeded
```

### 下一步

1. 接最小 HTTP API 薄封装：`GET /runs/{run_id}/evidence`；
2. 或先补 Docker 多用户 Evidence 端到端权限测试；
3. 补失败/取消 Run evidence smoke。

## 2026-07-10 第十一批

### 已完成

- 新增 `Pilot107HttpApi`，实现最小 stdlib HTTP route；
- 新增 `GET /healthz`；
- 新增 `GET /runs/{run_id}/evidence`，复用 `EvidenceQueryService`；
- 新增 `pilot107.api.dev_server`；
- 新增 `scripts/serve-api.sh`，用于本地启动 API；
- 新增 HTTP API 单元测试；
- 新增 `scripts/smoke-sim-api-evidence.sh`，通过真实 HTTP GET 验证 Evidence endpoint；
- 明确 HTTPS 策略：本地 M0 可用 localhost HTTP；比赛 M1 浏览器入口必须 HTTPS，由应用节点 reverse proxy 终止 TLS，API 内部监听 localhost 或私网 HTTP。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/check_phase1_docs.sh
cd simulator/compose && sh scripts/check-compose-config.sh
bash scripts/smoke-sim-api-evidence.sh
```

结果：

```text
Ran 51 tests
OK
Phase 0 planning document skeleton is present.
compose config ok
api evidence smoke run_195d4e74d2284abe9f60be339cd905ed job=22 collection=succeeded files=8
```

### 下一步

1. 补 Docker 多用户 Evidence 端到端权限测试；
2. 补失败/取消 Run evidence smoke；
3. 之后扩展 HTTP API：Run 查询、提交、取消。

## 2026-07-10 第十二批

### 已完成

- 新增 Evidence collector 越权路径单元测试；
- 修正 Evidence manifest 扫描 `.sh` 文件时的 content type；
- 新增 `scripts/smoke-sim-evidence-permissions.sh`；
- Docker live smoke 验证 Alice 授权日志可采集；
- Docker live smoke 验证 Bob 路径被 `allowed_roots` 拒绝；
- Docker live smoke 验证 Alice 目录内 symlink 指向 Bob 文件时被 canonical path 检查拒绝；
- Docker live smoke 验证 Evidence 查询只返回指定 Run 的 Evidence Store，不泄漏其他 Run。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/check_phase1_docs.sh
bash scripts/smoke-sim-evidence-permissions.sh
```

结果：

```text
Ran 52 tests
OK
Phase 0 planning document skeleton is present.
evidence permission smoke alice evidence ok run=run_perm_alice_afbeb81bec
bob path denied
symlink escape denied
cross-run query isolated
```

### 下一步

1. 补失败/取消 Run evidence smoke；
2. 修正 API base path 到 `/api/v1`；
3. 之后扩展 Run HTTP API。

## 2026-07-10 第十三批

### 已完成

- 新增 `scripts/smoke-sim-evidence-transitions.sh`；
- 失败 Run 现在可完整生成 submission/slurm/logs/manifest；
- 取消 Run 现在可完整生成 submission/slurm/logs/manifest；
- manifest 校验包含 `run_state`、`exit_code` 和全部关键 artifact；
- collection tasks 在失败/取消 Run 中也全部进入 `succeeded`。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/check_phase1_docs.sh
cd simulator/compose && sh scripts/check-compose-config.sh
bash scripts/smoke-sim-evidence-transitions.sh
```

结果：

```text
Ran 52 tests
OK
Phase 0 planning document skeleton is present.
compose config ok
evidence transition smoke failed=run_1b1d208e0c4c49eaa67d20eca6003230:24:FAILED:42:0 cancelled=run_4af97dbd69ca4019a82b8316cac374df:25:CANCELLED:None
```

### 下一步

1. 修正 API base path 到 `/api/v1`；
2. 之后扩展 Run HTTP API：Run 查询、取消；
3. 再进入 wrapper/environment/outputs。

## 2026-07-10 第十四批

### 已完成

- `Pilot107HttpApi` 支持 `/api/v1` 前缀；
- `GET /api/v1/runs/{run_id}/evidence` 已通过单元测试和 live smoke；
- 旧的本地兼容路径 `/runs/{run_id}/evidence` 暂时保留；
- `scripts/smoke-sim-api-evidence.sh` 改为请求 `/api/v1/runs/{run_id}/evidence`。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/check_phase1_docs.sh
cd simulator/compose && sh scripts/check-compose-config.sh
bash scripts/smoke-sim-api-evidence.sh
```

结果：

```text
Ran 53 tests
OK
Phase 0 planning document skeleton is present.
compose config ok
api evidence smoke run_eb5dfd3c59734d69aef709155cb32ed8 job=28 collection=succeeded files=8
url=http://127.0.0.1:39819/api/v1/runs/run_eb5dfd3c59734d69aef709155cb32ed8/evidence
```

### 下一步

1. 扩展最小 Run HTTP API：`GET /api/v1/runs/{run_id}`；
2. 再实现 `POST /api/v1/runs/{run_id}/cancel`；
3. 随后补 wrapper/environment/outputs。

## 2026-07-10 第十五批

### 已完成

- `Pilot107HttpApi` 新增 `GET /api/v1/runs/{run_id}`；
- Run summary 返回 `state`、`terminal_state`、`exit_code`、`result_status`、`collection_state`、`diagnosis_state`、`capsule_state`、`job_id`、`submit_strategy` 和时间字段；
- 新增 Run summary 单元测试；
- 新增 `scripts/smoke-sim-api-run-get.sh`；
- live smoke 验证真实 Docker Run 完成并采集 Evidence 后，可通过 HTTP 查询 Run summary。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/check_phase1_docs.sh
cd simulator/compose && sh scripts/check-compose-config.sh
bash scripts/smoke-sim-api-run-get.sh
```

结果：

```text
Ran 55 tests
OK
Phase 0 planning document skeleton is present.
compose config ok
api run get smoke run_f260daf223ec4d19b8fc03b3a9cf8f6e job=30 state=SUCCEEDED collection=succeeded
url=http://127.0.0.1:44873/api/v1/runs/run_f260daf223ec4d19b8fc03b3a9cf8f6e
```

### 下一步

1. 实现 `POST /api/v1/runs/{run_id}/cancel`；
2. live smoke 验证 HTTP 取消长作业；
3. 之后补 wrapper/environment/outputs。

## 2026-07-10 第十六批

### 已完成

- `Pilot107HttpApi` 新增 `POST /api/v1/runs/{run_id}/cancel`；
- API 层可注入 `RunService`，无 RunService 的只读模式返回 `run_service_unavailable`；
- `BaseHTTPRequestHandler` 新增 `do_POST` JSON 响应路径；
- 新增 HTTP cancel 单元测试，覆盖成功取消、missing run 和只读 API；
- 新增 `scripts/smoke-sim-api-cancel.sh`；
- live smoke 验证真实 Docker Slurm 长作业可通过 HTTP POST 取消，并写回 `CANCELLED`。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/smoke-sim-api-cancel.sh
```

结果：

```text
Ran 58 tests
OK
api cancel smoke run_fea1ec8a87604f4a89b746537ab3070c job=33 state=CANCELLED
url=http://127.0.0.1:40749/api/v1/runs/run_fea1ec8a87604f4a89b746537ab3070c/cancel
```

### 下一步

1. 补 `execution_wrapper.generated`，形成 user/submitted/wrapper 三层脚本证据；
2. 补 environment summary 和 outputs inventory；
3. 之后进入最小 Capsule manifest verify/export。

## 2026-07-10 第十七批

### 已完成

- `RunStore.apply_snapshot()` 在终态后新增 `environment_finalize` 和 `outputs_inventory` 采集任务；
- `DockerSlurmEvidenceCollector` 新增 `submission/execution_wrapper.generated.sh`；
- 新增 `environment/summary.json`，采集 simulator 用户态 `hostname/id/env` 摘要，并过滤非白名单环境变量；
- 新增 `outputs/inventory.json`，对授权 workdir 做有限深度输出清单，排除 Slurm 日志和 `pilot107-submit.sbatch`；
- 更新 Evidence smoke、Evidence query smoke、API evidence smoke、失败/取消 transition smoke，均要求新证据存在；
- 修正 `smoke-sim-api-evidence.sh` 的 API 构造参数，显式传入 `store`。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/smoke-sim-evidence.sh
bash scripts/smoke-sim-evidence-query.sh
bash scripts/smoke-sim-api-evidence.sh
bash scripts/smoke-sim-evidence-transitions.sh
```

结果：

```text
Ran 58 tests
OK
evidence smoke ... collection=succeeded artifacts=10
tasks=environment_finalize:succeeded,logs_finalize:succeeded,outputs_inventory:succeeded,runtime_status:succeeded,submission_snapshot:succeeded,terminal_accounting:succeeded
evidence query smoke ... collection=succeeded files=11
api evidence smoke ... collection=succeeded files=11
evidence transition smoke failed=...:FAILED:42:0 cancelled=...:CANCELLED:None
```

### 下一步

1. 实现最小 Capsule manifest verify/export；
2. 为 Capsule smoke 验证 manifest sha256、文件存在性和跨 Run 边界；
3. 然后回到最小 submit/prepare HTTP API。

## 2026-07-11 第十八批

### 已完成

- 新增 `pilot107.worker.capsule`；
- 新增 `RawCapsuleService.build_raw_capsule()`，从 Evidence manifest 构建 `data/phase0/capsules/runs/<run_id>/raw/`；
- Raw Capsule 写入根级 `manifest.json`、`provenance.json`、`collection_policy.json` 和 `checksums.txt`；
- 新增 `verify_raw_capsule()`，校验 manifest schema、logical path 安全性、manifest 文件存在性、manifest sha256 和 checksums；
- `RunStore` 新增 `update_capsule_state()`，Capsule 构建成功后 Run `capsule_state=ready`，失败时为 `failed`；
- 新增 Capsule 单元测试，覆盖成功构建、篡改检测、path traversal 拒绝；
- 新增 `scripts/smoke-sim-capsule.sh`，在 Docker Slurm 成功 Run 完整 Evidence 后构建并验证 Raw Capsule。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/smoke-sim-capsule.sh
```

结果：

```text
Ran 61 tests
OK
capsule smoke run_1b2a312c1b6e444982fac70c87f6630c capsule=capsule_ec3242d1f4974fd9bda0e5ba6c0a3775 files=10 checked=13 state=ready
capsule_dir=/home/knowingthesea/107pilot/data/phase0/capsules/runs/run_1b2a312c1b6e444982fac70c87f6630c/raw
```

### 下一步

1. 回到最小 Run submit/prepare HTTP API；
2. 先实现模拟环境受控 submit，不直接暴露任意 shell；
3. 随后补 Worker lease 或 EvidenceObject 索引。

## 2026-07-11 第十九批

### 已完成

- `runs` 表新增向后兼容字段 `resource_plan_json`；
- `RunService` 新增 `prepare()` 和 `submit_prepared()`，保留原 `submit()` 直接提交入口；
- `Pilot107HttpApi` 新增 `POST /api/v1/runs/prepare`；
- `Pilot107HttpApi` 新增 `POST /api/v1/runs/{run_id}/submit`；
- prepare 阶段返回 `script_artifacts`、`preview.execution_wrapper`、`preflight` 和 `risk_lint`；
- submit 阶段使用持久化 `resource_plan_json`，不要求 API 再传脚本或 shell；
- 新增 HTTP prepare/submit 单元测试；
- 新增 `scripts/smoke-sim-api-submit.sh`，通过真实 HTTP prepare/submit 提交 Docker Slurm 作业，并由 Worker 对账采集到 `SUCCEEDED`。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/smoke-sim-api-submit.sh
```

结果：

```text
Ran 65 tests
OK
api submit smoke run_59cb1d0ba3fb48c991d5ddc79357b197 job=44 state=SUCCEEDED collection=succeeded
url=http://127.0.0.1:39905/api/v1/runs/run_59cb1d0ba3fb48c991d5ddc79357b197
```

### 下一步

1. 补 Worker collection task 原子 acquire/lease；
2. 避免多 Worker 或重启竞争时重复执行采集任务；
3. 随后补 EvidenceObject 索引或正式 API 鉴权。

## 2026-07-11 第二十批

### 已完成

- `RunStore` 新增 `acquire_due_collection_tasks()`；
- collection task acquire 使用 SQLite `BEGIN IMMEDIATE`，原子抢占 `pending/failed_retryable` 或 lease 已过期的 `running` task；
- acquire 会写入 `lease_owner`、`lease_expires_at`，并记录 `collection.task_acquired` 事件；
- `RuntimeReconcileWorker` 改为只处理自己 acquire 到的 task；
- task succeeded/failed 支持校验 `lease_owner`，防止旧 Worker 在 lease 过期后覆盖新 Worker 接管的 task；
- 新增单元测试覆盖 lease acquire、二次 acquire、过期 lease 回收、错误 owner 提交结果被拒绝；
- Docker Evidence smoke 复验 Worker 采集链路仍正常。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/smoke-sim-evidence.sh
```

结果：

```text
Ran 68 tests
OK
evidence smoke run_d371fc80e5274fd5ad998b24b40c6a50 job=46 state=SUCCEEDED collection=succeeded artifacts=10
tasks=environment_finalize:succeeded,logs_finalize:succeeded,outputs_inventory:succeeded,runtime_status:succeeded,submission_snapshot:succeeded,terminal_accounting:succeeded
```

### 下一步

1. 补最小 EvidenceObject/manifest index；
2. 让查询和诊断可按 category/logical_path/sha256 检索；
3. 随后补正式 API 鉴权或 Worker service packaging。

## 2026-07-11 第二十一批

### 已完成

- 按设计文档树做当前项目总体复核；
- 运行核心测试、Compose 配置、Slurm core、权限隔离、Worker transition、Capsule 和 API submit smoke；
- 复核时发现并发提交竞态：`DockerSimulatorCommandBackend` 和 `CommandSubmitBackend` 都把脚本固定写为 `pilot107-submit.sbatch`，同一 workdir 下并发提交会互相覆盖；
- 修复提交脚本 staging：脚本文件名改为 `pilot107-submit-<idempotency_key>.sbatch`，无 key 时使用脚本 hash；
- `outputs_inventory` 排除规则同步改为 `pilot107-submit-*.sbatch`；
- 新增单元测试覆盖同一 workdir 下不同 `idempotency_key` 产生不同 staging 脚本；
- 并发复验 `smoke-sim-api-submit.sh` 与 `smoke-sim-worker-transitions.sh`，API submit 不再被失败脚本污染。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/check_phase1_docs.sh
cd simulator/compose && sh scripts/check-compose-config.sh
bash scripts/check-sim-core.sh
bash scripts/smoke-sim-evidence-permissions.sh
bash scripts/smoke-sim-worker-transitions.sh
bash scripts/smoke-sim-capsule.sh
bash scripts/smoke-sim-api-submit.sh
```

结果：

```text
Ran 69 tests
OK
compose config ok
Slurmctld(primary) at slurmctld is UP
evidence permission smoke ... cross-run query isolated
worker transition smoke ... FAILED/CANCELLED/SUCCEEDED
capsule smoke ... files=10 checked=13 state=ready
api submit smoke ... state=SUCCEEDED collection=succeeded
```

### 下一步

1. 补最小 EvidenceObject/manifest index；
2. 补 derived result_summary；
3. 补正式 API 鉴权或 Worker service packaging。

## 2026-07-11 第二十二批

### 已完成

- `RunStore` 新增 `evidence_objects` 表；
- 新增 `upsert_evidence_objects()` 和 `list_evidence_objects()`；
- `DockerSlurmEvidenceCollector` 可接收 `run_store`，每次写 manifest 时从 artifacts 同步 upsert EvidenceObject；
- EvidenceObject 记录 category、logical_path、store_path、source_uri、sha256、size、mime_type、collection_status、mutable_during_run 和 finalized_at；
- `EvidenceQueryService.get_evidence_tree()` 新增 `objects` 返回；
- live smoke 中所有 Worker collector 均传入 `run_store`；
- Evidence smoke、Evidence query smoke、API evidence smoke 均验证 objects 索引存在。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/smoke-sim-evidence.sh
bash scripts/smoke-sim-evidence-query.sh
bash scripts/smoke-sim-api-evidence.sh
```

结果：

```text
Ran 70 tests
OK
evidence smoke ... artifacts=10 objects=10
evidence query smoke ... files=11 objects=10
api evidence smoke ... files=11 objects=10
```

### 下一步

1. 补 derived result_summary；
2. 然后补正式 API 鉴权或 Worker service packaging。

## 2026-07-11 第二十三批

### 已完成

- 终态 Run 新增 `result_summary` collection task；
- `DockerSlurmEvidenceCollector` 新增 `derived/result_summary.v1.json`；
- result summary 汇总 Run 状态、exit code、Slurm accounting、job detail、stdout/stderr 日志状态、environment scope、outputs inventory 和 EvidenceObject 引用；
- `result_summary` 依赖 `slurm/logs/environment/outputs` 前置证据，缺失时 retry；
- `evidence_objects` 自动索引 `derived/result_summary.v1.json`；
- Evidence、Evidence query、API evidence、Capsule smoke 均要求 derived summary 存在。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/smoke-sim-evidence.sh
bash scripts/smoke-sim-evidence-query.sh
bash scripts/smoke-sim-api-evidence.sh
bash scripts/smoke-sim-capsule.sh
```

结果：

```text
Ran 70 tests
OK
evidence smoke ... artifacts=11 objects=11
evidence query smoke ... files=12 objects=11
api evidence smoke ... files=12 objects=11
capsule smoke ... files=11 checked=14 state=ready
```

### 下一步

1. 补正式 API 鉴权；
2. 或补 Worker service packaging，让 API/Worker 更接近 M1 部署形态。

## 2026-07-11 第二十四批

### 已完成

- `Pilot107HttpApi` 新增 trusted-header 身份解析，默认 header 为 `X-Pilot107-User`；
- API 可配置 `auth_required`，缺失身份返回 `AUTH.MISSING`，非法身份返回 `AUTH.FORBIDDEN`；
- `POST /api/v1/runs/prepare` 在鉴权模式下以可信身份作为 owner，拒绝 body owner 与可信身份不一致；
- `GET /api/v1/runs/{run_id}`、`GET /api/v1/runs/{run_id}/evidence`、`POST /api/v1/runs/{run_id}/cancel` 均增加 Run owner 边界检查；
- `dev_server` 和 `scripts/serve-api.sh` 支持 `--auth-required`、`--trusted-user-header` 与对应环境变量；
- API submit smoke 改为在 trusted-header 鉴权模式下完成 prepare/submit/get 全链路。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/smoke-sim-api-submit.sh
```

结果：

```text
Ran 77 tests
OK
api submit smoke ... state=SUCCEEDED collection=succeeded
```

### 下一步

1. 补 Worker service packaging：常驻 worker entrypoint、配置项、健康检查和 Compose 接入；
2. 然后复核 M1 部署边界：reverse proxy HTTPS 终止、API/Worker 私网通信、非 root 应用容器。

## 2026-07-11 第二十五批

### 已完成

- 新增 `pilot107.worker.service`，提供 Worker 常驻服务入口；
- Worker service 支持 `--once`、`--until-idle`、`--max-ticks`，并处理 SIGTERM/SIGINT；
- Worker service 配置统一从环境变量读取，包括 DB、Evidence root、backend、allowed roots、worker id、batch、interval、task lease、health path；
- 支持 `docker-compose-command`、`rest-native` 和 `in-memory` 三类 Worker backend 配置；
- 新增健康文件输出，记录最近一次 tick 的 checked、terminal、tasks、errors 和 task_errors；
- 新增 `scripts/serve-worker.sh`，用于本地启动常驻 Worker；
- 新增 `scripts/smoke-sim-worker-service.sh`，验证 service packaging 下的 Docker simulator 对账、Evidence 采集和健康文件；
- `simulator/compose/compose.yml` 中 `pilot107-worker` 新增 command、DB/Evidence/health env 和 healthcheck。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
sh simulator/compose/scripts/check-compose-config.sh
bash scripts/smoke-sim-worker-service.sh
```

结果：

```text
Ran 80 tests
OK
compose config ok
worker service smoke ... state=SUCCEEDED collection=succeeded
```

### 下一步

1. 补 API/Worker 镜像构建文件，让 `profiles: ["apps"]` 可以实际拉起本地应用容器；
2. 然后补最小 Contract/Recipe API，开始把 Run submit 从裸脚本请求推进到设计树里的 Recipe/Contract 入口。

## 2026-07-11 第二十六批

### 已完成

- 新增 `apps/Dockerfile`，基于 `python:3.12-slim` 构建非 root 应用运行时；
- 新增 `.dockerignore`，避免把 `data/`、`artifacts/` 和缓存目录打入应用镜像；
- 新增 `scripts/build-app-images.sh`，一次构建 `pilot107/api:local` 和 `pilot107/worker:local`；
- 新增 `scripts/check-app-images.sh`，验证镜像内 `pilot107` API/Worker 模块可导入；
- `pilot107-api` Compose 服务新增明确 command、trusted-header auth、`/healthz` healthcheck；
- 新增 `scripts/smoke-sim-apps-profile.sh`，启动 `pilot107-api` 和 `pilot107-worker`，等待二者 healthcheck，并从容器内访问 API `/healthz`。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
sh simulator/compose/scripts/check-compose-config.sh
bash scripts/build-app-images.sh
bash scripts/check-app-images.sh
bash scripts/smoke-sim-apps-profile.sh
```

结果：

```text
Ran 80 tests
OK
compose config ok
pilot107 app images ok
apps profile smoke ok
```

### 下一步

1. 补最小 Contract/Recipe API；
2. 同步补 API service builder，使容器内 API 后续可配置 REST/command backend，而不是只做 health/read-only 启动。

## 2026-07-11 第二十七批

### 已完成

- 新增 `pilot107.api.service`，负责从配置构建 `Pilot107HttpApi`、`RunStore`、`EvidenceQueryService` 和可选 `RunService`；
- API service builder 支持 `none`、`in-memory`、`rest-native`、`command`、`docker-compose-command` backend；
- `dev_server` 支持 `--backend`、`--allowed-roots`、`--command-timeout-seconds`、`--slurmrestd-url`、`--slurm-api-version`、`--slurm-token`；
- `scripts/serve-api.sh` 支持 `PILOT107_API_BACKEND` 和 `PILOT107_ALLOWED_ROOTS`；
- `pilot107-api` Compose 服务新增 `PILOT107_API_BACKEND`、DB/Evidence/auth 环境变量；
- 新增 `tests/test_api_service.py`，覆盖 API service 配置、只读 backend 和 in-memory backend 提交；
- 新增 `scripts/smoke-sim-api-container-submit.sh`，验证 API 容器在 `PILOT107_API_BACKEND=in-memory` 下可完成 trusted-header prepare/submit。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
sh simulator/compose/scripts/check-compose-config.sh
bash scripts/build-app-images.sh
bash scripts/check-app-images.sh
bash scripts/smoke-sim-apps-profile.sh
bash scripts/smoke-sim-api-container-submit.sh
```

结果：

```text
Ran 84 tests
OK
compose config ok
pilot107 app images ok
apps profile smoke ok
api container submit smoke ok
```

### 下一步

1. 补最小 Contract/Recipe API；
2. REST native live submit 继续作为专项处理，不阻塞 Contract/Recipe 主线。

## 2026-07-11 第二十八批

### 已完成

- 新增 `pilot107.core.contracts`；
- 内置 `recipe_python_cpu@1.0.0`，提供 Recipe list/detail payload；
- 新增 `ContractStore`，在同一 SQLite DB 内持久化 contracts；
- 新增 `ContractService`，支持 validate/create/get/preflight/to_submit_request；
- Contract validate 归一化 ResourcePlan，返回 effective_request、findings、risk_lint 和静态 configuration snapshot；
- Contract 渲染生成最小 submitted script，保留 recipe_version_id 和 workdir；
- HTTP API 新增：
  - `GET /api/v1/recipes`
  - `GET /api/v1/recipes/{recipe_id}/versions/{version}`
  - `POST /api/v1/contracts/validate`
  - `POST /api/v1/contracts`
  - `GET /api/v1/contracts/{contract_id}`
  - `POST /api/v1/contracts/{contract_id}/preflight`
  - `POST /api/v1/runs/prepare` 支持 `{ "contract_id": "..." }`
- Contract owner 继承 trusted-header 身份，跨用户 Contract 读取会被拒绝；
- 新增 `tests/test_contracts.py` 和 Contract HTTP 单元测试；
- 新增 `scripts/smoke-sim-api-container-contract.sh`，验证 API 容器内 Recipe/Contract/Run submit 纵切。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
sh simulator/compose/scripts/check-compose-config.sh
bash scripts/build-app-images.sh
bash scripts/check-app-images.sh
bash scripts/smoke-sim-apps-profile.sh
bash scripts/smoke-sim-api-container-contract.sh
```

结果：

```text
Ran 95 tests
OK
compose config ok
pilot107 app images ok
apps profile smoke ok
api container contract smoke ok
```

### 下一步

1. 回到 REST 专项，收敛 simulator `slurmrestd` auth/probe/live submit；
2. 或补 M1 部署脚本与 HTTPS reverse proxy 配置，使当前已闭合的 Docker 主线更接近比赛部署。

## 2026-07-11 第二十九批

### 已完成

- 进入 REST auth/live submit 专项；
- `UrllibHttpTransport` 新增 `RestAuthStyle`，支持：
  - `bearer`：`Authorization: Bearer <token>`
  - `slurm_headers`：`X-SLURM-USER-NAME` + `X-SLURM-USER-TOKEN`
- API service 和 Worker service 均支持：
  - `PILOT107_REST_AUTH_STYLE`
  - `PILOT107_SLURM_USER_NAME`
- simulator 添加开发用 JWT key 挂载，并将 `slurmrestd` 切到 `rest_auth/jwt`；
- 验证 Ubuntu Slurm 23.11 镜像只有 `rest_auth_jwt.so`，没有控制面 `auth_jwt.so`；
- 验证 `scontrol token` 当前不可用，报 `Required plugin type not loaded or initialized`；
- 新增 `scripts/probe-sim-rest-auth.sh`，生成 REST auth matrix 到 `artifacts/probes/sim_rest_auth.json`；
- 当前 probe 结果为 blocked：no-token、Bearer dev token、Slurm header dev token 均返回 401 `Authentication failure`。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
sh simulator/compose/scripts/check-compose-config.sh
bash scripts/check-sim-core.sh
bash scripts/probe-sim-rest-auth.sh
```

结果：

```text
Ran 98 tests
OK
compose config ok
worker-[1-2] idle
sim rest auth probe blocked
```

### 结论

当前 simulator REST live submit 不是应用适配层问题，而是基础 Slurm 包能力问题：`rest_auth/jwt` 插件存在，但控制面 JWT auth 插件缺失，导致无法生成/验证 Slurm JWT。比赛 Docker 主线继续使用 command backend；REST live submit 需要后续换 Slurm 镜像、源码构建 JWT 插件，或在真实 107 上做 Bearer probe。

### 下一步

1. 补 M1 部署脚本与 HTTPS reverse proxy 配置；
2. 或继续 REST 专项的下一层：尝试替换/构建包含 `auth_jwt.so` 的 Slurm simulator 镜像。

## 2026-07-11 第三十批

### 已完成

- 继续 REST 专项；
- 复核容器包能力：`slurm-wlm-basic-plugins` 仅提供 `auth_munge.so`、`auth_none.so`、`auth_slurm.so`、`rest_auth_jwt.so`、`rest_auth_local.so`，没有 `auth_jwt.so`；
- 复核 apt 包列表：当前镜像可见 Slurm 包没有独立 JWT/auth 插件包可直接安装；
- 新增 `scripts/probe-sim-rest-submit.sh`；
- REST submit probe 会先刷新 `sim_rest_auth.json`，若没有 supported auth 策略，则生成 `artifacts/probes/sim_rest_submit.json` 并标记 `skipped`；
- 为未来可用 JWT/Bearer 场景预留 `/slurm/v0.0.41/job/submit` live submit 分支。
- 加固 command backend accounting 解析：`sacct` owner 为空时改为可重试 transport error，避免把短暂落库不完整误判为越权。
- 加固 `smoke-sim-backend-job` 轮询：刚提交的 job 在 accounting 过渡窗口出现 backend transient error 时继续重试；Worker 主路径已具备同类重试行为。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/probe-sim-rest-submit.sh
bash scripts/smoke-sim-backend-job.sh
```

预期当前结果：

```text
Ran 99 tests
OK
sim rest submit probe skipped
backend smoke job <id> alice SUCCEEDED 0:0
```

### 结论

REST submit 的阶段状态从“未验证”收敛为“有 matrix，但当前 simulator 因 auth blocked 跳过 submit”。这不改变比赛主线：Phase 0A/M1 演示继续以 command backend 作为 Docker Slurm 可控提交路径，REST 保持兼容性专项。

## 2026-07-11 第三十一批

### 已完成

- 回顾设计文档树和 Phase 文档，确认当前仍处于 Phase 0A 收口后、Phase 0B/M1 部署前；
- 复验代表性链路：核心单元测试、Compose 配置、Docker Slurm 核心、REST submit matrix、API submit、apps profile、Raw Capsule；
- 修复 `smoke-sim-evidence-permissions` 验证脚本漂移：
  - 补齐 collector 所需的临时 `RunStore`；
  - 采集前先登记 Run，满足 `evidence_objects.run_id` 外键；
  - 复验 Alice/Bob/symlink/cross-run 隔离通过；
- 同步 `implementation_plan.md` 中 Phase 0A 阶段门勾选状态。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
sh simulator/compose/scripts/check-compose-config.sh
bash scripts/check-sim-core.sh
bash scripts/probe-sim-rest-submit.sh
bash scripts/smoke-sim-api-submit.sh
bash scripts/smoke-sim-apps-profile.sh
bash scripts/smoke-sim-capsule.sh
bash scripts/smoke-sim-evidence-permissions.sh
```

结果摘要：

```text
Ran 99 tests
OK
compose config ok
Slurmctld(primary) at slurmctld is UP
sim rest submit probe skipped
api submit smoke ... state=SUCCEEDED collection=succeeded
apps profile smoke ok
capsule smoke ... state=ready
evidence permission smoke ... cross-run query isolated
```

### 结论

Phase 0A 工程闭环已达到当前阶段门；下一步应转入 Phase 0B/M1 演示交付补强，优先补 Web 演示界面、HTTPS/reverse proxy、两机部署脚本和可重复演示剧本。REST live submit 保持并行兼容性任务。

## 2026-07-11 第三十二批

### 已完成

- 开始 Phase 0B/M1 演示补强的第一项：Web MVP；
- 新增 `pilot107.web.server`：
  - 静态页面服务；
  - 同源 `/api/*` 代理；
  - trusted-header 开发身份转发；
  - `/healthz` 和 `HEAD /`；
- 新增 Web 静态 UI：
  - Recipe/Contract 输入；
  - Contract validate；
  - Contract create + Run prepare + submit；
  - Run 状态刷新和取消；
  - Evidence tasks/objects 读取入口；
- 新增 `scripts/serve-web.sh`；
- 新增 `scripts/smoke-sim-web-mvp.sh`，覆盖 Web 同源代理到 API 的 Recipe/Contract/Run 纵切；
- `apps/Dockerfile` 构建产物扩展为 `pilot107/api:local`、`pilot107/worker:local`、`pilot107/web:local`；
- Compose `pilot107-web` 增加启动命令、`PILOT107_WEB_API_BASE_URL`、端口和 healthcheck；
- `smoke-sim-apps-profile.sh` 扩展为 API/Worker/Web 三容器 healthcheck，并验证 Web 代理 `/api/v1/recipes`。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/smoke-sim-web-mvp.sh
sh simulator/compose/scripts/check-compose-config.sh
bash scripts/build-app-images.sh
bash scripts/check-app-images.sh
bash scripts/smoke-sim-apps-profile.sh
curl -sS http://127.0.0.1:3000/healthz
curl -I http://127.0.0.1:3000/
curl -sS http://127.0.0.1:3000/api/v1/recipes
```

结果摘要：

```text
Ran 103 tests
OK
web mvp smoke ... job=1000
compose config ok
pilot107 app images ok
apps profile smoke ok
{"status":"ok"}
HTTP/1.0 200 OK
recipe_python_cpu
```

### 当前限制

当前 Compose apps profile 为了 Web 容器纵切验证使用 `PILOT107_API_BACKEND=in-memory`。真实 Docker Slurm live submit 仍通过 host 侧 `docker-compose-command` smoke 验证；容器化 M1 主路径需要在 Phase 0B 明确应用节点到 Docker 宿主机的提交/证据传输方式，或继续推进 REST auth 能力。

## 2026-07-11 第三十三批

### 已完成

- 修正 Web 实际交互无法完成的问题；
- 新增 `DemoSlurmBackend`：
  - API 提交返回稳定 `demo-*` job_id；
  - Worker 在独立进程中可对账同一 job_id 到 `SUCCEEDED 0:0`；
  - 支持 cancel 语义；
- 新增 `DemoEvidenceCollector`：
  - 生成 submission、slurm、logs、environment、outputs、manifest、derived summary；
  - 写入 `evidence_objects`，保证 Evidence API 有 tasks 和 objects；
- API/Worker service 均支持 `PILOT107_*_BACKEND=demo`；
- Compose 默认 API/Worker backend 改为 `demo`，避免前端默认撞上 REST auth blocked 或 in-memory 跨进程不可见；
- 新增 `scripts/smoke-sim-web-interactions.sh`，通过 Web 同源代理完成：
  - Recipe 查询；
  - Contract validate/create；
  - Run prepare/submit；
  - Worker 对账；
  - Evidence 查询。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
bash scripts/build-app-images.sh
bash scripts/check-app-images.sh
bash scripts/smoke-sim-apps-profile.sh
bash scripts/smoke-sim-web-interactions.sh
bash scripts/smoke-sim-backend-job.sh
```

结果摘要：

```text
Ran 107 tests
OK
pilot107 app images ok
apps profile smoke ok
web interactions smoke ... state=SUCCEEDED collection=succeeded objects=12
backend smoke job ... alice SUCCEEDED 0:0
```

### 结论

前端现在可以依赖后端完成所有当前 Web MVP 交互：提交、轮询、取消、Evidence 查询。下一步暂停视觉和布局继续改动，等待前端设计包；设计包到位后只需要替换界面层并对接现有 `/api/v1` 交互契约。

## 2026-07-11 第三十四批

### 已完成

- 按 `107Pilot_Codex_UI_Redesign_Handoff_v1.0` 启动前端重设计 Issue 1；
- 保持 `app.js` 不变，只调整静态 HTML 骨架和 CSS；
- 新增本地设计令牌：
  - `--app-bg`、`--text-primary`、brand 蓝/靛/紫/青；
  - success/warning/danger/info 状态色；
  - 半径、阴影、间距、motion duration/easing；
  - 保留旧变量别名，避免残留样式引用失效；
- 重构页面骨架：
  - 深色 Sidebar；
  - 顶部 Header；
  - Contract、Run、Preflight、Preview、Evidence 卡片结构；
  - 响应式单列/多列网格；
- 统一基础组件样式：
  - Button；
  - Badge/status pill；
  - Input/select/textarea；
  - Panel/card；
  - Toast；
- 未新增动画依赖，未引入 Tailwind 或前端构建链；
- CSS 审计确认未使用 `transition: all`，未加入装饰性粒子或径向光晕。

### 当前校验

```bash
bash scripts/check_phase0_core.sh
sh simulator/compose/scripts/check-compose-config.sh
bash scripts/build-app-images.sh
bash scripts/check-app-images.sh
bash scripts/smoke-sim-web-mvp.sh
bash scripts/smoke-sim-apps-profile.sh
bash scripts/smoke-sim-web-interactions.sh
bash scripts/smoke-sim-api-cancel.sh
bash scripts/smoke-sim-api-submit.sh
bash scripts/smoke-sim-api-evidence.sh
curl -sS http://127.0.0.1:3000/healthz
curl -I http://127.0.0.1:3000/
curl -sS http://127.0.0.1:3000/api/v1/recipes
```

结果摘要：

```text
Ran 107 tests
OK
compose config ok
pilot107 app images ok
web mvp smoke ... job=1000
apps profile smoke ok
web interactions smoke ... state=SUCCEEDED collection=succeeded objects=12
api cancel smoke ... state=CANCELLED
api submit smoke ... state=SUCCEEDED collection=succeeded
api evidence smoke ... collection=succeeded files=12 objects=11
{"status":"ok"}
HTTP/1.0 200 OK
recipe_python_cpu
```

### 结论

Issue 1 已完成设计系统与页面骨架，不改变现有 API、表单字段、提交、刷新、取消、轮询和 Evidence 加载逻辑。下一步进入 Issue 2 时再重构业务面板状态表达，并继续保持 `app.js` 行为契约不退化。

## 2026-07-11 第三十五批

### 已完成

- 完成前端重设计 Issue 2：业务面板状态重构；
- Contract 面板新增状态提示：
  - Ready；
  - Validating；
  - Validated；
  - Failed/Auth required；
- Run 面板新增三阶段进度表达：
  - Prepare；
  - Submit；
  - Evidence；
- Preflight 面板新增 loading、empty、warning、failed、success 状态表达；
- Preview 面板新增 empty/rendered 状态表达；
- Evidence 面板新增 summary：
  - task count；
  - object count；
  - collection state；
- Evidence tasks/objects 列表加入 ok/warn/error/muted 语义样式；
- 保持现有 API、表单字段、提交、刷新、取消、轮询和 Evidence 加载行为不变；
- 新增项目级浏览器操作约束 `AGENTS.md`：
  - 107Pilot 浏览器验证统一使用 `pilot-browser`；
  - 不再直接调用 `agent-browser`；
  - `@eN` ref 必须来自当前 session 的最新 snapshot；
  - 物理点击前必须保证目标在视口内，必要时先 `pilot-browser scrollintoview @ref`。

### 当前校验

```bash
node --check src/pilot107/web/static/assets/app.js
bash scripts/check_phase0_core.sh
sh simulator/compose/scripts/check-compose-config.sh
bash scripts/build-app-images.sh
bash scripts/check-app-images.sh
bash scripts/smoke-sim-web-mvp.sh
bash scripts/smoke-sim-apps-profile.sh
bash scripts/smoke-sim-web-interactions.sh
bash scripts/smoke-sim-api-cancel.sh
bash scripts/smoke-sim-api-submit.sh
bash scripts/smoke-sim-api-evidence.sh
```

结果摘要：

```text
node --check ok
Ran 107 tests
OK
compose config ok
pilot107 app images ok
web mvp smoke ... job=1000
apps profile smoke ok
web interactions smoke ... state=SUCCEEDED collection=succeeded objects=12
api cancel smoke ... state=CANCELLED
api submit smoke ... state=SUCCEEDED collection=succeeded
api evidence smoke ... collection=succeeded files=12 objects=11
```

### 浏览器行为验证

使用 `pilot-browser` 在 `http://127.0.0.1:3000/` 完成真实页面交互：

```text
Validate:
  POST /api/v1/contracts/validate -> 200
  Preflight -> OK
  Preview -> rendered

Create and Submit:
  POST /api/v1/contracts -> 201
  POST /api/v1/runs/prepare -> 201
  POST /api/v1/runs/{run_id}/submit -> 200
  GET /api/v1/runs/{run_id} -> 200
  Run -> SUCCEEDED
  Collection -> succeeded

Load Evidence:
  GET /api/v1/runs/{run_id}/evidence -> 200
  Evidence -> succeeded
  Tasks -> 7
  Objects -> 12
```

### Review 结论

Issue 2 已完成业务面板状态重构，当前行为验证证明页面可以完成 Contract validate、Run submit/poll 和 Evidence load 的完整闭环。未引入新的动画依赖，未改变后端 API 契约。后续 Issue 3 进入动画系统时，应只在现有状态节点上增加过渡和动效，不应改动请求路径、轮询节奏或 Evidence 数据加载逻辑。

## 2026-07-11 第三十六批

### 已完成

- 完成前端重设计 Issue 3：动画系统；
- 未新增动画依赖，继续使用 CSS transition/keyframes；
- 新增页面首次进入动画：
  - shell enter；
  - topbar enter；
  - panel enter；
  - 首屏总时长控制在 500ms 内；
- 新增导航 active indicator：
  - `.nav-list a::after`；
  - `hashchange` 时同步 active 状态；
- 完善按钮 hover/press：
  - hover 轻微上移；
  - press 下压；
  - 不使用 `transition: all`；
- 新增 RUNNING/处理中状态的小点呼吸：
  - `.status-pill.run::before`；
  - `.stage-item.run span`；
  - `.state-banner.run .state-icon`；
  - 只让点状元素动，不让整个 Badge 闪烁；
- 新增 Preflight checking 进度扫光：
  - `.state-banner.run::after`；
  - 不触发额外 API 请求；
- 新增 Evidence/list item 插入动画：
  - `.list-item` 使用 `item-insert`；
  - Evidence tasks/objects 加载后逐项进入；
- 调整 Toast 动效：
  - opacity + translateY；
  - 不改变现有 toast 触发逻辑；
- 完善 `prefers-reduced-motion: reduce`：
  - motion token 降为 1ms；
  - animation delay 清零；
  - animation iteration 限制为 1；
  - 禁用位移类 transform；
  - 禁用运行态扫光。

### 当前校验

```bash
node --check src/pilot107/web/static/assets/app.js
bash scripts/check_phase0_core.sh
sh simulator/compose/scripts/check-compose-config.sh
bash scripts/check-app-images.sh
bash scripts/smoke-sim-web-mvp.sh
bash scripts/smoke-sim-apps-profile.sh
bash scripts/smoke-sim-web-interactions.sh
bash scripts/smoke-sim-api-cancel.sh
bash scripts/smoke-sim-api-submit.sh
bash scripts/smoke-sim-api-evidence.sh
bash scripts/build-app-images.sh
bash scripts/check-app-images.sh
```

结果摘要：

```text
node --check ok
Ran 107 tests
OK
compose config ok
pilot107 app images ok
web mvp smoke ... job=1000
apps profile smoke ok
web interactions smoke ... state=SUCCEEDED collection=succeeded objects=12
api cancel smoke ... state=CANCELLED
api submit smoke ... state=SUCCEEDED collection=succeeded
api evidence smoke ... collection=succeeded files=12 objects=11
```

### 浏览器行为验证

使用 `pilot-browser` 验证：

```text
page-enter keyframes loaded: yes
panel-enter applied: yes
nav active indicator:
  #submit -> inactive after Evidence click
  #evidence -> active
  ::after opacity -> 1
prefers-reduced-motion:
  --motion-fast -> 1ms
  shell animation duration -> 0.001s
  shell animation delay -> 0s
  toast transform -> none
RUNNING/preflight animation rules:
  status-pulse keyframes loaded
  progress-sweep keyframes loaded
  state-banner.run::after configured
Evidence load:
  GET /api/v1/runs/{run_id}/evidence -> 200
  task items -> 7
  object items -> 12
  first object animation -> item-insert
```

真实业务闭环仍通过：

```text
Validate:
  POST /api/v1/contracts/validate -> 200
  Preflight -> OK
  Preview -> rendered

Create and Submit:
  POST /api/v1/contracts -> 201
  POST /api/v1/runs/prepare -> 201
  POST /api/v1/runs/{run_id}/submit -> 200
  GET /api/v1/runs/{run_id} -> 200
  Run -> SUCCEEDED
  Collection -> succeeded

Load Evidence:
  GET /api/v1/runs/{run_id}/evidence -> 200
  Evidence -> succeeded
  Tasks -> 7
  Objects -> 12
```

### Review 结论

Issue 3 已完成动画系统，且未改变 API、表单、提交、刷新、取消、轮询和 Evidence 加载行为。注意：会重建同一 Compose project 的 smoke 脚本不要并行运行，否则 Docker 可能出现临时容器名冲突；已通过串行重跑验证不是代码问题。下一步进入 Issue 4：测试与视觉回归，生成指定截图并运行类型检查、构建、单测和 Playwright/浏览器回归。

## 2026-07-11 第三十七批

### 已完成

- 完成前端重设计 Issue 4：测试与视觉回归；
- 新增最小 Playwright 测试基础设施：
  - `package.json`；
  - `package-lock.json`；
  - `playwright.config.cjs`；
  - `tests/ui/visual.spec.js`；
- Playwright 使用本地静态服务加载 `src/pilot107/web/static`；
- Playwright route mock `/healthz` 和 `/api/v1/*`，用于稳定生成视觉状态；
- 使用本机已有 Chrome，并通过 launch args 固定：
  - `--no-sandbox`；
  - `--disable-dev-shm-usage`；
- 新增 `.gitignore`：
  - 忽略 `node_modules/`；
  - 忽略 `artifacts/playwright-output/`；
  - 保留 `artifacts/visual-regression/*.png` 作为指定截图产物；
- 修复 `mypy` 暴露的 5 个类型问题：
  - manifest `collected_at` 显式保持为 `str`；
  - JSON 读取结果增加 dict 类型守卫；
  - worker backend/task handler 增加返回类型；
  - HTTP handler headers 转为普通 `dict`。

### 生成截图

```text
artifacts/visual-regression/submit-idle.png
artifacts/visual-regression/submit-preflight-checking.png
artifacts/visual-regression/submit-preflight-warning.png
artifacts/visual-regression/submit-running.png
artifacts/visual-regression/submit-failed.png
artifacts/visual-regression/submit-succeeded-evidence.png
artifacts/visual-regression/submit-mobile.png
```

### 当前校验

```bash
npm run check:js
npm run screenshots
npm run test:ui
uv run --extra dev mypy src/pilot107
bash scripts/check_phase0_core.sh
sh simulator/compose/scripts/check-compose-config.sh
bash scripts/build-app-images.sh
bash scripts/check-app-images.sh
bash scripts/smoke-sim-web-mvp.sh
bash scripts/smoke-sim-apps-profile.sh
bash scripts/smoke-sim-web-interactions.sh
bash scripts/smoke-sim-api-cancel.sh
bash scripts/smoke-sim-api-submit.sh
bash scripts/smoke-sim-api-evidence.sh
```

结果摘要：

```text
npm run check:js -> ok
npm run screenshots -> 7 passed
npm run test:ui -> 7 passed
mypy -> Success: no issues found in 23 source files
Ran 107 tests
OK
compose config ok
pilot107 app images ok
web mvp smoke ... job=1000
apps profile smoke ok
web interactions smoke ... state=SUCCEEDED collection=succeeded objects=12
api cancel smoke ... state=CANCELLED
api submit smoke ... state=SUCCEEDED collection=succeeded
api evidence smoke ... collection=succeeded files=12 objects=11
```

### Review 结论

Issue 4 已完成指定截图生成和回归验证。Playwright 覆盖 idle、preflight checking、preflight warning、running、failed、succeeded evidence 和 mobile 七个视觉状态；后端 smoke 继续证明真实 API、提交、取消、轮询和 Evidence 行为未退化。`ruff check .` 仍会报告大量既有 import/order/line-length 问题，本轮未做全仓格式化，以避免引入无关改动。

## 2026-07-11 第三十八批

### 已完成

- 新增 competition 部署 profile：
  - `simulator/compose/compose.competition.yml`；
  - `simulator/compose/.env.competition.example`；
  - `simulator/compose/nginx/competition.conf` 作为生产 nginx 参考配置；
- 新增受控 `pilot107-command-gateway`：
  - 运行在 Slurm simulator 镜像内；
  - 使用 JSON HTTP API；
  - 不执行 shell；
  - 命令首项白名单；
  - 文件和 cwd 限制在 `PILOT107_ALLOWED_ROOTS`；
  - 支持 bearer token；
- `pilot107-api` 和 `pilot107-worker` 支持 `command-gateway` backend；
- `pilot107-api` 新增 Capsule API：
  - `POST /api/v1/runs/{run_id}/capsule`；
  - 复用 `RawCapsuleService`；
- Web 新增 Build Capsule 按钮；
- 新增本地 HTTPS reverse proxy：
  - `pilot107.web.reverse_proxy`；
  - 使用 `pilot107/web:local` 镜像，避免比赛启动依赖公网拉 nginx 镜像；
  - 8080 重定向到 8443；
  - 8443 终止 TLS；
- 新增一键脚本：
  - `scripts/start-competition.sh`；
  - `scripts/check-competition.sh`；
  - `scripts/stop-competition.sh`；
  - `scripts/smoke_competition_web.py`；
- Slurm simulator 镜像增加 `python3`，用于 command gateway。

### 当前校验

```bash
uv run --extra dev mypy src/pilot107
bash scripts/check_phase0_core.sh
npm run check:js
bash scripts/build-slurm-sim-image.sh
bash scripts/build-app-images.sh
bash scripts/check-app-images.sh
docker run --rm pilot107/slurm-sim:local python3 --version
docker compose --env-file simulator/compose/.env.competition.example -f simulator/compose/compose.yml -f simulator/compose/compose.competition.yml --profile competition config
PILOT107_SKIP_BUILD=1 bash scripts/start-competition.sh
bash scripts/check-competition.sh
curl -k -sS https://127.0.0.1:8443/healthz
curl -k -sS https://127.0.0.1:8443/
```

结果摘要：

```text
mypy -> Success: no issues found in 24 source files
unit -> Ran 107 tests, OK
js syntax -> ok
app images -> ok
slurm-sim python -> Python 3.12.3
competition compose config -> ok
start-competition -> competition profile is running: https://127.0.0.1:8443/
HTTPS health -> {"status":"ok"}
HTTPS page -> returned 107Pilot HTML
competition web smoke ok
  success job=105
  failure job=106
  cancelled job=107
  all submit_strategy=command
  all job_id != demo-*
  all evidence succeeded
  all capsule ready
```

### Review 结论

Phase 0B 本地 competition profile 已形成第一版可验收闭环：Web/API/Worker 通过受控 command gateway 连接 Docker Slurm backend，而不是 demo backend；HTTPS 入口、一键启动、成功/失败/取消、Evidence 和 Capsule 均已通过本地 smoke。尚未完成的是学校实际应用节点与 Docker 宿主机的两机部署验证、正式 TLS 证书替换、校园网防火墙策略验证和真实 107 的 M1-R 兼容探测。

## 2026-07-12 第三十九批

### 已完成

- 新增无第三方依赖的 competition profile 压测脚本：
  - `scripts/load_competition.py`；
  - 支持 `read`、`validate`、`prepare`、`workflow` 场景；
- 输出承载力报告：
  - `docs/phase-0/load_capacity_report.md`。

### 当前校验

```bash
PILOT107_SKIP_BUILD=1 bash scripts/start-competition.sh
python3 -m py_compile scripts/load_competition.py
python3 scripts/load_competition.py --concurrency 100 --scenario all
python3 scripts/load_competition.py --concurrency 100 --scenario workflow --workflow-timeout 360
docker stats --no-stream \
  pilot107-sim-pilot107-api-1 \
  pilot107-sim-pilot107-web-1 \
  pilot107-sim-pilot107-worker-1 \
  pilot107-sim-pilot107-command-gateway-1 \
  pilot107-sim-pilot107-reverse-proxy-1
```

结果摘要：

```text
load read concurrency=100 ok=100 errors=0 elapsed=2.482s rps=40.3 p95=2282.6ms
load validate concurrency=100 ok=100 errors=0 elapsed=2.497s rps=40.0 p95=2269.2ms
load prepare concurrency=100 ok=100 errors=0 elapsed=3.179s rps=31.5 p95=1108.7ms
load workflow concurrency=100 ok=100 errors=0 elapsed=164.619s p50=105078.2ms p95=164184.3ms
```

### Review 结论

本地 competition profile 已通过 100 并发轻量请求、100 并发 Contract/Run 写入，以及 100 并发完整 `create → prepare → submit → SUCCEEDED + Evidence collected → Capsule ready` 工作流。当前满足“至少应对 100 并发”的最低口径，但完整工作流尾延迟较高；下一步如要提高体验，应优先做多 Worker、SQLite `busy_timeout`、生产级 HTTP server 和 gateway/worker 指标。

## 2026-07-12 第四十批

### 已完成

- 补齐“使用学校 VM 实测之前”的部署准备件：
  - `scripts/export-competition-bundle.sh`；
  - `scripts/import-competition-images.sh`；
  - `scripts/preflight-competition-vm.sh`；
  - `docs/phase-0/vm_test_readiness.md`；
- 部署包导出脚本默认包含：
  - competition compose；
  - VM 预检/启动/停止/检查脚本；
  - 源码和构建文件；
  - Phase 0 报告；
  - `pilot107/*:local` 离线镜像归档；
  - `SHA256SUMS`；
  - 最终 `.tar.gz` 和 `.sha256`；
- 打包逻辑默认排除本机生成的 `.env.competition` 和 `certs/`，避免把本地私钥混入 VM 部署包。

### 下一步边界

下一步才进入真实 VM 实测：

```text
传输 bundle
→ sha256sum -c SHA256SUMS
→ import images
→ preflight --require-images
→ start competition
→ check competition
→ 100 concurrency smoke
```

### 当前校验

```bash
bash -n scripts/export-competition-bundle.sh scripts/import-competition-images.sh scripts/preflight-competition-vm.sh scripts/start-competition.sh scripts/check-competition.sh scripts/stop-competition.sh
python3 -m py_compile scripts/load_competition.py scripts/smoke_competition_web.py
bash scripts/preflight-competition-vm.sh --require-images
PILOT107_SKIP_BUILD=1 PILOT107_EXPORT_IMAGES=1 bash scripts/export-competition-bundle.sh
sha256sum -c artifacts/deployment/107pilot-competition-bundle-20260711T163304Z.tar.gz.sha256
cd artifacts/deployment/107pilot-competition-bundle-20260711T163304Z && sha256sum -c SHA256SUMS
bash artifacts/deployment/107pilot-competition-bundle-20260711T163304Z/scripts/import-competition-images.sh artifacts/deployment/107pilot-competition-bundle-20260711T163304Z/images/pilot107-images.tar.gz
bash artifacts/deployment/107pilot-competition-bundle-20260711T163304Z/scripts/preflight-competition-vm.sh --require-images
```

结果摘要：

```text
script syntax -> ok
python compile -> ok
local preflight -> ok
bundle archive -> /home/knowingthesea/107pilot/artifacts/deployment/107pilot-competition-bundle-20260711T163304Z.tar.gz
bundle size -> 153M
archive sha256 -> ok
bundle SHA256SUMS -> ok
bundle excludes local .env.competition and certs
bundle image import -> ok
bundle preflight --require-images -> ok
```

## 2026-07-12 第四十一批

### 已完成

- 根据“可先跳过 VM 验证”的决策，转入 Phase 1 接口硬化。
- 新增核心身份模型：
  - `UserIdentity`；
  - `IdentityMode`；
  - `resolve_trusted_header_identity`；
  - `AUTH.MISSING` / `AUTH.FORBIDDEN` 映射。
- HTTP API 改为使用核心身份模型，保持原有 API 响应和 owner 校验行为不变。
- 新增 Slurm 控制面契约测试，覆盖 submit、idempotency、get、cancel 和跨用户拒绝。
- 新增 command gateway 安全测试，覆盖 bearer、命令白名单、结构化 argv、路径输入和越界写入。
- 新增最小平台配置快照：
  - `ClusterProfile`；
  - `UserEntitlementProfile`；
  - `EndpointSet`；
  - `ConfigurationSnapshot`。

### 当前边界

真实 VM 验证仍是 Phase 0B 的未完成项，但当前不阻塞 Phase 1 的接口、权限和 EvidenceTransport 补强。

## 2026-07-12 第四十二批

### 已完成

- 继续 Phase 1 接口硬化，补齐 EvidenceTransport 和采集边界测试。
- 新增 EvidenceTransport 契约与授权文件系统实现：
  - `EvidenceTransport`；
  - `EvidencePolicy`；
  - `EvidenceCapability`；
  - `EvidenceRoot`；
  - `FileStat`；
  - `TextTail`；
  - `OutputInventory`；
  - `AuthorizedFilesystemEvidenceTransport`。
- 新增 EvidenceTransport contract tests，覆盖：
  - capability probe；
  - run root 准备；
  - stat；
  - tail；
  - range read；
  - inventory policy；
  - symlink escape；
  - transport 二次授权。
- 新增架构边界测试，确保业务层不直接调用 `docker exec`。
- 补齐 CollectionTask 聚合语义测试：
  - retryable failure → degraded；
  - permanent failure → failed。

### 当前校验

```bash
uv run --extra dev mypy src/pilot107
PYTHONPATH=src uv run --extra dev pytest tests/test_evidence_transport.py tests/test_architecture_boundaries.py tests/test_run_store.py tests/test_evidence.py
uv run --extra dev ruff check tests/test_evidence_transport.py tests/test_architecture_boundaries.py tests/test_run_store.py
PYTHONPATH=src uv run --extra dev pytest
```

结果摘要：

```text
mypy -> ok
targeted tests -> 18 passed
targeted lint -> ok
full pytest -> 130 passed
```

### 当前边界

DockerSlurmEvidenceCollector 仍保持现有 executor 实现，尚未整体迁移到 EvidenceTransport 协议；这不影响当前 Docker 主线行为。

## 2026-07-12 第四十三批

### 已完成

- 继续 Phase 1 接口硬化，补齐 Worker auth expiry/error taxonomy。
- 新增 Worker 错误分类：
  - `AUTH.EXPIRED`；
  - `AUTH_REQUIRED`；
  - `AUTH.FORBIDDEN`；
  - `SLURM.BACKEND_ERROR`；
  - `EVIDENCE.COLLECTION_ERROR`。
- Worker 对账遇到认证类 Slurm 错误时：
  - `WorkerRunError.code` 写入 `AUTH.EXPIRED` 或 `AUTH_REQUIRED`；
  - `retryable=false`；
  - `auth_required=true`；
  - Run events 追加 `worker.run_error`。
- CollectionTask 采集遇到认证类错误时：
  - 标记为 non-retryable；
  - `collection.task_failed` 事件记录 `error_code`、`retryable`、`auth_required`；
  - 聚合后进入 `collection_state=failed`。
- 保留普通 Slurm backend error 和普通 Evidence collection error 的 retryable 行为。

### 当前校验

```bash
uv run --extra dev mypy src/pilot107
PYTHONPATH=src uv run --extra dev pytest tests/test_runtime_worker.py tests/test_run_store.py tests/test_worker_service.py
uv run --extra dev ruff check src/pilot107/worker/runtime_worker.py tests/test_runtime_worker.py
PYTHONPATH=src uv run --extra dev pytest
```

结果摘要：

```text
mypy -> ok
targeted tests -> 18 passed
targeted lint -> ok
full pytest -> 132 passed
```

### 当前边界

下一步优先处理 command gateway 的 audit、rate limit 和 request id；真实 VM 验证仍保持跳过状态。

## 2026-07-12 第四十四批

### 已完成

- 继续 Phase 1 接口硬化，补齐 command gateway 的 request id、audit log 和基础 rate limit。
- Command gateway 新增 request id：
  - 复用安全的 `X-Request-Id`；
  - 不安全或缺失时生成 `gw-<uuid>`；
  - 响应头和响应体都返回 `request_id`。
- Command gateway 新增 JSONL audit log：
  - 通过 `PILOT107_GATEWAY_AUDIT_LOG` 配置；
  - competition profile 默认写入 `/var/log/slurm/pilot107-command-gateway-audit.jsonl`；
  - 记录请求路径、状态码、耗时、命令名、参数数量、用户和路径；
  - 不写入 bearer token、stdin 或文件内容。
- Command gateway 新增固定窗口限流：
  - `PILOT107_GATEWAY_RATE_LIMIT_MAX`；
  - `PILOT107_GATEWAY_RATE_LIMIT_WINDOW_SECONDS`；
  - 默认 `1200` requests / `60` seconds / client；
  - 超限返回 `429`。
- 单元测试新增：
  - request id 复用与替换；
  - rate limit；
  - audit redaction。

### 当前校验

```bash
uv run --extra dev ruff check simulator/compose/scripts/command-gateway.py tests/test_command_gateway.py
python3 -m py_compile simulator/compose/scripts/command-gateway.py
uv run --extra dev mypy src/pilot107
PYTHONPATH=src uv run --extra dev pytest
```

结果摘要：

```text
gateway lint -> ok
gateway py_compile -> ok
mypy -> ok
full pytest -> 135 passed
```

### 当前边界

下一步可转入 DockerSlurmEvidenceCollector 到 EvidenceTransport 协议的渐进迁移，或开始真实 107 只读 ConfigurationSnapshot probe。

## 2026-07-12 第四十五批

### 已完成

- 新增真实 107 只读 `ConfigurationSnapshot` probe 脚本包。
- 包内容：
  - `scripts/real107_probe/probe_real107_snapshot.py`；
  - `scripts/real107_probe/real107_configuration_snapshot_probe.sbatch`；
  - `scripts/real107_probe/README.md`；
  - `scripts/package-real107-probe.sh`。
- probe 行为：
  - 只调用 HTTP GET；
  - 默认在 Slurm job 内执行 `scontrol token lifespan=600` 获取短期 token；
  - 不提交业务作业；
  - 不取消作业；
  - 不读取用户项目文件；
  - 不保存 JWT 或 Authorization header；
  - 输出 `configuration_snapshot.json` 和 `probe_report.json`。
- 新增离线 fake REST 测试：
  - snapshot 构造；
  - partial probe；
  - `scontrol token` 输出解析；
  - 无 token fallback。
- 新增说明文档：
  - `docs/phase-1/real107_configuration_snapshot_probe.md`。
- 已生成可上传包：
  - `artifacts/probes/pilot107-real107-probe-20260712T004659Z.tar.gz`；
  - `artifacts/probes/pilot107-real107-probe-20260712T004659Z.tar.gz.sha256`。

### 当前校验

```bash
python3 -m py_compile scripts/real107_probe/probe_real107_snapshot.py
PYTHONPATH=src uv run --extra dev pytest tests/test_real107_probe.py
uv run --extra dev ruff check scripts/real107_probe/probe_real107_snapshot.py tests/test_real107_probe.py
bash scripts/package-real107-probe.sh
cd artifacts/probes && sha256sum -c pilot107-real107-probe-20260712T004659Z.tar.gz.sha256
uv run --extra dev mypy src/pilot107
PYTHONPATH=src uv run --extra dev pytest
```

结果摘要：

```text
probe py_compile -> ok
probe tests -> 4 passed
probe lint -> ok
package -> ok
package sha256 -> ok
mypy -> ok
full pytest -> 139 passed
```

### 当前边界

真实 107 probe 包已经 ready，但尚未上传到真实 107 并运行；运行后需要把 `configuration_snapshot.json` 和 `probe_report.json` 回收并更新 `production_access_report.md`。

### 真实 107 首次提交反馈

用户在 `tradmin-02` 上提交旧版 probe carrier job 时收到：

```text
sbatch: error: Batch job submission failed: Invalid qos specification
```

已根据真实 workflow 修正 probe carrier job：

```text
#SBATCH -p Students
#SBATCH --qos=qos_stu_medium_2gpu
#SBATCH --gres=gpu:A100:1
```

结论：Docker simulator 没有完整模拟真实 107 的 mandatory QoS/association 策略；这不否定 Docker 主线闭环，但属于真实平台兼容偏差，后续资源 profile 和 preflight 必须纳入真实 107 QoS 事实。

## 2026-07-12 第四十六批

### 已完成

- 回收并解析真实 107 `ConfigurationSnapshot` probe 输出：
  - `artifacts/probes/real107-probe-output.zip`；
  - `artifacts/probes/real107/real107-probe-output/21039/configuration_snapshot.json`；
  - `artifacts/probes/real107/real107-probe-output/21039/probe_report.json`。
- probe 结果为 `partial`：
  - `ping` 成功；
  - `nodes` 成功；
  - `jobs` 成功；
  - `openapi` 成功；
  - `partitions` 返回 HTTP 500，错误为 `Slurmdb query failed / Connection refused`，但仍带有可用分区摘要 payload。
- 真实 107 当前观测事实：
  - REST target `http://107.ustc.edu.cn:6820`；
  - API version `v0.0.41`；
  - OpenAPI `3.0.3`；
  - Slurm `25.11.2`；
  - auth strategy `single_user_jwt_bearer`；
  - OpenAPI digest `374c1eefce2239ceafe624a45305ffe9b722ea63ef7927903e23c7e87ede9541`；
  - 用户 home/allowed root `/public/home/pb23061276`。
- 真实分区摘要与补充说明一致：
  - `CPU-6530`、`CPU-8358P`；
  - `GPU-RTX5090`、`GPU-A100`；
  - `P107-RTX5090`、`P107-A100`；
  - `Students`。
- `Students` 分区 AllowQos 包含：
  - `qos_stu001`；
  - `qos_stu_default`；
  - `qos_stu_small`；
  - `qos_stu_medium`；
  - `qos_stu_medium_2gpu`；
  - `qos_stu_long`；
  - `qos_stu_cpu_long`。

### 文档更新

- 更新 `docs/phase-1/production_access_report.md`：
  - probe 状态从未执行改为已执行；
  - 增加 job 21039、REST、OpenAPI、Slurm、partition、node、jobs 事实；
  - 记录 `/partitions` partial payload 语义。
- 更新 `docs/phase-1/real107_configuration_snapshot_probe.md`：
  - 增加已回收结果和解释；
  - 增加 Docker simulator 反向要求。
- 更新 `docs/phase-1/interface_hardening_status.md`：
  - 标记 real107 probe 已 ingested；
  - 将后续缺口改为 CapabilityProfile ingest 和 simulator fixture。
- 更新 `docs/phase-1/evidence_transport_decision.md`：
  - `/public` 标记为 real107 probe observed；
  - `/home` 保持 training material / 待独立确认。

### 当前结论

- 真实 107 只读 REST 能力已被初步证明，但仍是 M1-R partial，不是 M2 集成完成。
- Docker simulator 需要补真实 QoS/association、`Students` profile、节点 DOWN/MIXED 和 REST partial response fixture；其中 QoS/association、`Students` profile 和 REST partial response 已在第四十七批完成第一版。
- `configuration_snapshot.cluster.qos=[]` 不能解释为无 QoS；完整 QoS 仍需 `sacctmgr` 或管理员确认。

## 2026-07-12 第四十七批

### 已完成

- 根据真实 107 probe 结果修正 Docker Slurm simulator：
  - `slurm.conf` 增加 107 风格分区：
    - `CPU-6530`；
    - `CPU-8358P`；
    - `GPU-RTX5090`；
    - `GPU-A100`；
    - `P107-RTX5090`；
    - `P107-A100`；
    - `Students`；
    - 保留 legacy `debug` 供旧测试兼容。
  - `gres.conf` 增加 fake A100/RTX5090 GRES；
  - worker hostname 改为 `anode16`、`anode17`；
  - `Students` 默认分区允许真实 probe 观察到的学生 QoS；
  - 新增 `scripts/apply-sim-real107-profile.sh` 初始化 QoS 和用户 association；
  - 新增 `scripts/smoke-sim-real107-profile.sh` 验证真实 profile。
- 应用侧补真实 107 profile：
  - `REAL107_SIM_PARTITION_QOS`；
  - `validate_resource_plan(..., partition_qos=...)`；
  - API service 支持 `PILOT107_CONTRACT_PROFILE=real107-sim`；
  - compose API 默认启用 real107-sim contract profile；
  - Web 默认资源改为 `Students/qos_stu_medium_2gpu`。
- REST partial payload 语义补强：
  - `check_slurm_rest_semantics(..., partial_fields=...)`；
  - 测试覆盖 `errors/warnings + partitions payload` 降级为 warning。
- 演示和 smoke 脚本默认资源从 `debug/normal` 改为：

```text
partition = Students
qos = qos_stu_medium_2gpu
gres = gpu:A100:1
```

### 当前校验

```bash
uv run --extra dev ruff check src/pilot107/api/service.py src/pilot107/core/resources.py src/pilot107/core/contracts.py src/pilot107/core/platform.py src/pilot107/core/rest_semantics.py tests/test_resources.py tests/test_rest_semantics.py tests/test_platform_profile.py tests/test_contracts.py tests/test_api_service.py
uv run --extra dev mypy src/pilot107
PYTHONPATH=src uv run --extra dev pytest
sh simulator/compose/scripts/check-compose-config.sh
bash scripts/build-slurm-sim-image.sh
bash scripts/start-sim-core.sh
bash scripts/smoke-sim-real107-profile.sh
bash scripts/check-sim-core.sh
bash scripts/smoke-sim-command-job.sh
```

结果摘要：

```text
targeted ruff -> ok
mypy -> ok
full pytest -> 144 passed
compose config -> ok
slurm image build -> ok
start sim core -> ok
real107 profile smoke -> ok, job 313 completed
check sim core -> ok
command job smoke -> ok, job 314 completed
```

### 当前边界

- 当前 Ubuntu Slurm 23.11 simulator 可接受 fake GPU GRES 并完成 `--gres=gpu:A100:1` 作业；
- SlurmDBD 不暴露 `gres/gpu` accounting TRES，因此 `GrpTRES=gres/gpu` 无法由 live accounting 强制；
- GPU 组额度和真实 QoS 上限仍由 107Pilot profile/preflight 测试模拟；
- 真实 107 的 submit/cancel/file read 仍未执行 probe，继续保持 M1-R 非阻塞。

## 2026-07-12 第四十八批

### 已完成

- 根据 `/home/knowingthesea/文档/docs-main` 重新梳理平台信息和特征：
  - 平台页面、当前授权和实时命令输出是动态资源事实；
  - 学生常用作业流使用 `Students` 分区；
  - docs-main 示例默认 QoS 为 `qos_stu_default`；
  - 真实比赛 carrier profile 继续使用 `Students/qos_stu_medium_2gpu/gpu:A100:1`；
  - `/public` 是共享存储语义，`/tmp`、`/usr`、`/var`、`/opt` 是节点本地路径语义；
  - FAQ 中的 QoS/walltime/CPU limit/pending reason 进入后续诊断依据。
- 新增 `CapabilityProfile` 第一版：
  - `PartitionCapability`；
  - `QosCapability`；
  - `RestCapability`；
  - `CapabilityProfile`；
  - `docker_sim_capability_profile()`；
  - `capability_profile_from_real107_probe()`；
  - `load_capability_profile()`。
- 默认 competition profile：

```text
profile_id = docker-real107-sim
default_partition = Students
default_qos = qos_stu_medium_2gpu
rest.api_version = v0.0.41
rest.partial_payload_with_errors = true
```

- 真实 probe profile 已验证可从本地目录加载：

```text
artifacts/probes/real107/real107-probe-output/21039
profile_id = real107-probe
default_partition = CPU-6530
default_qos = null
partitions = CPU-6530, CPU-8358P, GPU-RTX5090, GPU-A100, P107-RTX5090, P107-A100, Students
partial_payload_with_errors = true
```

- API/service 接入：
  - `GET /api/v1/platform/capabilities`；
  - `PILOT107_CAPABILITY_PROFILE_PATH`；
  - direct Run prepare preflight 使用 `CapabilityProfile.partition_qos()`；
  - `PILOT107_CONTRACT_PROFILE=real107-sim` 通过 `CapabilityProfile` 获取 partition/QoS。
- 文档树更新：
  - 新增 `docs/phase-1/capability_profile.md`；
  - 更新 `interface_hardening_status.md`；
  - 更新 `current_gap_assessment.md`；
  - 更新 `production_access_report.md`；
  - 更新 `implementation_plan.md`。

### 当前校验

```bash
uv run --extra dev ruff check src/pilot107/core/platform.py src/pilot107/api/http_app.py src/pilot107/api/service.py tests/test_platform_profile.py tests/test_http_api.py tests/test_api_service.py
PYTHONPATH=src uv run --extra dev pytest tests/test_platform_profile.py tests/test_http_api.py tests/test_api_service.py
uv run --extra dev mypy src/pilot107
PYTHONPATH=src uv run python -c "from pathlib import Path; from pilot107.core.platform import load_capability_profile; p=load_capability_profile(Path('artifacts/probes/real107/real107-probe-output/21039')); print(p.to_payload()['profile_id'])"
```

结果摘要：

```text
targeted ruff -> ok
targeted pytest -> 39 passed
mypy -> ok
real107 profile ingest -> real107-probe loaded
```

### 当前边界

- `CapabilityProfile` 已进入后端和接口层，但前端尚未消费；
- QoS 数值上限已进入 profile，但资源预检目前主要使用 partition/QoS 合法性，后续应补 CPU/GPU/memory/walltime BLOCK/WARN；
- OpenAPI digest 可加载，但尚未做自动刷新任务；
- 真实 107 submit/cancel/file read 仍未 probe。

## 2026-07-12 第四十九批

### 已完成

- 前端资源选择消费 `/api/v1/platform/capabilities`：
  - Partition 从静态 input 改为按 `CapabilityProfile.partitions` 填充的 select；
  - QoS 从静态 input 改为按当前 partition `allow_qos` 填充的 select；
  - 默认值使用 `default_partition` 和 `default_qos`；
  - 表单新增 GPU 和 memory 字段，进入 Contract payload。
- 新增前端 Diagnostics 面板：
  - 展示 `profile_id`；
  - 展示 partition/QoS 数量；
  - 展示当前选中 QoS 的 CPU/GPU/memory/walltime 上限；
  - 展示 REST API version、partial payload 语义、dynamic facts 和 limitations；
  - 当前面板消费的是 `CapabilityProfile`，不是正式 Diagnosis API。
- QoS-aware Preflight 接入数值上限：
  - 新增 `QosResourceLimit`；
  - 新增 `CapabilityProfile.qos_limits()`；
  - `validate_resource_plan()` 支持 `qos_limits`；
  - Contract validate 和 direct Run prepare 共用同一套 QoS 上限规则；
  - 已覆盖 CPU/GPU/memory/walltime 超限 BLOCK；
  - 对没有数值上限的 QoS 返回 `RESOURCE.QOS_LIMITS_UNKNOWN` WARN。
- 规则命名统一到 `RESOURCE.QOS_*`：
  - `RESOURCE.QOS_CPU_LIMIT_EXCEEDED`；
  - `RESOURCE.QOS_GPU_LIMIT_EXCEEDED`；
  - `RESOURCE.QOS_MEMORY_LIMIT_EXCEEDED`；
  - `RESOURCE.QOS_WALLTIME_LIMIT_EXCEEDED`；
  - `RESOURCE.QOS_LIMITS_UNKNOWN`。

### 当前校验

```bash
PYTHONPATH=src uv run --extra dev pytest tests/test_web_server.py tests/test_http_api.py tests/test_resources.py tests/test_contracts.py tests/test_api_service.py
uv run --extra dev ruff check src/pilot107/core/resources.py src/pilot107/core/platform.py src/pilot107/core/contracts.py src/pilot107/api/http_app.py src/pilot107/api/service.py tests/test_resources.py tests/test_contracts.py tests/test_api_service.py tests/test_web_server.py
uv run --extra dev mypy src/pilot107
npm run test:ui
```

结果摘要：

```text
targeted pytest -> 55 passed
targeted ruff -> ok
mypy -> ok
Playwright UI -> 8 passed
```

### 下一步

前置规则已统一，已进入 Issue A：

```text
Diagnosis Store
→ Rule Engine
→ 失败 Run 规则诊断
→ GET /runs/{run_id}/diagnoses
```

已完成 Issue A 第一版核心层：

- 新增 `diagnoses` 表；
- 新增 `DiagnosisRecord`；
- 新增 `RunStore.replace_diagnoses()`；
- 新增 `RunStore.list_diagnoses()`；
- 新增 `pilot107.core.diagnosis` 规则引擎；
- 当前覆盖规则：
  - `SLURM.INVALID_QOS`；
  - `SLURM.INVALID_PARTITION`；
  - `RUNTIME.COMMAND_NOT_FOUND`；
  - `RUNTIME.PYTHON_PACKAGE_MISSING`；
  - `RUNTIME.TIMEOUT`；
  - `RUNTIME.OOM`；
  - `RUNTIME.NONZERO_EXIT`。

后续 Issue A 剩余工作：

```text
Evidence snippet selector
→ Worker 自动触发 diagnosis
→ GET /api/v1/runs/{run_id}/diagnoses
```

## 2026-07-12 第五十批

### 已完成

- 完成 Issue A 诊断闭环第一版：
  - 新增 `DiagnosisContextBuilder`；
  - 新增 `DiagnosisService`；
  - 从已索引 EvidenceObject 读取白名单小片段：
    - `submission/submit.stderr`；
    - `logs/stderr.tail.txt/json`；
    - `slurm/job_detail.json`；
    - `slurm/accounting.json`；
    - `environment/summary.json`；
    - `outputs/inventory.json`。
- 新增 Diagnosis API：

```text
GET  /api/v1/runs/{run_id}/diagnoses
POST /api/v1/runs/{run_id}/diagnose
```

- API 权限边界：
  - 复用 Run owner 访问控制；
  - 跨用户读取 diagnoses 返回 `AUTH.FORBIDDEN`。
- Worker 自动触发诊断：
  - Worker 每个 tick 扫描终态 Run；
  - 仅在 `collection_state=succeeded/degraded` 且 `diagnosis_state=pending` 时触发；
  - Worker 重启后仍可发现已完成 collection 但未诊断的 Run；
  - 自动写入 `diagnosis.updated` 和 `diagnosis.worker_completed` 事件。
- Worker service：
  - 常驻 worker 默认构造 `DiagnosisService`；
  - health file 增加 `diagnoses_checked`、`diagnoses_succeeded`、`diagnosis_errors`；
  - `--until-idle` 会等待 diagnosis queue 也为空。

### 当前校验

```bash
PYTHONPATH=src uv run --extra dev pytest tests/test_diagnosis.py tests/test_http_api.py tests/test_runtime_worker.py tests/test_worker_service.py tests/test_run_store.py
uv run --extra dev ruff check src/pilot107/core/diagnosis.py src/pilot107/core/run_store.py src/pilot107/api/http_app.py src/pilot107/worker/runtime_worker.py src/pilot107/worker/service.py tests/test_diagnosis.py tests/test_http_api.py tests/test_runtime_worker.py tests/test_worker_service.py tests/test_run_store.py
uv run --extra dev mypy src/pilot107
```

结果摘要：

```text
targeted pytest -> 54 passed
targeted ruff -> ok
mypy -> ok
```

### 下一步

```text
前端 Diagnostics 面板读取真实 Diagnosis API
→ Agent explain API
→ LLM provider=none/campus 双模式
```

## 2026-07-12 第五十一批

### 已完成

- 前端 Run Diagnostics 已接入真实 Diagnosis API：
  - 新增 `Run Diagnostics` 区块；
  - 读取 `GET /api/v1/runs/{run_id}/diagnoses`；
  - 展示 `rule_id`、severity、summary、evidence refs、suggested patch、retryable、confidence；
  - 终态 Run refresh 后会静默刷新诊断；
  - 手动 `Load Evidence` 后会同步刷新诊断；
  - 诊断 API 失败仅影响 Run Diagnostics 区块，不中断原 Evidence 加载行为。
- UI mock 增加 diagnoses 路由和失败 Run 诊断展示测试。

### 当前校验

```bash
npm run test:ui
PYTHONPATH=src uv run --extra dev pytest tests/test_http_api.py tests/test_diagnosis.py tests/test_runtime_worker.py
```

结果摘要：

```text
Playwright UI -> 9 passed
targeted pytest -> 42 passed
```

### 下一步

```text
Agent explain API
→ LLM provider=none/campus 双模式
→ EvidenceTransport 迁移
```

## 2026-07-12 第五十二批

### 已完成

- 完成 Agent explain API `provider=none` 第一版：
  - 新增 `pilot107.core.agent.AgentExplainService`；
  - 新增确定性解释生成 `explain_without_llm()`；
  - 新增 `POST /api/v1/runs/{run_id}/agent/explain`；
  - 复用 Run owner 权限边界；
  - unsupported provider 返回 `agent_provider_unsupported`；
  - 当前不调用外部 LLM，也不保存解释状态。
- 安全边界：
  - facts 仅从 stored diagnoses 生成；
  - 每条 fact 必须绑定 `evidence_refs`；
  - 无 evidence refs 的 diagnosis 进入 warnings，不生成 fact；
  - 不读取 token、完整环境变量或完整文件内容。
- 前端 Run Diagnostics severity 判断兼容后端小写 `error/warn/info`。

### 当前校验

```bash
PYTHONPATH=src uv run --extra dev pytest tests/test_agent.py tests/test_http_api.py
uv run --extra dev ruff check src/pilot107/core/agent.py src/pilot107/api/http_app.py tests/test_agent.py tests/test_http_api.py
uv run --extra dev mypy src/pilot107
```

结果摘要：

```text
targeted pytest -> 36 passed
targeted ruff -> ok
mypy -> ok
```

### 下一步

```text
LLM provider=none/campus 双模式
→ 前端 Explain 展示
→ EvidenceTransport 迁移
```

## 2026-07-12 第五十三批

### 已完成

- 完成 LLM provider 双模式第一版：
  - `provider=none` 保留确定性 evidence-bound explain；
  - `provider=campus` 新增 OpenAI-compatible Chat Completions provider；
  - campus provider 通过 `PILOT107_LLM_BASE_URL`、`PILOT107_LLM_API_KEY`、`PILOT107_LLM_MODEL`、`PILOT107_LLM_TIMEOUT_SECONDS`、`PILOT107_LLM_MAX_TOKENS` 配置；
  - API service builder 显式读取 LLM 配置；
  - Compose apps/competition 模板透传 `PILOT107_LLM_*`；
  - API README 记录 opencode/USTC provider 到 107Pilot 环境变量的映射方式。
- 安全边界：
  - 不读取或依赖个人 `~/.config/opencode`；
  - 不把任何真实 API key 写入仓库；
  - LLM 只接收 deterministic summary、diagnoses 和带 evidence refs 的 facts；
  - LLM 返回的 narrative/recommendations 不改变 facts。

### 当前校验

```bash
PYTHONPATH=src uv run --extra dev pytest tests/test_agent.py tests/test_http_api.py tests/test_api_service.py
uv run --extra dev ruff check src/pilot107/core/agent.py src/pilot107/api/http_app.py src/pilot107/api/service.py tests/test_agent.py tests/test_http_api.py tests/test_api_service.py
docker compose --env-file .env.example -f compose.yml --profile apps config
docker compose --env-file .env.competition.example -f compose.yml -f compose.competition.yml --profile competition config
```

结果摘要：

```text
targeted pytest -> 45 passed
targeted ruff -> ok
compose apps config -> ok
compose competition config -> ok
```

### 下一步

```text
前端 Explain 展示
→ 校园/USTC LLM 网关 smoke
→ EvidenceTransport 迁移
```

## 2026-07-12 第五十四批

### 已完成

- 前端 Agent Explain 展示接入：
  - Diagnostics 页面新增 `Agent Explain` 区块；
  - provider 可选 `none/campus`；
  - 点击 `Explain` 调用 `POST /api/v1/runs/{run_id}/agent/explain`；
  - 展示 summary、narrative、recommendations、facts、warnings 和 model；
  - 新 Run 会清空旧 explanation，避免跨 Run 状态串扰。
- 行为边界：
  - Explain 仅由用户点击触发；
  - Run polling 和 Evidence refresh 不会自动调用 LLM；
  - Explain 失败只影响 Agent Explain 区块，不影响提交、刷新、取消、Evidence、Diagnosis。
- UI mock 增加 agent explain route 和 evidence-bound explanation 测试。

### 当前校验

```bash
npm run test:ui
PYTHONPATH=src uv run --extra dev pytest
uv run --extra dev mypy src/pilot107
bash scripts/check_phase1_docs.sh
```

结果摘要：

```text
Playwright UI -> 10 passed
pytest -> 166 passed
mypy -> ok
docs check -> ok
```

### 下一步

```text
校园/USTC LLM 网关 smoke
→ EvidenceTransport 迁移
→ REST 专项收敛
```

## 2026-07-12 第五十五批

### 已完成

- 新增校园/USTC LLM 网关 smoke 脚本：
  - `scripts/smoke-campus-llm.py` 只读取 `PILOT107_LLM_*` 环境变量；
  - 不读取个人 opencode 配置，不打印 API key；
  - 构造临时 failed Run 和 evidence-bound diagnosis；
  - 调用 `provider=campus`，校验 provider、status、model、facts 和 explanation 文本；
  - 缺少 LLM 环境变量时默认安全跳过；
  - `PILOT107_REQUIRE_LLM_SMOKE=1` 可用于部署检查中的强制失败。
- 新增 `scripts/smoke-campus-llm.sh`，与现有 smoke 脚本一致从仓库根目录通过 `uv` 运行。
- API README 增补 smoke 命令和跳过/强制策略。

### 当前校验

```bash
env -u PILOT107_LLM_BASE_URL -u PILOT107_LLM_API_KEY -u PILOT107_LLM_MODEL PYTHONPATH=src uv run --extra dev python scripts/smoke-campus-llm.py
uv run --extra dev ruff check scripts/smoke-campus-llm.py
PYTHONPATH=src uv run --extra dev pytest tests/test_agent.py
```

结果摘要：

```text
campus llm smoke missing-env path -> skipped, exit 0
ruff -> ok
targeted pytest -> 3 passed
```

### 下一步

```text
在具备 PILOT107_LLM_* 的环境执行真实网关 smoke
→ EvidenceTransport 迁移
→ REST 专项收敛
```

## 2026-07-12 第五十六批

### 已完成

- DockerSlurmEvidenceCollector 第一段迁移到 `EvidenceTransport`：
  - 新增 `DockerVolumeEvidenceTransport`，用于 worker 已挂载 Docker shared volume 的比赛/应用容器形态；
  - `DockerSlurmEvidenceCollector` 新增可选 `evidence_transport` 和 `evidence_policy`；
  - `logs_finalize` 在配置 transport 时通过 transport 执行授权 stat/tail/hash；
  - `outputs_inventory` 在配置 transport 时通过 transport 执行授权 inventory；
  - 未配置 transport 时保留原 `stat/tail/sha256sum/find` 命令采集回退。
- Worker service 自动装配：
  - 当 `PILOT107_ALLOWED_ROOTS` 在当前 worker 文件系统中真实存在时，`docker-compose-command` / `command-gateway` backend 自动启用 `DockerVolumeEvidenceTransport`；
  - 根路径不存在时保持旧命令采集，兼容本地外部 docker-compose smoke。
- 新增测试：
  - 验证 Docker collector 通过 EvidenceTransport 读取 logs 和 outputs；
  - 验证启用 transport 时不调用 `stat/tail/sha256sum/find` 文件命令；
  - 保留 symlink/path escape 和权限边界测试。

### 当前校验

```bash
PYTHONPATH=src uv run --extra dev pytest
uv run --extra dev mypy src/pilot107/worker/evidence.py src/pilot107/worker/service.py tests/test_evidence.py
uv run --extra dev ruff check src/pilot107/worker/evidence.py src/pilot107/worker/service.py tests/test_evidence.py
bash scripts/smoke-sim-evidence.sh
bash scripts/smoke-sim-evidence-permissions.sh
bash scripts/smoke-sim-worker-service.sh
```

结果摘要：

```text
pytest -> 167 passed
mypy targeted -> ok
ruff targeted -> ok
evidence smoke -> collection=succeeded, artifacts=11, objects=11
evidence permissions smoke -> alice ok, bob denied, symlink denied, cross-run isolated
worker service smoke -> state=SUCCEEDED, collection=succeeded, tasks=7/7
```

### 下一步

```text
EvidenceTransport command-gateway / competition profile 实机 smoke
→ REST 专项收敛
→ M1 HTTPS/reverse proxy 与两机部署脚本
```

## 2026-07-12 第五十七批

### 已完成

- 完成 EvidenceTransport command-gateway / competition profile 实机修正：
  - 新增 `scripts/smoke_competition_evidence_transport.py`；
  - 新增 `scripts/smoke-competition-evidence-transport.sh`；
  - 专项 smoke 校验 logs 和 outputs 当前走 `command_fallback`；
  - 验证 `DockerVolumeEvidenceTransport` 在当前非 root 应用容器下会受到 `/public/home/alice/slurm-*.out` 文件权限限制；
  - 将 `DockerVolumeEvidenceTransport` 改为 `PILOT107_ENABLE_DOCKER_VOLUME_EVIDENCE_TRANSPORT=1` 显式 opt-in；
  - competition 默认保持 command-gateway fallback，符合服务用户不直接读取用户私有日志的权限模型。
- 修正 Worker collection retry：
  - retryable collection failure 写入 `next_attempt_at`；
  - 退避为 1/2/4/8/16/32/60 秒；
  - 失败事件记录 `retry_delay_seconds`；
  - 修复 `RuntimeReconcileWorker.run_until_idle()` 缩进错误；
  - 增加 rate limit 不应被误判为 auth required 的回归测试。
- 调整 competition command gateway 限流：
  - competition profile 默认 `PILOT107_GATEWAY_RATE_LIMIT_MAX=6000`；
  - 保留 gateway 固定窗口限流和审计；
  - 目标是支撑 100 并发 API 操作和 Evidence 批量采集。
- 明确 LLM 边界：
  - 当前 `check-competition.sh`、EvidenceTransport smoke 和 100 并发承载测试不调用 LLM；
  - 若后续接入真实 Agent Explain，可通过 `PILOT107_LLM_MODEL` 指向 qwen 系列校园/USTC provider 模型。

### 当前校验

```bash
uv run --extra dev ruff check src/pilot107/core/run_store.py src/pilot107/worker/runtime_worker.py src/pilot107/worker/service.py scripts/smoke_competition_evidence_transport.py tests/test_runtime_worker.py tests/test_worker_service.py tests/test_command_gateway.py
uv run --extra dev mypy src/pilot107/core/run_store.py src/pilot107/worker/runtime_worker.py src/pilot107/worker/service.py scripts/smoke_competition_evidence_transport.py tests/test_runtime_worker.py tests/test_worker_service.py
PYTHONPATH=src uv run --extra dev pytest tests/test_runtime_worker.py tests/test_run_store.py tests/test_worker_service.py tests/test_evidence.py tests/test_command_gateway.py
bash scripts/smoke-competition-evidence-transport.sh
bash scripts/check-competition.sh
python3 scripts/load_competition.py --concurrency 100 --scenario all --timeout 30
```

结果摘要：

```text
ruff targeted -> ok
mypy targeted -> ok
pytest targeted -> 35 passed
competition evidence transport smoke -> ok, logs=command_fallback/command_fallback, outputs=command_fallback
competition web smoke -> success/failure/cancel all reached capsule ready
100 concurrency load -> read 100/100, validate 100/100, prepare 100/100, errors=0
```

### 下一步

```text
REST 专项收敛
→ M1 HTTPS/reverse proxy 与两机部署脚本
→ 前端设计包接入前的后端交互回归固化
```

## 2026-07-13 第五十八批

### 已完成

- REST 专项收敛（路径 A — simulator REST live）全 6 lane 落地：
  - Lane 1（Slurm simulator JWT auth）：从 Ubuntu `slurm-wlm` 源码构建 `auth_jwt.so`（`apt-get source` + `--with-jwt` + `libjwt-dev`，因 SchedMD 无 apt 仓库、`download.slurm.sh` NXDOMAIN），仅 COPY 单个 `.so` 到 `/usr/lib/x86_64-linux-gnu/slurm-wlm/auth_jwt.so`；`slurm.conf`/`slurmdbd.conf` 增 `AuthAltTypes=auth/jwt` + `AuthAltParameters=jwt_key=/etc/slurm/jwt_hs256.key`；`jwt_hs256.key`（32 随机字节，slurm:slurm 0400）烤入镜像（弃用 bind-mount 解决 host/container UID 不一致）；slurmrestd 保持 `-a rest_auth/jwt` + `0.0.0.0:6820`，增 `SLURM_JWT=daemon`；清理 `compose.yml`（5 处）+ `compose.competition.yml`（1 处）遗留 key bind-mount；live 验证 `scontrol token lifespan=3600` + `curl GET /slurm/v0.0.40/nodes` → 200（anode16/anode17，Slurm 23.11.4）。
  - Lane 2（REST 适配器契约测试）：`tests/test_rest_native_backend.py` 27 个契约测试（6 矩阵 + 3 submit smoke + read/cancel/auth/语义分类），混合 in-process `ScriptedTransport` 与 real-socket `FakeSlurmRestServer`；确认 `RestNativeSlurmBackend`/`UrllibHttpTransport`/`RestAuthStyle.SLURM_HEADERS`/`check_slurm_rest_semantics` 已正确，无需硬化；确认 `SLURM_HEADERS` 同时发送 `X-SLURM-USER-NAME` + `X-SLURM-USER-TOKEN`，无 Bearer；暴露 idempotency 不去重、不强制 WorkDirPreflight、cancel 不预检终态三项 gap 转交 Lane 3/4b；既有 lint 做机械 ruff 修正（无逻辑变更）。
  - Lane 3（token mint + probe 重做 + live 矩阵）：新增 `src/pilot107/adapters/rest_token.py`（`SimulatorRestTokenProvider`：`scontrol token` 签发、按用户内存缓存、60s 刷新阈值、`threading.Lock` 线程安全、token 不入日志；`StaticTokenProvider`、`RestTokenProvider` Protocol、`_parse_slurm_jwt`）+ `tests/test_rest_token.py`（16 测试）；新增 `scripts/_sim_rest_helpers.py`（`detect_sim_rest_url()`、`mint_sim_token()`、`DEFAULT_API_VERSION=v0.0.40`）；重做 `probe_sim_rest_auth.py`（真实 token + v0.0.40，报 supported）；解 skip `probe_sim_rest_submit.py`（真实 submit/get/cancel）；新增 `scripts/smoke_sim_rest_live.sh` + `.py`（11 场景 live 矩阵）；适配器 v0.0.41 默认保留，simulator 路径显式传 v0.0.40；live 确认适配器不对 idempotency_key 去重；accounting 端点为 `/slurmdb/v0.0.40/jobs`（`/slurm/v0.0.40/accounting` 404）。
  - Lane 4a（real107 smoke + digest 刷新）：新增 `scripts/probe_real107_rest_readonly.py` + `.sh`（只读 GET probe，无 token 安全跳过，计算 openapi_digest，token 不入输出）；`src/pilot107/core/platform.py` 新增 `compute_openapi_digest`/`refresh_openapi_digest`/`refresh_configuration_snapshot_digest`/`refresh_rest_capability_digest`（token 不入 digest/errors）；`tests/test_openapi_digest.py`（13 测试）。
  - Lane 4b-i（WorkDirPreflight）：新增 `src/pilot107/core/preflight.py`（约 640 行，`PathChecker` Protocol、`LocalPathChecker`、`preflight_workdir_paths` 纯函数、`preflight_workdir_fs` 注入 FS，返回 `list[PreflightFinding]`，`WORKDIR_*` 代码）+ `tests/test_preflight.py`（26 测试，`/tmp`/local-only/path-escape → BLOCK）。
  - Lane 4b-ii（服务接入）：新增 `src/pilot107/adapters/rest_token_backend.py`（`TokenMintingRestBackend` 包装器按用户 mint、不入 receipt/日志；`find_jobs_by_marker`）；新增 `src/pilot107/core/submission_reconcile.py`（`reconcile_submission` → `ReconcileResult` bound/not_found/uncertain）；`src/pilot107/core/run_service.py` 接入 WorkDirPreflight + idempotency 对账 + 新错误类 `WorkDirPreflightError`/`SubmissionUncertainError`；`api/service.py` + `worker/service.py` 新增 `rest_token_provider_enabled`/`workdir_preflight_enabled`/`idempotency_reconcile_enabled` 标志；`simulator/compose/.env.example`/`.env.competition`/`.env.competition.example` 增 `PILOT107_REST_AUTH_STYLE=slurm_headers`、`PILOT107_SLURM_API_VERSION=v0.0.40`、`PILOT107_REST_TOKEN_PROVIDER=1`；`tests/test_service_rest_wiring.py`（11 测试）、`tests/test_submission_reconcile.py`（5 测试）。
  - 关键技术决策：simulator 锁定 v0.0.40（Slurm 23.11.4 最大 API 版本，`v0.0.41` 路径 404，属 24.05+）；`SLURM_HEADERS` 而非 Bearer；`auth_jwt.so` 源码构建；`jwt_hs256.key` 烤入镜像；bad token → HTTP 500（error_number 7000）而非 401，仅 no-token → 401；accounting 走 `/slurmdb/` 而非 `/slurm/`。
  - 主线边界：command backend 仍为默认，`RestNativeSlurmBackend` 仅 env-gated 可选；竞赛演示不依赖 REST live，`check-competition.sh` 无回归。
  - 已知遗留：唯一 job name marker 仍硬编码 `pilot107-run`（per-run 唯一 marker 暂缓）；command-gateway FS checker 待补；live REST smoke 仍直连适配器，wired `rest-native` 端到端仅 fake 单测覆盖；对账仅 `not_found` 重试一次；真实 107 submit/cancel/file read 仍未 probe（M1-R 非阻塞）；LLM campus provider 仍只在 smoke 中 skipped。

### 当前校验

```bash
PYTHONPATH=src uv run --extra dev pytest
uv run --extra dev mypy src/pilot107
uv run --extra dev ruff check src/pilot107 tests
bash scripts/probe-sim-rest-auth.sh
bash scripts/probe-sim-rest-submit.sh
bash scripts/smoke_sim_rest_live.sh
bash scripts/check-competition.sh
```

结果摘要：

```text
pytest -> 269 passed in 6.79s（收敛前 167，新增 102）
mypy -> Success, no issues found in 32 source files（收敛前 24-25）
ruff src/pilot107 tests -> all checks passed
probe-sim-rest-auth -> status=supported（原 blocked）
probe-sim-rest-submit -> status=submitted（原 skipped）
smoke_sim_rest_live -> 11/11 scenarios pass
check-competition -> competition web smoke ok（success/failure/cancelled + capsules）
scripts/ 既有 ruff baseline 42 errors 为预存 smoke-script lint，不在本批次范围
```

### 下一步

```text
M1 HTTPS/reverse proxy 与两机部署脚本
→ 前端设计包接入前的后端交互回归固化
```

## 2026-07-13 第五十九批

### 已完成

- 接续 `docs/phase-1/error_library_integration_plan.md`，完成已知错误库后端第一段：
  - `src/pilot107/core/diagnosis.py` 新增 `KnownErrorRule`；
  - 诊断引擎从 `data/known_errors/*.yaml` 加载规则；
  - 支持 `symptoms` 子串匹配、`regex:` 正则匹配、`terminal_state_match` 和 `state_match`；
  - 保留现有 7 条规则的 `rule_id`、触发条件和 `suggested_patch` 向后兼容；
  - `DiagnosisContextBuilder` 的 evidence snippet 路径改为默认 8 路径 + 规则声明路径并集；
  - 在无 PyYAML 的 `uv` 环境下使用受限 YAML parser，避免新增运行时依赖。
- 扩展诊断持久化：
  - `DiagnosisRecord` 增加可选 `category`、`stage`、`fix_guide`；
  - `RunStore` 对 `diagnoses` 表做非破坏性列迁移；
  - API 诊断 payload 透传新增字段。
- 扩展 Agent explain：
  - deterministic facts 纳入 `fix_guide.fix` / `prevention` / `automation`；
  - campus LLM system prompt 明确只能使用 facts、fix_guide 和 evidence_refs。
- 新增已知错误库 API：
  - `GET /api/v1/diagnosis/known-errors`；
  - `GET /api/v1/diagnosis/known-errors/{error_id}`；
  - 支持列表摘要和单条详情（symptoms、evidence_paths、fix_template、fix_guide、state/terminal match）。
- 新增/扩展测试：
  - YAML 规则加载与旧规则兼容；
  - terminal/state match 触发；
  - `category` / `stage` / `fix_guide` 入库；
  - diagnosis API 字段透传；
  - known-errors API 列表/详情/404；
  - Agent fact 包含 fix guide。

### 当前校验

```bash
PYTHONPATH=src uv run --extra dev pytest tests/test_diagnosis.py tests/test_agent.py tests/test_http_api.py
PYTHONPATH=src uv run --extra dev mypy src/pilot107
PYTHONPATH=src uv run --extra dev ruff check src/pilot107 tests
PYTHONPATH=src uv run --extra dev pytest tests
```

结果摘要：

```text
targeted pytest -> 45 passed
mypy -> Success, no issues found in 32 source files
ruff src/pilot107 tests -> all checks passed
full pytest -> 272 passed in 6.83s
```

注：全量 pytest 在默认 sandbox 下有 5 个 real-socket 测试因 `PermissionError: Operation not permitted` 失败；提升权限允许本地 127.0.0.1 临时 HTTP server 后全量通过。

### 尚未完成

- `data/known_errors/` 当前仅有 7 条旧规则 + `SLURM.ARRAY_DEPENDENCY_NEVER_SATISFIED`；
- Wan–HiF4 其余通用模式、107 特化模式和 `INDEX.yaml` 尚未补齐；
- `data/submission_templates/` 尚未落地；
- 前端暂未展示 `fix_guide`；
- `docs/phase-1/error_library.md` / `submission_templates.md` 尚未新增。

### 下一步

```text
补齐 Wan–HiF4 + 107 特化错误 YAML 与 INDEX
→ 优秀提交模板 data/submission_templates
→ 前端展示 fix_guide
→ M1 HTTPS/reverse proxy 与两机部署脚本
```

## 2026-07-14 第六十批

### 已完成

- 补齐 `data/known_errors/` 错误库数据层：
  - 当前共 27 条规则；
  - 覆盖既有 7 条硬编码迁移规则；
  - 覆盖 Wan–HiF4 记录：HF4-005 合并入 `RUNTIME.PYTHON_PACKAGE_MISSING`，HF4-007 保留并修正既有 `SLURM.ARRAY_DEPENDENCY_NEVER_SATISFIED` regex，其余记录新增为独立 YAML；
  - 新增 107 特化规则：`SLURM.WORKDIR_NOT_SHARED`、`SLURM.REST_AUTH_REJECTED`、`SLURM.SUBMISSION_UNCERTAIN`、`RUNTIME.PYTHON_PACKAGE_TRANSIENT`、`ARTIFACT.POSTPROCESS_FALSE_FAILURE`；
  - 新增 `data/known_errors/INDEX.yaml`。
- 新增优秀提交模板：
  - `data/submission_templates/recipe_student_cpu_basic.yaml`；
  - `data/submission_templates/recipe_student_gpu_array.yaml`；
  - `data/submission_templates/recipe_resilient_submission.yaml`；
  - `data/submission_templates/INDEX.yaml`。
- 模板覆盖的关键协议：
  - `set -Eeuo pipefail`；
  - 显式 `KIT_ROOT` / `DATA_ROOT` 导出，不依赖 `$0/BASH_SOURCE`；
  - `tmp -> validate -> atomic mv -> COMPLETE`；
  - array throttle 与 DAG 层 GPU 峰值说明；
  - missing task scanner / resubmit missing；
  - cwd 稳定化、import probe、marker reconcile。
- 新增 `tests/test_submission_templates.py`，守住模板索引、strict mode 和 COMPLETE 协议。
- 前端 Run Diagnostics 已展示 `fix_guide`：
  - `src/pilot107/web/static/assets/app.js` 的诊断条目展示 `category`、`stage`；
  - 追加 `Fix:` / `Prevention:` / `Automation:` 三段；
  - `tests/ui/visual.spec.js` mock 数据覆盖新增字段。
- 新增总览文档：
  - `docs/phase-1/error_library.md`；
  - `docs/phase-1/submission_templates.md`。
- 补齐 M1 HTTPS/reverse proxy 与两机部署脚本化入口：
  - 新增 `simulator/compose/compose.competition-slurm-host.yml`，暴露 Slurm 宿主机 command-gateway；
  - 新增自包含 `simulator/compose/compose.competition-app-node.yml`，仅运行 API/Worker/Web/reverse-proxy，远端连接 Slurm 宿主机 command-gateway；
  - 新增 `scripts/start-competition-slurm-host.sh` / `stop-competition-slurm-host.sh`；
  - 新增 `scripts/start-competition-app-node.sh` / `stop-competition-app-node.sh`；
  - `.env.competition.example` 增 `PILOT107_COMMAND_GATEWAY_PORT` 和 `PILOT107_REMOTE_COMMAND_GATEWAY_URL`；
  - `scripts/export-competition-bundle.sh` 纳入 data 目录、phase-1 文档、两机脚本和 compose override；
  - `README_DEPLOY.md` 生成内容增加单机/两机启动路径；
  - `docs/phase-0/competition_deployment_plan.md` 和 `vm_test_readiness.md` 增加两机执行顺序。

### 当前校验

```bash
PYTHONPATH=src uv run python -c 'from pilot107.core.diagnosis import load_known_error_rules; rules=load_known_error_rules(); print(len(rules))'
python3 -c '<parse data/submission_templates/*.yaml and bash -n sbatch_template>'
PYTHONPATH=src uv run --extra dev pytest tests/test_diagnosis.py tests/test_submission_templates.py tests/test_http_api.py
PYTHONPATH=src uv run --extra dev mypy src/pilot107
PYTHONPATH=src uv run --extra dev ruff check src/pilot107 tests
PYTHONPATH=src uv run --extra dev pytest tests
npm run check:js
npm run test:ui
bash -n scripts/export-competition-bundle.sh scripts/start-competition-slurm-host.sh scripts/stop-competition-slurm-host.sh scripts/start-competition-app-node.sh scripts/stop-competition-app-node.sh
docker compose ... compose.competition-slurm-host.yml config
docker compose ... compose.competition-app-node.yml config
PILOT107_SKIP_BUILD=1 PILOT107_EXPORT_IMAGES=0 PILOT107_BUNDLE_DIR=/tmp/pilot107-bundle-test bash scripts/export-competition-bundle.sh
```

结果摘要：

```text
known error loader -> 27 rules
submission templates -> YAML parse ok, sbatch_template bash -n ok
targeted pytest -> 43 passed
mypy -> Success, no issues found in 32 source files
ruff src/pilot107 tests -> all checks passed
full pytest -> 273 passed in 7.02s
node --check app.js -> ok
playwright ui -> 10 passed
two-machine script bash -n -> ok
slurm-host/app-node compose config -> ok
light bundle export -> ok,新增脚本/override/data/docs 已入包
```

注：全量 pytest 和 Playwright UI 均需提升权限允许本地 127.0.0.1 临时 HTTP server / Chromium。

### 尚未完成

- 模板仍为独立数据文件，尚未接入 `RecipeCatalog` API；
- 两机 profile 已完成 compose config 与 bundle 验证，仍需真实两台 VM 网络/防火墙/证书实测。

### 下一步

```text
真实两台 VM 网络/防火墙/证书实测
→ 前端设计包接入前的后端交互回归固化
```
