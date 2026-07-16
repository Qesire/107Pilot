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
| Python 源文件 | 57 |
| Python 源码 | 24,443 行 |
| Python 测试文件 | 59 |
| Python/JS 测试源码 | 14,462 行 |
| Web 源码 | 3,719 行 |
| Python/Shell 脚本 | 93 |
| Python 测试 | 468 passed |
| Ruff | passed |
| mypy strict | 57 source files passed |
| Web typecheck | passed |
| Vitest | 4 files / 15 tests passed |
| Web production build | 1,912 modules built |
| Docker core | MariaDB/API/Web/Worker/Slurm core healthy |
| Compose contracts | base/competition/slurm-host/app-node passed |

Python 测试中有 6 项需要绑定本机回环端口验证 SSE/HTTP wire behavior；受限命令沙箱会拒绝 socket，但正常本机或 CI runner 上的完整测试为 468 passed。项目已在 `pyproject.toml` 固定 `src` import path，不再依赖隐式 `PYTHONPATH`。

## 阶段状态

| 阶段 | 当前判定 |
| --- | --- |
| Phase 0–2 | 既定本机范围完成 |
| Phase 3A | owner-scoped read model/SSE/lineage 已评审 |
| Phase 3B | 平台事实、entitlement、preflight 已评审；工程治理正在收敛 |
| Phase 3C | Template/Market/Adoption 本地纵向链路已评审 |
| Phase 3D | 工程纵向链路已评审；用户反馈无人数门禁、非阻塞 |
| Phase 3E | 尚未实现 RemediationSession；当前下一产品主线 |
| Phase 3F | Run Evidence 已有；Agent/Terminal 工作台未完成 |
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

1. `api/http_app.py` 约 3,304 行，需在 Phase 3E 路由进入前拆分；
2. 前端仅 15 项 Vitest，核心错误状态与纵向路径覆盖不足；
3. 尚无 RemediationSession、多轮 evaluator 和受控修复闭环；
4. 当前存储以 SQLite 为主，多 Worker 一致性和 outbox 未完成；
5. 当前 simulator 宣称模拟 GPU/64 CPU，不适合直接作为纯 CPU VM 发布配置；
6. VM、真实身份和真实 107 均不在当前已验证能力内。
