# 107Pilot 当前状态索引

快照日期：2026-08-19

## 2026-08-19 A1 完成重基线

- `pilot-agentd` A0 已完成：独立 Node 22.19.0 / Pi 0.84.1 服务、campus OpenAI-compatible streaming、deterministic faux、严格 Python↔TypeScript Turn/事件/错误合同、abort/checkpoint restore，以及 explain/Contract patch/remediation 统一调用链。
- **A1 只读持久 Agent Turn 已完成本机 D0/D1 验收**：版本化 v2 合同、SQLite/PostgreSQL Store 合同、owner-scoped Session/Turn HTTP、持久事件重放、outbox Worker 恢复、取消、幂等与 fencing、七个有界只读工具、独立 capability secret 和私有 Tool Gateway 均已接通。实施计划与完成清单：[`../superpowers/plans/2026-08-14-pilot-agent-a1-readonly-turns.md`](../superpowers/plans/2026-08-14-pilot-agent-a1-readonly-turns.md)。
- A1 D1 纵向证据：HTTP → outbox Worker → Agentd → Tool Gateway → Store 固定轨迹产生 16 条连续持久事件，调用 `run_get`、`run_log_read`、`evidence_read` 各一次，共返回 942 bytes；Alice/Bob 隔离、浏览器断点重放、单 Turn、工具幂等和 stale-fence 拒绝均通过。
- A1 故障证据：API Turn commit、Worker outbox claim、Agentd 单个工具结果持久化、browser event N 四个屏障均通过 deterministic D0 注入和真实容器 stop/start/reconnect；100 idle Session / 10 concurrent faux Turn 资源与释放测试通过。D1 观测到共享 Agentd 约 28–41 MiB，作为后续容量基线，不作为生产阈值。
- A1 完成门禁：Agentd 14 files / 204 tests、Python 1139 passed / 17 skipped / 30 subtests、Ruff、112 个 mypy 源文件、base/competition/slurm-host/app-node Compose 渲染、A1 smoke/fault 全绿。PostgreSQL live integration 仍随未配置的本地 PostgreSQL 测试一起 skip，不将其表述为校园生产实测。
- 下一阶段顺序仍为 A2 隔离工作区编辑、A3 异步 Slurm 验证、A4 安全发布/正式运行、A5 修复与市场 Agent；A2 尚未启动，需单独实施计划与 D0/D1 review gate。
- Runtime Watch 与资源观测作为独立事实提供者推进；A1 只消费已有 Store，不夹带新 Slurm 采集。全领域 PostgreSQL parity、真实身份和真实 107 继续作为生产门禁，不冒充本地 A1 完成条件。

当前环境：本机 D0/D1
当前主线边界：A1 只读持久 Agent Turn 已完成；A2 尚未开始。
已封版历史基线：模拟 Slurm 阶段已验证发布 revision `d3ceb4cd43b77c7cee9d10768db7ada324b02ed0`；验收证据：source acceptance 12/12 PASS + runtime acceptance 10/10 PASS（同一 SHA，seal mode）。round-4..7 P1 已闭环；round-8 P1（baseline attribution fail-closed / lease-aware baseline 预算）已闭环；round-8 P2-1..P2-4 已闭环；round-11 P1-1（baseline stat/OSError 区分 ENOENT，其他错误 → status=error + error_code）/ P1-2（提交阶段 renew_outbox + 续租，unparseable lease → fail-closed）/ P1-3（build 脚本真正构建 Slurm Dockerfile + 移除 /dev/urandom + 锁定 uv/slurm-wlm + 双 clean-build rootfs content hash 对比）已闭环。当前判定：d3ceb4c 已构建 bundle GO；模拟 Slurm 功能闭环 GO；baseline 异常归因与多 dispatcher 租约 GO；app 镜像跨 build 可复现性 GO（rootfs content hash 双 build 一致）；slurm 镜像非 slurm-wlm apt 包仍有残余漂移（base digest + slurm-wlm 版本已锁，文档化为 practical-vs-mathematical 权衡）。

## 权威入口

- 自动工程任务：[`automated_execution_plan_20260716.md`](automated_execution_plan_20260716.md)
- 环境与用户参与边界：[`revised_execution_plan_20260716.md`](revised_execution_plan_20260716.md)
- 用户反馈方式：[`phase3d_user_feedback_protocol.md`](phase3d_user_feedback_protocol.md)
- 历史完整设计与阶段记录：[`current_actual_and_execution_plan.md`](current_actual_and_execution_plan.md)

发生冲突时，按以上顺序解释当前执行状态；历史 review 只证明其评审日期当时的事实。

## 最新自动基线

| 项目 | 当前事实 |
| --- | --- |
| Python 源文件 | 112 |
| Python 测试文件 | 115 |
| Recipe 模板 | 6 个；含 structured preflight、GPU shard array、fail-closed merge gate |
| 已知错误规则 | 37 条 |
| Ruff | passed（src/tests/scripts） |
| mypy strict | 112 source files passed |
| Python 测试 | 1139 passed，17 skipped，30 subtests passed |
| Agentd Vitest | 14 files / 204 tests passed |
| Playwright test:ui | 14/14 passed（Market→Adopt→Studio→Preflight→Run→Evidence，CI 阻塞门禁） |
| Web production build | passed（static bundle 已与源码同步，CI 含 drift 检查） |
| Compose contracts | base/competition/CPU-RC/slurm-host/app-node passed |
| Source acceptance (d3ceb4c, seal mode) | 12/12 PASS：uv_sync、npm_ci、ruff、mypy、pytest、typecheck、vitest、playwright、build、static_drift、compose_config、sync_drift |
| Runtime acceptance (d3ceb4c, seal mode) | 10/10 PASS：manifest_validate、import_images、start_stack、compose_readiness、check_cpu_rc、auto_capsule、rule_remediation、restart_recovery、image_binding、report |
| Local seal acceptance (d3ceb4c) | d3ceb4c 验收证据已全绿（source 12/12 + runtime 10/10，同一 SHA，seal mode）；round-4..7 P1 + round-8 P1 + round-8 P2-1..P2-4 + round-11 P1-1（baseline stat/OSError 区分 ENOENT）/ P1-2（提交阶段续租 + unparseable lease fail-closed）/ P1-3（build 脚本真正构建 Slurm Dockerfile + 移除 /dev/urandom + 锁定 uv/slurm-wlm + 双 clean-build rootfs content hash 对比）已闭环；当前判定：已构建 bundle GO / 模拟 Slurm 功能闭环 GO / baseline 异常归因与多 dispatcher 租约 GO / app 镜像跨 build 可复现性 GO（rootfs content hash 双 build 一致）/ slurm 镜像非 slurm-wlm apt 包仍有残余漂移（base digest + slurm-wlm 版本已锁，practical-vs-mathematical 权衡） |
| GitHub CI | 当前可用 connector 未返回 `d3ceb4c` 的 workflow run；本地 seal 验收已全绿，GitHub Actions 独立验证待 workflow run ID/check URL 补录 |

完整测试已在允许本机回环 socket 的执行环境通过；受限沙箱内同一套代码仅有 7 个回环 HTTP 测试因 `PermissionError` 不能绑定端口，不是代码失败。项目已在 `pyproject.toml` 固定 `src` import path，不依赖隐式 `PYTHONPATH`。

## 阶段状态

| 阶段 | 当前判定 |
| --- | --- |
| Phase 0–2 | 既定本机范围完成 |
| Phase 3A | owner-scoped read model/SSE/lineage 已评审 |
| Phase 3B | 平台事实、entitlement、preflight 已评审；工程治理基线已收敛 |
| Phase 3C | Template/Market/Adoption 本地纵向链路已评审 |
| Phase 3D | 工程纵向链路已评审；用户反馈无人数门禁、非阻塞 |
| Phase 3E | Remediation 事件/输入/takeover、专用 action、provider-neutral LLM 安全 benchmark 完成；provider 创建时序与 Worker LLM 环境已接通 |
| Phase 3F | Run timeline/lineage/compare、安全命令与 Agent Evidence/diff/执行/前后结果工作台已完成 |
| Phase 3G | ControlRepository PostgreSQL parity/运行时选择、Run/collection/Agent outbox fencing、恢复、持久 trace、LLM/SSE metrics 与安全基线完成；全领域业务 Store PG parity 仍进行中 |
| Phase 3H | CPU-only 固定 revision 离线候选完成；systemd 部署声明化；本阶段最终环境为 Docker Slurm simulator，真实 107 不属于本阶段验收范围 |

## 环境声明

- D0：本机单元、契约、权限、迁移测试；
- D1：本机 Docker Slurm simulator live behavior；
- S1：8C/16G CPU VM 已部署并 G3 功能链通过（见 `s1_vm_deployment_evidence_20260718.md`）；最新 SHA 重部署验收为后续 VM 工作项；
- R0：开发者个人 SSH 辅助只读 probe；
- R1：107Pilot 真实平台集成，当前不具备条件；
- 校园多用户生产：NO-GO。

Docker、fake/replay LLM 和本地 command gateway 的结果不得表述为真实 107、真实模型或校园生产能力。系统通过 backend 与 capability profile 抽象了 Slurm 差异，本阶段在 Docker Slurm simulator 上完成闭环验证；真实集群主要替换连接、认证、资源与文件系统配置，但尚未纳入本阶段实测。

## 当前工程风险

1. Run、Remediation、Template 等业务 Store 仍以 SQLite 为主，尚未完成全领域 PostgreSQL parity/接线；
2. Prometheus 长期 retention/firing 与在线供应链扫描仍需目标运维/CI 环境验证；
3. CPU-RC 早期 revision 已在 S1 (8C/16G VM) 部署并通过 G3 功能链；发布 revision `d3ceb4cd43b77c7cee9d10768db7ada324b02ed0` 的 source acceptance (12/12) + runtime acceptance (10/10) 已在同一 SHA 全绿（seal mode）；round-4..7 P1 + round-8 P1 + round-8 P2-1..P2-4 + round-11 P1-1（baseline stat/OSError 区分 ENOENT）/ P1-2（提交阶段续租 + unparseable lease fail-closed）/ P1-3（build 脚本真正构建 Slurm Dockerfile + 移除 /dev/urandom + 锁定 uv/slurm-wlm + 双 clean-build rootfs content hash 对比）已闭环；当前判定：已构建 bundle GO / 模拟 Slurm 功能闭环 GO / baseline 异常归因与多 dispatcher 租约 GO / app 镜像跨 build 可复现性 GO（rootfs content hash 双 build 一致）/ slurm 镜像非 slurm-wlm apt 包仍有残余漂移（base digest + slurm-wlm 版本已锁，practical-vs-mathematical 权衡）；
4. 真实身份和真实 107 均不在当前已验证能力内，亦不属于本阶段验收范围。
