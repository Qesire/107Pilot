# 107Pilot 当前状态索引

快照日期：2026-07-18
当前环境：本机 D0/D1
当前主线：自动执行至 CPU-only VM 部署准备完成。

## 权威入口

- 自动工程任务：[`automated_execution_plan_20260716.md`](automated_execution_plan_20260716.md)
- 环境与用户参与边界：[`revised_execution_plan_20260716.md`](revised_execution_plan_20260716.md)
- 用户反馈方式：[`phase3d_user_feedback_protocol.md`](phase3d_user_feedback_protocol.md)
- 历史完整设计与阶段记录：[`current_actual_and_execution_plan.md`](current_actual_and_execution_plan.md)

发生冲突时，按以上顺序解释当前执行状态；历史 review 只证明其评审日期当时的事实。

## 最新自动基线

| 项目 | 当前事实 |
| --- | --- |
| Python 源文件 | 73 |
| Python 源码 | 33,210 行 |
| Python 测试文件 | 74 |
| Python 测试源码 | 18,253 行 |
| Web 源码 | 5,235 行 |
| Python/Shell 脚本 | 105 |
| Python 测试 | 594 passed，13 PostgreSQL integration skipped，2 subtests passed |
| Ruff | passed |
| mypy strict | 73 source files passed |
| Vitest | 10 files / 64 tests passed |
| Web production build | 1,914 modules built |
| Docker core | CPU-RC MariaDB/API/Web/Worker/Slurm healthy；单 Slurm worker |
| Compose contracts | base/competition/CPU-RC passed |

完整测试已在允许本机回环 socket 的执行环境通过；受限沙箱内同一套代码仅有 7 个回环 HTTP 测试因 `PermissionError` 不能绑定端口，不是代码失败。项目已在 `pyproject.toml` 固定 `src` import path，不依赖隐式 `PYTHONPATH`。

## 阶段状态

| 阶段 | 当前判定 |
| --- | --- |
| Phase 0–2 | 既定本机范围完成 |
| Phase 3A | owner-scoped read model/SSE/lineage 已评审 |
| Phase 3B | 平台事实、entitlement、preflight 已评审；工程治理基线已收敛 |
| Phase 3C | Template/Market/Adoption 本地纵向链路已评审 |
| Phase 3D | 工程纵向链路已评审；用户反馈无人数门禁、非阻塞 |
| Phase 3E | Remediation 事件/输入/takeover、专用 action、provider-neutral LLM 安全 benchmark 已完成 |
| Phase 3F | Run timeline/lineage/compare、安全命令与 Agent Evidence/diff/执行/前后结果工作台已完成 |
| Phase 3G | ControlRepository PostgreSQL parity/运行时选择、Run/collection/Agent outbox fencing、恢复、持久 trace、LLM/SSE metrics 与安全基线完成；全领域业务 Store PG parity 仍进行中 |
| Phase 3H | CPU-only 本地候选功能闭环完成；离线包收尾中；真实 107 仅只读历史兼容探测 |

## 环境声明

- D0：本机单元、契约、权限、迁移测试；
- D1：本机 Docker Slurm simulator live behavior；
- S1：未来 8C/16G CPU VM 固定发布候选，当前未部署；
- R0：开发者个人 SSH 辅助只读 probe；
- R1：107Pilot 真实平台集成，当前不具备条件；
- 校园多用户生产：NO-GO。

Docker、fake/replay LLM 和本地 command gateway 的结果不得表述为真实 107、真实模型或校园生产能力。

## 当前工程风险

1. Run、Remediation、Template 等业务 Store 仍以 SQLite 为主，尚未完成全领域 PostgreSQL parity/接线；
2. Prometheus 长期 retention/firing 与在线供应链扫描仍需目标运维/CI 环境验证；
3. CPU-RC 已与 GPU simulator 分离，但尚未在实际 8C/16G VM 上验证资源和磁盘性能；
4. VM、真实身份和真实 107 均不在当前已验证能力内。
