# 06. 实施顺序、差距与验收

## 1. 当前能力审计

| 领域 | 已有基础 | 设计差距 |
|---|---|---|
| Slurm 模拟器 | Docker/command gateway、QoS/profile、Run/Worker/Evidence live 流程 | 保持与最新真实 probe 的行为差异矩阵；不得引入 simulator-only 产品分支 |
| Run 与恢复 | Run 状态机、outbox、fencing、retry、Evidence/Capsule | 真实 backend 尚未接入同一服务 builder |
| 市场 | draft/review/release/adoption/strict verification、React 市场页 | 缺少普通成功 Run 的直接发布与统一 MarketItem read model |
| Studio | Contract schema、预检、准备、提交、Agent patch 建议 | 需消费 publication lineage 和真实连接状态 |
| Agent | Evidence-bound explanation、policy、approval、派生 Run、workbench | 缺少 typed real-backend 工具和 RepairTicket 聚合 |
| Terminal | 四个固定模拟器诊断命令 | 真实 SSH 需要 Relay 投影，不能开放 shell |
| 真实 107 | CLI/环境/compute probe、成功/失败/取消实证 | 缺少正式 `SshSlurmBackend`、session store、EvidenceTransport、API/Worker 接线 |
| 代码上下文 | 受限只读错误窗口、SSH ControlMaster 原型 | 缺 ArtifactManifest、RepairTicket、每用户 session 隔离 |
| 身份/存储 | trusted proxy、SQLite 主路径、Postgres parity 基础 | 校园身份、Slurm identity mapping、全领域 Postgres 接线未完成 |

## 2. 里程碑

### M0 — 领域与 API 基线：成功作业发布

目标：在当前高保真模拟 Slurm 上完成普通市场闭环。

后端：

1. `RunPublication` SQLite + PostgreSQL migration；
2. publication store/service/read service；
3. eligibility 字段、create/list/detail/adopt/withdraw API；
4. owner、visibility、request key、read-model 脱敏测试；
5. 保持旧 TemplateMarket API 完全兼容。

前端：

1. Run 成功后的 owner-only 发布 CTA；
2. 发布确认 sheet；
3. `MarketItem` union、市场列表与详情；
4. adopt 进入 Studio；
5. 空状态、forbidden、withdraw 与 query invalidation。

验收：一条模拟 Slurm 成功 Run 可以被 owner 发布；另一个用户可浏览、采用并得到自己的 Contract；失败/取消 Run 不可发布。

### M1 — 真实 SSH backend 的受控纵向切片

目标：把已经人工验证过的真实 107 success/failure/cancel 操作接入 API/Worker，而不是继续依赖脚本。

后端：

1. `SshRelayClient`、session state、owner/target/root policy；
2. `SshSlurmBackend`：prepare directory、write script、submit、query、cancel；
3. `SshEvidenceTransport`：accounting、日志尾部、输出清单；
4. `real107-ssh` API/Worker config builder；
5. auth expiry 和 idempotency/reconciliation fault injection。

前端：连接状态、`AUTH_REQUIRED`、真实 Run 的事实状态与同一 Run UI。没有单独的“真实平台版页面”。

验收：在一个明确授权的私有远端目录中，API/Worker 完成 success、exit 42、cancel 与 auth-required 四个场景，并生成与模拟器同结构的 Run/Evidence read model。

### M2 — 外置 Agent 的修复交接

目标：Agent 能在不接管完整代码仓库的前提下完成故障到修复验证的闭环。

后端：`ArtifactManifest`、`RepairTicket`、action-plan read model、Relay typed read tools、审批后派生 Run。

前端：现有 Agent workbench 增加 repair ticket、数据披露提示和新旧 Run 对比。

验收：一个真实或模拟 traceback 场景能生成 ticket；本地修改后，新 ArtifactManifest 关联派生 Run，Evidence 显示前后差异。

### M3 — 门户生产化

目标：从单用户/比赛 VM 进入可申请正式接入的门户。

内容：学校身份、用户到 Slurm session 映射、Pilot Link、多用户 session 隔离、完整 PostgreSQL 接线、备份恢复、长期指标、HTTPS/限流和操作 runbook。

## 3. 先后约束

```text
M0 市场闭环
      │
      ├─ M1 真实 backend 与 Evidence
      │       │
      │       └─ M2 Agent action / repair handoff
      │
      └─ UI 可先在模拟 Slurm 中完整演示
              │
              └─ M3 多用户生产化
```

M0 不等待真实 SSH；但 M1 不得通过复制或改写 Run/Worker 状态机实现。M2 不得在 M1 前把 LLM 接到任意 SSH shell。

## 4. 自动化验收矩阵

| 层级 | 必测内容 |
|---|---|
| 单元 | publication eligibility、visibility、adoption lineage、SSH argv validation、parser、redaction、Agent policy |
| 服务 | store migration、request-key 幂等、outbox/fence、session state transition |
| HTTP | owner/forbidden、error envelope、read-model disclosure、pagination、connection state |
| 前端 | union decoding、发布确认、private-content 空状态、Studio adoption、Agent action approval |
| 模拟器 live | `Market → Adopt → Studio → Run → Evidence → Publish`；成功/失败/取消/恢复 |
| 真实 107 授权 smoke | submit/query/cancel/evidence/auth expiry；只使用私有目录与短作业 |
| 安全 | prompt injection logs、path traversal、symlink、跨用户 Run、过期 session、重复 submit |

## 5. 发布门禁

每个里程碑完成前必须满足：

- ruff、mypy、Python tests、TypeScript typecheck、Vitest、production build；
- 对应 API contract tests 与 browser flow；
- 模拟器 compose configuration 与 live smoke；
- `git diff --check`；
- 新增 schema 的 SQLite 与 PostgreSQL migration test；
- 文档中声明的真实平台结论有 probe 或授权 smoke artifact 支持。

真实 107 的访问失效、MFA 未续期或无法获得授权不是模拟器功能失败；系统必须把它如实报告为 connection/auth 状态。

## 6. 完成定义

达到“门户核心闭环”时，普通用户可以在浏览器中：

1. 看到别人主动分享的一次成功作业；
2. 采用为自己的 Contract，替换自己的路径/参数；
3. 在模拟 Slurm 或真实 107 的同一工作流中提交和观察；
4. 查看 Evidence、诊断和 Agent 建议；
5. 成功后自主勾选发布；
6. 不必把完整代码仓库、密码或 OTP 交给 107Pilot。

