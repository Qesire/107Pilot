# 107Pilot 智能门户实施设计

日期：2026-07-26  
状态：设计基线；本目录不改变已上线的模拟器主线，也不把未实现的真实 107 接入表述为既成能力。

## 定位

107Pilot 的目标产品是**真实 107 Slurm 的外置智能 Web 门户**。它运行在真实集群之外，通过用户授权的连接操作用户自己的 Slurm 资源；完整代码仓库、私有数据和本地代码 Agent 留在用户控制的环境。

产品主线是：

```text
市场中的成功作业 → 私有 Contract → Slurm Run → Evidence / Capsule
                 ↘          外置 Agent          ↗
```

高保真模拟 Slurm 是开发、验收和演示环境中的 Slurm 实现。对 107Pilot 的领域逻辑而言，它与真实 107 必须遵守同一 `SlurmBackend` 契约；不得为模拟器另写市场、Run、Agent 或页面流程。只有审计来源和连接配置可以不同。

## 本轮固定决策

1. 107Pilot 的 Agent 部署在门户控制面，不部署在真实 107 平台。
2. 普通市场分享的唯一业务门槛是：来源 Run 已 `SUCCEEDED`、退出码为零，且所有者明确勾选发布。
3. 市场不判断作业是否可迁移、是否包含长代码、是否依赖私有目录或是否可被他人完整复现；发布者自行决定。
4. 完整代码、数据、私有工作目录、凭据和原始日志不得因市场发布而自动公开。
5. 官方模板、课程模板可以继续使用现有的审核和严格验证路径；普通成功作业分享不经过该路径。
6. LLM 不拥有任意 Shell。它提出类型化动作，确定性策略、审批和 Slurm 适配器决定是否执行。
7. 真实 107 接入是可替换后端，不是第二套产品或前端模式。

## 设计文档地图

| 文档 | 解决的问题 | 主要读者 |
|---|---|---|
| [00-system-boundaries.md](00-system-boundaries.md) | 系统边界、信任边界、模拟器与真实平台的关系 | 全体 |
| [01-domain-model.md](01-domain-model.md) | Contract、Run、市场发布、Evidence、Agent 的领域模型与迁移 | 后端、产品 |
| [02-slurm-backend-and-ssh.md](02-slurm-backend-and-ssh.md) | 真实 SSH SlurmBackend、会话和 Evidence transport | 后端、运维 |
| [03-market-and-sharing.md](03-market-and-sharing.md) | 成功作业市场的发布、读取、采用和 UI 语义 | 前端、后端、产品 |
| [04-external-agent-and-local-code.md](04-external-agent-and-local-code.md) | 外置 Agent、Repair Ticket、本地代码边界 | Agent、后端 |
| [05-web-api-coordination.md](05-web-api-coordination.md) | 页面、read model、API、缓存和交互顺序 | 前端、后端 |
| [06-rollout-and-acceptance.md](06-rollout-and-acceptance.md) | 分阶段实施、测试矩阵和完成标准 | 全体 |
| [07-m0-implemented-contract.md](07-m0-implemented-contract.md) | 已落地的 M0 存储、HTTP 与前端契约，以及与目标设计的差异 | 前端、后端、测试 |
| [08-m1-implemented-contract.md](08-m1-implemented-contract.md) | 已落地的受控 SSH Relay、真实 Slurm/Evidence 接线及剩余 live 验收 | 前端、后端、运维 |

## 当前实现基线

下列能力已存在，应复用而非重写：

- `Contract → Run → Worker reconcile → Evidence/Capsule → Diagnosis → approved action` 的领域闭环；
- `SlurmBackend`、Docker command gateway、REST、in-memory 和 demo 后端；
- 模板草稿、审核、不可变 release、adoption 与 verification；
- React 市场、Studio、Run、Evidence、Agent、受控 Terminal 页面；
- 真实 107 的 SSH 探测和成功/失败/取消作业实证。

当前缺口不是“再写一套门户”，而是把这些已有组件按本目录的契约接成：

```text
普通成功 Run 发布路径
        +
真实 SSH backend / evidence transport / 会话治理
        +
外置 Agent 的受限工具闭环
```

具体的现状与目标差异以 [06-rollout-and-acceptance.md](06-rollout-and-acceptance.md) 为准。

## 2026-07-26 已落地的 M0 切片

`RunPublication` 的 SQLite/PostgreSQL 表、发布/列表/详情/采用/撤回 API、市场页双分区和 Run 发布确认表单已经实现。它使用与其他 Run 相同的领域对象和 `SlurmBackend`；测试通过标准 in-memory Slurm 契约驱动成功作业，未添加任何模拟器专用业务分支。

精确 API、公开字段、Contract lineage 与统一 MarketItem endpoint 见
[07-m0-implemented-contract.md](07-m0-implemented-contract.md)。

## 2026-07-26 M1 实现进展

受控 ControlMaster Relay、`SshSlurmBackend`、`SshEvidenceTransport`、
连接状态 API 与统一前端状态已经落地并通过本地 contract 测试。真实 107
success、exit 42、cancel 和 auth-required 四场景仍需在既有 MFA session
可访问时完成 live acceptance，不能用 mock 结果替代。精确边界见
[08-m1-implemented-contract.md](08-m1-implemented-contract.md)。
