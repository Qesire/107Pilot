# PostgreSQL 业务域迁移 Runbook

`PILOT107_DATABASE_MODE=postgres` 是生产业务数据库模式开关。生产通过
`PILOT107_POSTGRES_DSN_FILE` 提供 DSN；`PILOT107_POSTGRES_DSN` 仅用于隔离测试。设置后，API 与
Worker 会同时使用 PostgreSQL 保存 Run、Contract、平台快照、权益快照、模板市场、
修复会话及 control-plane 的 lease/outbox/trace。`PILOT107_CONTROL_POSTGRES_DSN`
仍可用于旧的“只迁 control-plane”兼容模式，但不能和前者混用为两个不同数据库。

SQLite 继续只作为本地开发、离线演示和迁移回退证据库；生产 API/Worker 不应在
`PILOT107_POSTGRES_DSN` 已设置时继续写入它。

应用镜像已包含 PostgreSQL runtime extra；`simulator/compose/compose.yml` 和两机
`compose.competition-app-node.yml` 都会把该变量同时传给 API 与 Worker。生产环境应让
两者连接到同一个受 TLS 和最小权限保护的 PostgreSQL 服务，而不是使用本地 SQLite volume。

## 一次性迁移步骤

1. 在生产 PostgreSQL 创建最小权限的应用角色、UTF-8 数据库和 TLS 连接；DSN 仅写入
   secret manager 或服务环境，不写入命令行、Git 或运行日志。
2. 在维护窗口停止 API 和 Worker 的所有 writer，并确认没有在执行 submit、采集、
   capsule 或 Agent action。先使用既有恢复入口备份 SQLite、Evidence 与 Capsule。
3. 部署包含 `004a.001.postgres_domain_schema` 及其后续追加迁移（当前为
   `004a.002.run_publications`）的版本。数据库初始化由 advisory lock 串行化；它不会
   修改 SQLite。不得改写已记录迁移的 checksum；业务表扩展必须以新的 migration ID 追加。
4. 从运行节点执行（DSN 只经环境传递）：

   ```bash
   export PILOT107_DATABASE_MODE=postgres
   export PILOT107_POSTGRES_DSN_FILE='/run/secrets/pilot107-postgres-dsn'
   PYTHONPATH=src uv run python scripts/migrate-sqlite-domain-to-postgres.py \
     --sqlite-db /var/lib/pilot107/pilot107.db --source-quiesced
   ```

   导入器绝不 truncate PostgreSQL，也绝不修改 SQLite。目标非空时，只有全部表行数和
   canonical digest 与源完全一致才会报告 `already_complete`；否则会失败并回滚事务。
5. 再跑一次只读校验：

   ```bash
   PYTHONPATH=src uv run python scripts/migrate-sqlite-domain-to-postgres.py \
     --sqlite-db /var/lib/pilot107/pilot107.db --verify-only
   ```

6. 为 API 和 Worker 同时设置同一个数据库模式和 DSN secret file，再启动 Worker，最后启动
   API。先执行只读 snapshot、模板进入 Studio、短作业提交/查询、完成后 Evidence 与
   capsule 收集、Agent 修复会话六条验证；每条都记录 run_id、Slurm job_id、request_id
   和数据库 migration ID。

## 回退边界

在第 6 步前，回退只需保持服务停写并撤销环境变量，SQLite 源库仍未被改动。第 6 步后
不能把“切回 SQLite”当作无损故障切换，因为 PostgreSQL 可能已接受新写入；必须先停写、
备份 PostgreSQL，并执行经过验证的 PostgreSQL→SQLite 恢复流程或从 PostgreSQL 备份恢复。
严禁通过手工删表或双写来制造回退。

## 可重复门禁

真实 PostgreSQL 回归使用专用临时数据库，显式要求 reset opt-in：

```bash
bash scripts/smoke-postgres-domain-migration.sh
```

该门禁会创建完整 schema，迁移一个包含 Run/event、平台 snapshot、模板 draft 和
RemediationSession 的 SQLite 源库，逐表 digest 对比后，再从 PostgreSQL 读取每个领域对象。
容器镜像不可用时它必须失败或跳过，不能作为已验证证据。

若运行节点已安装 PostgreSQL server 二进制，也可启动完全隔离、位于临时目录的本机实例：

```bash
PILOT107_TEST_POSTGRES_MODE=local bash scripts/smoke-postgres-domain-migration.sh
```

该模式以 UTF-8、trust-only 的专用用户初始化一个 `/tmp` 数据目录，绑定 loopback 测试端口，
结束时停止 server 并删除该目录；不读取、修改或复用系统 PostgreSQL 集群。

要验证 PostgreSQL 存储与实际 Docker Slurm 的完整业务闭环，先启动 simulator，并且只向
专用、可清空的测试库提供下面两个变量：

```bash
export PILOT107_TEST_POSTGRES_DSN='postgresql://…/pilot107_test'
export PILOT107_TEST_POSTGRES_ALLOW_RESET=1
PYTHONPATH=src uv run --all-extras scripts/smoke_sim_phase3c.py
```

该 Phase 3C 门禁会显式 truncate 该测试库，然后通过模板发布、采用、Contract preflight、
Slurm submit、`sacct` 证据收集、raw Capsule 和模板验证。它绝不能指向生产数据库。

本机已安装 PostgreSQL server 时，下列入口会自动建立临时 UTF-8 测试库，再执行相同的
Docker Slurm 闭环，结束时删除该集群：

```bash
bash scripts/smoke-sim-phase3c-postgres.sh
```

要额外验证 Web BFF → API → Worker 通过同一个临时 PostgreSQL 的完整 HTTP 路径，可运行：

```bash
bash scripts/smoke-sim-web-postgres.sh
```

该入口仅把临时数据库绑定到 Docker `sim` 网络 gateway，完成后会恢复本地 API、Worker 与 Web
到默认 SQLite 配置，避免临时数据库清理后留下重启循环。

competition profile 已启动时，可再运行 `bash scripts/check-competition.sh`，验证 HTTPS Web
BFF → API → command gateway → 实际 Slurm 的成功、失败、取消三类作业都写入 PostgreSQL，且
都有 Evidence 与 Capsule。
