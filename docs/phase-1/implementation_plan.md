# Phase 0 实施计划：Docker 主线优先

> 状态：started  
> 日期：2026-07-10  
> 设计来源：`/home/knowingthesea/文档/107/design_v1.4`

## 1. 基线调整

比赛交付目标调整为：

```text
学校提供的校园网应用节点
→ 运行 107Pilot Web/API/Worker
→ 连接学校提供的 Docker Slurm 模拟宿主机
→ 完成全功能演示闭环
```

真实 107 平台降级为：

```text
参考平台兼容目标
+ 可选只读探测
+ 少量真实作业验证
```

它不再是 Docker 主线开发的阻塞条件。

## 2. 阶段目标

本阶段必须形成并推进：

```text
docs/phase-0/docker_mainline_plan.md
docs/phase-0/competition_deployment_plan.md
docs/phase-0/real_platform_compatibility_plan.md
docs/phase-0/server_questions.md
```

原 Phase -1 文档保留为真实平台兼容探测参考，不再阻塞主线。

## 3. 阶段边界

本阶段做：

- 搭建完整 Docker Slurm；
- 分离 Web/API/Worker；
- 模拟多用户和共享目录权限；
- 实现 REST 查询、REST submit、模拟 command backend；
- 完成成功、失败、取消、重试；
- 完成 Evidence、Capsule、Worker 重启恢复；
- 准备两机比赛部署问题清单。

本阶段不做：

- 不持久化真实 token；
- 不把真实 107 REST submit 作为主线依赖；
- 不接入真实 `/public` ACL；
- 不做无人值守真实平台 command proxy；
- 不把 Docker 验证冒充真实 GPU 验证。

## 4. 任务拆分

### 0A-1 本地 Docker Slurm

验收：

- Slurm 控制面、slurmrestd、slurmdbd、MariaDB 可启动；
- REST 查询和 accounting 可用；
- shared `/public` 与 worker-local `/tmp` 差异可验证。

### 0A-2 应用进程分离

验收：

- `pilot107-api`、`pilot107-worker`、`pilot107-web` 不与 `slurmctld` 混在一个进程或容器；
- API/Worker 使用非 root 用户；
- 应用容器最小权限。

### 0A-3 多用户权限模拟

验收：

- `alice`、`bob`、`pilot107`、`slurm` 身份存在；
- Alice 不能读取 Bob 的目录；
- symlink escape 被拒绝；
- Worker 只能采集授权 Run。

### 0A-4 REST submit 与 command backend

验收：

- REST + 合法 shared workdir 成功；
- REST + invalid/unwritable workdir 结构化失败；
- REST 超时按 marker 对账；
- command backend 白名单执行；
- Shell 注入被拒绝。

### 0B 分布式比赛部署

验收：

- 应用节点能访问 Docker 宿主机；
- 只开放 slurmrestd 和必要 EvidenceTransport；
- Web 使用 HTTPS；
- DB 和 Evidence 持久化；
- 两台机器重启后可恢复。

### 0C 真实平台非阻塞探测

验收：

- GET ping/jobs/nodes/partitions；
- 短期 JWT 测试；
- 可选人工确认 smoke job；
- 任意失败不阻塞 0A/0B。

## 5. 阶段门

Phase 0A 通过条件：

- [x] 一个独立 API；
- [x] 一个独立 Worker；
- [x] 一个可重复 Docker Slurm；
- [x] 一个成功 Run；
- [x] 一个失败 Run；
- [x] 一个取消 Run；
- [x] 一个可验证 Capsule；
- [x] 一个可加载的 CapabilityProfile；
- [x] 一次 API/Worker 重启恢复；
- [x] Docker 多用户权限测试通过。

Phase 0B 通过条件：

- [ ] 应用节点 + Docker 宿主机完成演示闭环；
- [ ] HTTPS 或校园网受控风险记录；
- [ ] 持久化和备份策略可执行。

Phase 0C 不阻塞主线。
