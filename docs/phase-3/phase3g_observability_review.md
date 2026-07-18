# Phase 3G 控制面可观测性底座审查

日期：2026-07-18
范围：API 请求、durable outbox、Worker 长期累计、freshness/active 生命周期、Prometheus 告警与结构化错误脱敏。
结论：本底座切片 P0/P1 已清零，Compose 实际 stdlib 路径和 FastAPI 适配器均有同一 scrape 语义；R4-3 仍需 LLM/SSE 专项指标、持久 trace correlation、完整安全基线与 PostgreSQL 业务 Store 接线。

## 固定契约

- Worker 指标按稳定 worker ID 分文件持久化，read-modify-write 使用跨进程文件锁，发布使用 fsync + atomic replace、权限 `0600`；
- ticks、reconcile、submit、Evidence collection、diagnosis、Agent execution、remediation 的 checked/succeeded/error 跨进程重启累计；
- graceful stop 写 `active=0`，硬崩溃保留 `active=1`，stale 告警只匹配 active Worker；重新启动同一 ID 延续原计数并恢复 active；
- outbox 双后端提供 topic/state messages、attempts、reclaims、due pending 与 expired running 快照；
- API 指标只使用归一化 route，Run/Contract/Advice/Execution/Session 等对象 ID 不进入 label；
- metric source 损坏时 `/metrics` 仍返回，置 `pilot107_metrics_scrape_error=1`，不回显异常或文件内容；
- Worker health、outbox last_error、Run event 与 remediation audit event 持久化前统一脱敏；fencing token 与 LLM token count 明确保留；
- Compose healthcheck 校验 tick 小于 60 秒、telemetry 无错误、active 与 schema，不再把任意陈旧非空文件视为健康。

## Findings-first 结果

### 已修复 P1：最近 tick health 无长期累计且多 Worker 会覆盖

原 `worker-health.json` 只能看到最后一次 tick，多 Worker 共享路径会覆盖，重启后历史丢失。现在每个 worker ID 使用独立哈希文件，累计计数跨重启延续；相同 ID 并发更新通过 flock 串行化，40 个并发更新无丢失。

### 已修复 P1：health 发布非原子且健康检查只判断文件存在

直接 `write_text` 可能暴露部分 JSON；原 Compose probe 对旧文件、`ok=false` 和停止更新均保持 healthy。现在 health 与 telemetry 原子发布，probe 强制 freshness、telemetry、active 与 schema。

### 已修复 P1：错误与审计可能持久化凭据

Worker error、outbox retry error、Run/remediation event payload 先前可保存 DSN、Bearer、password/token assignment。统一 redactor 现在覆盖结构化 secret key、URL credentials、Bearer 与 assignment；metric scrape 错误不输出详情。

### 已修复 P1：过宽 token 脱敏破坏 fencing 审计

第一次全量门禁发现 `fencing_token` 被当成 credential，导致 crash/reclaim 测试从 `[1,2]` 变成 redacted。现在只 allowlist fencing 与 LLM token-count 字段；真实 token/access token 仍脱敏。

### 已修复 P1：测试的 FastAPI `/metrics` 不存在于 Compose stdlib 入口

首次 Docker live 请求 `/metrics` 命中鉴权 catch-all，证明适配器测试不能代表发布入口。metrics registry 已下沉到 transport-neutral API，stdlib 与 FastAPI 分别观测并共享 scrape；新增真实 `ThreadingHTTPServer` 回归。

### 已修复 P1：容器 hostname 变化制造永久 stale Worker

第一次 live 重建产生新的 hostname worker ID，旧文件保持 active 并会永久告警。Compose 现配置稳定 `runtime-worker-primary`；graceful tombstone 区分正常停止与崩溃，旧 live 快照已通过同一 API 标为 inactive。

### 已修复 P1：未知 URL label 高基数与 telemetry lock symlink

最初归一化只替换已知对象位置，未知 URL/action 仍可能进入 Prometheus label；现在未知 API root 和 action 各自收敛到固定 placeholder。跨进程 lock 改用 `O_NOFOLLOW` 打开并验证 regular file，拒绝通过 symlink 修改或锁定外部目标。

## 验证证据

- Worker telemetry：跨 store 重建、不同 Worker 隔离、同 ID 40 并发无丢计数、corrupt/symlink fail closed、stop/resume active 语义；
- API：FastAPI 与真实 stdlib server 均验证无身份 `/metrics`、route 归一化、outbox/Worker 汇总、损坏来源不泄露；
- ControlRepository：SQLite/PostgreSQL 共享契约覆盖 due、expiry、attempt 与 reclaim；无 PG 环境时明确 skipped；
- alert config：5 条规则名称唯一，包含持续时间、severity 与 summary；
- 全量门禁：578 passed、11 PostgreSQL integration skipped、2 subtests passed；Ruff、strict mypy（70 source files）、Compose config 与 `git diff --check` 通过；
- simulator core check：API/Worker/Web healthy，Slurm controller UP，2 个模拟节点 idle；
- Docker live：stdlib `/metrics` 返回 normalized `/api/v1/runs/{run_id}` 且不含真实 ID；outbox due=0、expired=0、scrape error=0；
- Worker live restart：稳定 ID `runtime-worker-primary`，ticks 从 248 延续到 280，重启后 active=1、telemetry error=null、health=healthy；旧 ephemeral Worker active=0；
- 一次性 PostgreSQL 16 容器重跑因 Docker Registry manifest 请求 EOF 未启动；没有把该 cell 记为通过，之前的 PostgreSQL parity 证据不覆盖本次新增 metric query；
- 未连接真实 107，未上传或部署 VM。

## 残余风险与后续输入

1. API counter 为进程内累计，长期 retention 依赖 R5 包内的 Prometheus-compatible collector 或外部监控；
2. Agent route/Worker 已有总量，但 LLM provider latency/token/failure 与 SSE active connection 尚未独立计量；
3. `X-Request-ID` 与 Run/Job/Session 各自可用，尚缺单一持久 request-to-domain trace/audit record；
4. `/metrics` 无用户鉴权，只能置于可信监控网络或 proxy allowlist；该网络边界属于 3G-4 安全切片；
5. PostgreSQL 新 metric query 仍需在可用 PG16 环境补跑真实契约；完整业务 Store parity 仍未完成；
6. alert rules 已验证结构，尚未由本机 Prometheus 执行 firing/resolution 状态机。
