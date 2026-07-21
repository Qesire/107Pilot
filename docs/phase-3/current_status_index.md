# 107Pilot 当前状态索引

快照日期：2026-07-20
当前环境：本机 D0/D1
当前主线：模拟 Slurm 阶段封版候选。已验证发布 revision：`c42f904384792633b611ee1bb9bc4b6b25733080`；验收证据：source acceptance 12/12 PASS + runtime acceptance 10/10 PASS（同一 SHA，seal mode）。round-4..7 P1 已闭环；round-8 P1（baseline attribution fail-closed / lease-aware baseline 预算）已闭环；round-8 P2-1..P2-4 已闭环；round-9 P2-5（Dockerfile digest pin + uv.lock）部分实现但跨时间/跨主机同 SHA 可复现构建 **未成立**（round-11 审计纠正）。round-11 P1-1（baseline stat/OSError 区分 ENOENT）/ P1-2（提交阶段续租或单一绝对 deadline）/ P1-3（真正构建并绑定 Slurm source image + 移除 /dev/urandom + 锁定 APT/uv + 双 clean-build digest 对比）为收敛中硬化项。当前判定：c42f904 已构建 bundle GO；模拟 Slurm 功能闭环 GO；baseline 异常归因与多 dispatcher 租约条件 GO；跨时间/跨主机同 SHA 可复现构建 NO-GO。

## 权威入口

- 自动工程任务：[`automated_execution_plan_20260716.md`](automated_execution_plan_20260716.md)
- 环境与用户参与边界：[`revised_execution_plan_20260716.md`](revised_execution_plan_20260716.md)
- 用户反馈方式：[`phase3d_user_feedback_protocol.md`](phase3d_user_feedback_protocol.md)
- 历史完整设计与阶段记录：[`current_actual_and_execution_plan.md`](current_actual_and_execution_plan.md)

发生冲突时，按以上顺序解释当前执行状态；历史 review 只证明其评审日期当时的事实。

## 最新自动基线

| 项目 | 当前事实 |
| --- | --- |
| Python 源文件 | 76 |
| Python 测试文件 | 81 |
| Recipe 模板 | 6 个；含 structured preflight、GPU shard array、fail-closed merge gate |
| 已知错误规则 | 37 条 |
| Ruff | passed（src/tests/scripts/simulator） |
| mypy strict | 76 source files passed |
| Python 测试 | 750 passed，13 PostgreSQL integration skipped，11 subtests passed |
| Vitest | 12 files / 93 tests passed |
| Playwright test:ui | 14/14 passed（Market→Adopt→Studio→Preflight→Run→Evidence，CI 阻塞门禁） |
| Web production build | passed（static bundle 已与源码同步，CI 含 drift 检查） |
| Compose contracts | base/competition/CPU-RC/slurm-host/app-node passed |
| Source acceptance (c42f904, seal mode) | 12/12 PASS：uv_sync、npm_ci、ruff、mypy、pytest、typecheck、vitest、playwright、build、static_drift、compose_config、sync_drift |
| Runtime acceptance (c42f904, seal mode) | 10/10 PASS：manifest_validate、import_images、start_stack、compose_readiness、check_cpu_rc、auto_capsule、rule_remediation、restart_recovery、image_binding、report |
| Local seal acceptance (c42f904) | c42f904 验收证据已全绿（source 12/12 + runtime 10/10，同一 SHA，seal mode）；round-4..7 P1 + round-8 P1 + round-8 P2-1..P2-4 已闭环；round-9 P2-5（Dockerfile digest pin + uv.lock）部分实现但跨时间/跨主机同 SHA 可复现构建未成立（round-11 审计纠正）；当前判定：已构建 bundle GO / 模拟 Slurm 功能闭环 GO / baseline 异常归因与多 dispatcher 租约条件 GO / 跨时间跨主机同 SHA 可复现构建 NO-GO；round-11 P1-1/P1-2/P1-3 为收敛中硬化项 |
| GitHub CI | 当前可用 connector 未返回 `c42f904` 的 workflow run；本地 seal 验收已全绿，GitHub Actions 独立验证待 workflow run ID/check URL 补录 |

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
3. CPU-RC 早期 revision 已在 S1 (8C/16G VM) 部署并通过 G3 功能链；发布 revision `c42f904384792633b611ee1bb9bc4b6b25733080` 的 source acceptance (12/12) + runtime acceptance (10/10) 已在同一 SHA 全绿（seal mode）；round-4..7 P1 + round-8 P1 + round-8 P2-1..P2-4 已闭环；round-9 P2-5（Dockerfile digest pin + uv.lock）部分实现但跨时间/跨主机同 SHA 可复现构建未成立（round-11 审计纠正：build 脚本 tag 可变镜像而非构建 Slurm Dockerfile / /dev/urandom 随机输入 / uv 浮动且留在最终镜像）；当前判定：已构建 bundle GO / 模拟 Slurm 功能闭环 GO / baseline 异常归因与多 dispatcher 租约条件 GO / 跨时间跨主机同 SHA 可复现构建 NO-GO；round-11 P1-1/P1-2/P1-3 为收敛中硬化项；
4. 真实身份和真实 107 均不在当前已验证能力内，亦不属于本阶段验收范围。
