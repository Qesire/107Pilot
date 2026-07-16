# 107Pilot

107Pilot 是面向本科生算力平台的证据化 Slurm 实验工作流系统。

当前实施状态：

```text
Phase 3A–3D 工程纵向链路已完成本地 review
当前自动工程主线：Phase 3E Remediation Engine → Phase 3F 工作台 → 可本机完成的 Phase 3G → CPU-only 发布候选
```

当前实际状态与下一阶段执行计划：

```text
docs/phase-3/revised_execution_plan_20260716.md
docs/phase-3/automated_execution_plan_20260716.md
docs/phase-3/current_status_index.md
docs/phase-3/phase3d_user_feedback_protocol.md
docs/phase-3/current_actual_and_execution_plan.md  # 历史完整设计与阶段记录
```

## 当前基线

当前开发与验收环境边界为：

```text
本机
→ 运行 107Pilot Web/API/Worker 与 Docker Slurm simulator
→ 完成开发、自动验证和 CPU-only 发布候选

8C/16G CPU VM
→ 只在共享验收或演示需要时部署固定候选

真实 107
→ 当前仅有开发者辅助只读兼容探测，不是系统接入
```

VM 不作为第二台开发机。真实 107 平台不作为当前自动工程依赖；没有正式准入时不得宣称 submit/cancel/evidence 已验证。

## 当前文档入口

```text
docs/phase-1/implementation_plan.md
docs/phase-0/docker_mainline_plan.md
docs/phase-0/competition_deployment_plan.md
docs/phase-0/real_platform_compatibility_plan.md
docs/phase-0/server_questions.md
docs/phase-1/production_access_report.md
docs/phase-1/auth_decision.md
docs/phase-1/evidence_transport_decision.md
docs/phase-1/submission_strategy.md
```

## 设计基线

设计树位于：

```text
/home/knowingthesea/文档/107/design_v1.4
```

本仓库实施必须遵守：

- 用户身份不由前端任意 username 决定；
- Slurm 控制面不等于文件访问权限；
- API 请求生命周期不承担长期作业跟踪；
- Docker 主线优先，真实 107 接入并行非阻塞；
- 平台配置必须通过 probe 或明确来源记录；
- token 不进入日志、数据库明文、Evidence、Capsule 或 Agent context。

## 当前优先级

```text
P0 Phase 3B 平台事实、API 模块化和工程治理
P0 Phase 3C 模板草稿、发布市场和采用闭环
P0 Phase 3D Contract Studio 与不丢失高级能力的产品界面
P0 Phase 3E Agent remediation session 与真实模型评测
P1 Phase 3F Run/Agent 工作台和受控终端协同
P1 Phase 3G 生产身份、PostgreSQL、恢复与可观测性
P1 Phase 3H 真实 107 渐进接入和比赛交付
```

## API 运行入口

安装 API 和开发依赖：

```bash
uv sync --extra api --extra dev
```

现有 stdlib server 仍承担完整 API 与 SSE；FastAPI 入口已公开 health/platform OpenAPI，
其他 GET/POST 在迁移期转发到同一领域适配层：

```bash
uv run uvicorn pilot107.api.asgi_app:create_app --factory --host 127.0.0.1 --port 8080
```
