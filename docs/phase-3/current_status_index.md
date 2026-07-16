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
| Phase 3E | persistent RemediationSession、预算、lease/CAS、规则闭环、审批执行与 evaluator 核心已实现；事件/输入 API、动作覆盖与 LLM benchmark 待补 |
| Phase 3F | Run Evidence 已有；Agent queue/detail/预算/批准/拒绝/取消/执行第一版已通过本地浏览器回归，Run compare 与安全命令待补 |
| Phase 3G | SQLite 单机能力已有；PostgreSQL/多实例/生产控制面未完成 |
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

1. Remediation list/detail 仍缺 keyset pagination、ETag、事件补读和安全的 `awaiting_input` 恢复协议；
2. 受控 action 仍需扩大专用 probe/diff/rollback 覆盖，LLM proposal benchmark 尚未完成；
3. Run/Agent 工作台尚缺前后 Run/Evidence/outputs 对比、安全 native command 和完整错误状态回归；
4. 当前存储以 SQLite 为主，多 Worker 一致性、outbox、恢复和可观测性未完成；
5. 当前 simulator 宣称模拟 GPU/64 CPU，不适合直接作为纯 CPU VM 发布配置；
6. VM、真实身份和真实 107 均不在当前已验证能力内。
