# 107Pilot 当前状态索引

快照日期：2026-07-16  
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
| Python 源文件 | 63 |
| Python 源码 | 26,791 行 |
| Python 测试文件 | 63 |
| Python 测试源码 | 14,782 行 |
| Web 源码 | 4,458 行 |
| Python/Shell 脚本 | 93 |
| Python 测试 | 496 collected；最近完整 CI 492 passed，Agent 增量 18 passed |
| Ruff | passed |
| mypy strict | 63 source files passed |
| Web typecheck | passed |
| Vitest | 8 files / 54 tests passed |
| Web production build | 1,913 modules built |
| Docker core | MariaDB/API/Web/Worker/Slurm core healthy |
| Compose contracts | base/competition/slurm-host/app-node passed |

Python 测试中有 6 项需要绑定本机回环端口验证 SSE/HTTP wire behavior。最近一次完整 CI 在 Agent UI 切片前为 492 passed；该切片新增/修改的 remediation、API、ASGI 定向测试 18 passed，前端 54 passed，Docker Web smoke 与 `pilot-browser` live 回归通过。当前执行环境的完整 CI 再运行因工具审批额度暂停，不应误写为代码失败。项目已在 `pyproject.toml` 固定 `src` import path，不再依赖隐式 `PYTHONPATH`。

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
| Phase 3G | ControlRepository PostgreSQL parity、Run/collection/Agent outbox fencing、恢复与可观测性底座完成；业务 Store PG 接线、安全收口仍进行中 |
| Phase 3H | 本地比赛链路部分完成；真实 107 仅只读兼容探测 |

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
2. LLM/SSE 专项指标、持久 request-to-domain trace、Prometheus retention 与 firing 演练仍未完成；
3. proxy trust、rate/body/response limit、CSP/CSRF 和供应链扫描仍需 3G-4 安全收口；
4. 当前 simulator 宣称模拟 GPU/64 CPU，不适合直接作为纯 CPU VM 发布配置；
5. VM、真实身份和真实 107 均不在当前已验证能力内。
