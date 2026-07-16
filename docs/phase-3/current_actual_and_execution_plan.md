# 107Pilot 当前实际情况与下一阶段执行计划

> 2026-07-16 更新：在确认“真实平台仅有开发者个人临时 SSH 探测权限、系统尚未接入真实 107、8C/16G CPU VM 暂不作为开发机、用户反馈不设固定参与人数门禁”后，后续执行顺序与环境边界已由 [`revised_execution_plan_20260716.md`](revised_execution_plan_20260716.md) 重订。本文保留为此前完整设计与阶段记录；发生冲突时以重订版为当前执行基线。

日期：2026-07-16  
目标：比赛冠军级完整产品，并具备并入真实 107 平台成为生产设施的工程路径。

## 1. 执行结论

当前系统已经不是概念原型。它具备可验证的 Slurm 控制、Evidence、Diagnosis、
Contract/Recipe、Workflow 和受控 Agent 单动作执行闭环；Phase 3A 又补齐了产品读模型、
稳定分页、事件补读、SSE 和 Run 图谱。

但当前仍不能称为完整产品或生产设施，主要原因不是 Slurm 提交能力，而是以下三条产品链尚未闭合：

1. Worker 不会在诊断完成后自动创建、推进和评估一个多轮 remediation session；
2. Web 仍是 vanilla JavaScript 单页演示控制台；模板后端虽已闭环，高级 Contract、历史、市场、
   DAG、审批和终端仍未产品化；
3. 身份、数据库、多副本恢复、可观测性和真实平台准入尚未达到生产门槛。

因此下一阶段不应只做视觉重写，也不应继续在当前 2100 行以上的 HTTP handler 中追加所有功能。
推荐路线是先稳定平台事实和产品领域契约，再完成模板与 Studio，随后把 Agent 从“单动作”升级为
“有预算、有审批、有结果判定的修复会话”，最后完成生产治理。

## 2. 当前可验证基线

### 2.1 代码与测试

- Python 源码约 20,673 行，测试约 11,750 行，Python/Shell 脚本约 8,199 行；
- `ruff check src tests scripts`：通过；
- `mypy src/pilot107`：56 个源模块严格检查通过；
- `PYTHONPATH=src uv run --extra dev pytest -q`：439 项通过；
- Phase 3A 读模型定向测试：7 项通过；
- 1 万条 Run keyset pagination 使用 `idx_runs_owner_created`；
- 当前目录没有 `.git` 元数据，无法使用提交历史、diff 基线、blame 和 CI 分支保护。这是工程治理缺口，
  不是功能缺口，但必须在继续大规模开发前处理。

### 2.2 Docker 实测

2026-07-16 复核结果：

- Docker Slurm 25.11 simulator 的 `slurmctld`、`slurmdbd`、`slurmrestd`、MariaDB、
  login node 和两个 worker 均健康；
- `Students`、`debug` 等分区可见，两个 worker 为 idle；
- Phase 3A live smoke 真实提交成功：job `30`；
- Phase 3B evidence smoke：job `33`，采到 RUNNING 状态、节点和终态 account/QoS；
- Phase 3B platform/entitlement smoke：job `34`，DefaultAccount `students`，8 个授权 QoS；
- live smoke 覆盖 Run/Contract 列表、真实 `run.snapshot` 事件、Run 图谱、待审批队列、
  ETag 304 和 SSE 摘要脱敏。
- Phase 3C live smoke 通过：public release 发布审核、市场搜索、Bob 采用、owner-scoped
  canonical Contract、预检、Docker Slurm 成功 Run、finalized Evidence、Raw Capsule checksums、
  server-derived verification 和市场 metrics 全链闭合。

### 2.3 Phase 3A review 结论

review 中发现并已修复：

1. Run 按 Recipe 过滤的子查询缺少 `contracts.owner = runs.owner`，损坏关联数据下存在跨 owner
   元数据侧信道；已补 owner 联合条件和负面测试。
2. SSE 内部轮询继承客户端 `If-None-Match`，特殊客户端可能导致内部事件查询返回 304；已在
   SSE handler 中剥离该条件头并补回归测试。

允许进入下一阶段的残余风险：

- SSE 仍是每连接线程加 SQLite 轮询，适合比赛和单机，不是最终多副本事件总线；
- `q` 使用 `%LIKE%`，常规分页有索引保证，但全文搜索需在市场和大规模数据阶段单独设计；
- SQLite 中 Run 与 Contract 没有完整跨表外键，当前依靠应用层 owner/preflight 约束；
- HTTP API 仍集中在 `http_app.py`，继续扩展会快速增加 review 风险。

### 2.4 Phase 3B 完成状态

已完成 3B-1 与 3B-2：

- checksum-verified SQLite migration runner 和 `schema_migrations` 历史；
- owner-scoped `PlatformSnapshotStore`、TTL/freshness、keyset pagination、内容 hash；
- 平台快照列表/latest/detail API，以及 capabilities 对最新快照的安全引用；
- command `argv/stdout/stderr` 的采集脱敏和 API 防御性隐藏；
- `/api/v1/health/live`、`/api/v1/health/ready`，区分 checked、configured、disabled；
- FastAPI/ASGI 渐进适配、OpenAPI 公共面快照、兼容 GET/POST 转发 contract tests。

3B-2 review 已修复 snapshot path 参数缺失、ASGI 转发契约未覆盖、可选依赖被错误标记为
`ok` 三个问题。3B-3 与 3B-4 已完成并通过 findings-first review：

- 每个只读平台命令的精确 argv allowlist，拒绝“枚举名称合法但 argv 任意”的绕过；
- simulator executor collector，command missing/timeout/transport failure 转换为安全 partial observation；
- `conda env list --json` 探针；当前 Docker 无 conda，正确记录 partial；
- login snapshot 采集、脱敏、TTL 持久化与 owner API live smoke；
- 通过 RunService 提交固定 compute runtime probe，在 allocated GPU job 内检查 `nvidia-smi`/PyTorch；
- freshness-aware Contract preflight overlay，过期事实为 UNKNOWN，新鲜事实只 WARN，不冒充 entitlement；
- owner-scoped UserEntitlementSnapshot、DefaultAccount、association/QoS、TTL/data_quality 与安全 API；
- preflight 只使用 fresh authoritative entitlement，并在 prepare/submit 两条路径重新校验；
- pending/running `squeue` Evidence、终态 account/partition/QoS/TRES accounting 与 manifest 索引；
- InvalidQOS、association 缺失、Conda batch init、NVML mismatch 规则及安全处置边界；
- wheel/sdist 携带全部 33 条诊断规则，脱离源码安装验证通过；
- Docker live jobs `33`、`34` 成功，模拟器 GPU runtime 正确记录为 `unavailable`。

进入 3C 后已清理全 `scripts/` 的 32 项历史问题；`ruff check scripts` 和脚本 compileall 现已通过。

### 2.5 Phase 3C 当前进度

已完成第一切片：

- `003c.001.template_market` migration，覆盖 draft/review/release/withdrawal/adoption/verification；
- 草稿 optimistic lock、提交审核锁定、拒绝后修订、批准后发布状态机；
- 数据库 trigger 保证 release 不可更新、不可删除，撤回使用独立事实表；
- private/course/campus/public 可见性基础判定和 course scope；
- 采用操作幂等，并复制为采用者 owner-scoped private draft；
- 采用往返测试证明 advanced/raw sbatch 字段不会被新生界面路径丢弃。

已完成第二切片：

- `003c.002.template_publication_policy` 可增量升级已有 3C schema；
- reviewer/admin/course-instructor/course-TA 授权矩阵，课程审核绑定 course scope；
- 所有角色均禁止审核自己的草稿，审核角色和 scope 作为审计事实持久化；
- 提交审核前执行 Contract schema、materializer、静态 preflight 和结构化发布门禁；
- secret、危险 shell、raw sbatch allowlist、workdir、partition/GPU/container compatibility 检查；
- License、attribution、dataset access 和 risk statement 必填；
- publish 时重新执行当前门禁，release 固化 gate policy/report，旧的无门禁 release 禁止新采用；
- 容器验证只接受门禁注入的受信 digest，不相信草稿自报的 `verified` 字段；当前
  materializer 尚无 OCI capability，因此即使 digest 受信也仍会正确阻断容器模板；
- 第二切片 findings-first review 中发现的门禁过期、raw sbatch 绕过、自报容器验证三个
  P1 已修复，审查见 `phase3c_policy_gate_review.md`。

已完成第三切片：

- `003c.003.template_api_idempotency` 为发布 request key 建立数据库唯一约束；
- reviewer/admin/course instructor/course TA/course member 只从服务端配置生成，不接受客户端
  自报 role 或 course scope；
- 草稿 create/list/detail/PATCH/validate、review submit/queue/decision、publish、release detail、
  adopt/withdraw API 已接入，并进入显式 FastAPI/OpenAPI 公共面；
- 草稿与审核队列使用 scope-bound keyset cursor，owner、reviewer role 和 course scope 变化会使
  不匹配的 cursor 失效；
- 写操作使用显式 expected version，发布与采用使用 request key；
- course/private release 执行服务端可见性判定，发布者始终可见自己的 release；
- 撤回详情保留 actor/reason，新采用被禁止，已有采用的幂等重试仍返回原 lineage；
- 第三切片 findings-first review 的身份冒充、撤回后幂等、课程可见性 P1 与分页/队列/撤回详情
  P2 均已修复，审查见 `phase3c_template_api_review.md`。

已完成第四切片与 Phase 3C 纵向闭环：

- `003c.004.template_market_vertical` 增加 adoption Contract lineage 与受控 verification 审计字段、
  幂等和唯一约束；
- `/api/v1/templates` 支持可见性、关键词、partition、GPU、verification environment、verified
  过滤，按验证等级、最近通过时间、采用量、发布时间稳定排序并使用 scope-bound keyset cursor；
- adoption 在同一个 `BEGIN IMMEDIATE` 事务中生成 adopter-owned private draft、canonical Contract
  和 lineage；Contract 失败时三个对象整体回滚，并发幂等重试只产生一条 lineage；
- verification 只接受 actor-owned、绑定 adoption Contract 的终态 Run；status/environment/Evidence
  digest 均由服务端派生，客户端自报字段被拒绝；
- verification 要求 finalized accounting/result/manifest Evidence、完整 collection 和 ready Capsule，
  写入前重新验证 Raw Capsule checksums 并固化 manifest SHA；
- 已发布 draft 可通过 optimistic lock 开启下一修订，旧 release 保持数据库不可变；新增授权后的
  release diff API；
- Phase 3C 总 review 发现的 adoption 原子异常吞噬、manifest 未索引、Capsule 未绑定/未复验、
  release 无法迭代四个 P1 均已修复；审查见 `phase3c_vertical_review.md`；
- 模拟器健康门和 `smoke-sim-phase3c.sh` 均通过，证明 publish-to-real-job 全链，而非仅靠单测。

Phase 3C 后端纵向闭环现已 review 结项。可信 role directory 仍来自服务端静态配置，尚未接入学校
身份/课程目录；这属于生产身份适配残余风险，不阻塞进入 Phase 3D 产品壳与 Contract Studio。

## 3. 能力完成度

以下百分比是工程规划估算，不是测试覆盖率。

| 能力域 | 当前状态 | 比赛目标距离 | 生产目标距离 |
| --- | --- | --- | --- |
| Slurm 提交/查询/取消/重试/依赖 | 真实 Docker 主线闭环 | 近 | 中，需真实 107 和 HA |
| Evidence/Diagnosis/Capsule | 主流程闭环，环境事实仍偏薄 | 中 | 中 |
| ContractV2/Recipe/物化 | 后端核心较完整 | 近 | 中，需签名和供应链治理 |
| Agent 解释 | 规则和自有 OpenAI-compatible LLM 均有适配 | 近 | 中，需模型 SLO 和审计 |
| Agent 审批执行 | 单 Advice、单 patch action 闭环 | 中 | 远，缺 session、多轮、预算和恢复 |
| 模板建立/分享/市场/采用 | 后端纵向闭环并通过 Docker real-job smoke，UI 未完成 | 中 | 远 |
| 新生体验 | 只有演示表单 | 很远 | 很远 |
| 高级配置/源码/脚本 | Contract 后端支持，UI 未支持 | 远 | 远 |
| 终端协同 | 无产品入口 | 远 | 很远，安全成本高 |
| 身份/RBAC | 开发可信 header 边界 | 中 | 很远 |
| 数据库/多副本/可观测性 | SQLite 单机，基础事件 | 中 | 很远 |

整体判断：后端比赛核心约处于 75% 到 85%，可展示产品体验约处于 20% 到 30%，
Agent 自动修复约处于 40% 到 50%，生产控制面约处于 25% 到 35%。

## 4. Agent 闭环的准确边界

### 4.1 已完成

```text
持久化 Evidence/Diagnosis
→ evidence-bound facts
→ 规则或本地 LLM 解释
→ 确定性 policy 生成 action candidate
→ preflight
→ Advice 持久化
→ 用户审批指定 action
→ 并发 CAS 执行记录
→ 派生 Contract
→ 派生 Run
→ 可选提交 Slurm
→ lineage 和事件审计
```

关键安全性质已经存在：

- LLM 不直接调用 Slurm、文件系统和数据库；
- Evidence 文本被视为不可信输入；
- 所有事实要求 Evidence 引用；
- patch 只允许白名单字段；
- action 审批后还要经过确定性 Contract 校验和 preflight；
- advice 在 Run 或 Evidence 变化后失效；
- 重复执行通过确定性 ID 和 CAS 返回同一结果。

### 4.2 未完成

当前闭环只覆盖“用户主动请求 Advice 后的单动作执行”。尚缺：

- Worker 在 `diagnosis_state=succeeded` 后幂等创建 Agent 任务；
- 一个 session 下的多 diagnosis、多 action、多 Run 和多轮评估；
- 等待用户补充环境名、路径、成功判据等输入的状态；
- 环境探针、module/conda 探针、数据路径探针、受控文件 patch 等专用执行器；
- 修复后输出是否正确的验证，而不只是 Slurm exit code 为 0；
- 总尝试次数、wall time、token、提交次数和资源上限；
- 连续失败、证据不足、结果退化和模型不可用时的停止条件；
- Agent session 的通知、UI、对比、撤销和人工接管。

所以不能声称“Agent 所有设计目标完成”。准确表述是：**受控单动作执行闭环完成，多轮自主修复闭环未完成。**

## 5. 目标架构

### 5.1 五个平面

```text
Product Plane
  Project / Template Market / Contract Studio / Run Workbench / Terminal

Control Plane
  Identity / RBAC / API / Contract / Run / Audit / Notification

Execution Plane
  Worker / Slurm Backend / EvidenceTransport / PTY Gateway

Intelligence Plane
  PlatformSnapshot / Diagnosis / Agent Session / Policy / Evaluator / LLM Provider

Storage Plane
  PostgreSQL or SQLite / Evidence Store / Capsule Store / Event Outbox
```

边界要求：

- Product Plane 永远不能直接持有 Slurm token；
- Agent 只提交结构化 proposal，不直接操作执行面；
- Terminal 与可视化 UI 并存，但 terminal session 是显式、短期、用户作用域资源；
- 表单、高级编辑器、YAML/JSON、最终 sbatch 和 CLI 都是同一个 canonical Contract 的投影；
- 所有写操作产生 AuditEvent 和 domain event；
- Slurm REST 不直接暴露到浏览器。SchedMD 明确说明 `slurmrestd` 不适合直接面向互联网，
  生产部署必须由可信代理、TLS 和连接限制保护。

### 5.2 API 演进

现有 `Pilot107HttpApi` 保留为领域服务适配层，但不再继续无限增长：

1. 按 `contracts`、`runs`、`templates`、`agent`、`platform`、`terminal` 拆分 route module；
2. 使用 FastAPI/ASGI 作为生产 transport，生成 OpenAPI；
3. 旧 handler 在迁移期间跑同一组 contract tests，避免一次性重写；
4. cursor、error envelope、request ID、ETag、owner policy 作为共享中间件；
5. SSE 先保留，生产多副本阶段换为 outbox 加 broker，不改变浏览器事件协议。

## 6. 下一阶段总顺序

```text
3B 平台事实与 API 基础
→ 3C 模板市场纵向闭环
→ 3D 产品壳与 Contract Studio
→ 3E Agent Remediation Engine
→ 3F Run/Agent 工作台与终端协同
→ 3G 生产控制面
→ 3H 真实 107、比赛金路径与交付
```

3B 到 3F 均要交付可运行纵向切片，禁止长时间只建设抽象层。

## 7. Phase 3B：平台事实与 API 基础

### 7.1 目标

让后续表单、模板兼容性和 Agent 判断使用实时、带来源和 TTL 的平台事实，并控制 API 复杂度。

### 7.2 数据模型

- `Project`：owner、名称、共享根、默认环境、默认资源、归档状态；
- `PlatformSnapshot`：scope、source_authority、observed_at、expires_at、raw artifact refs；
- `UserEntitlementSnapshot`：account、partition、QOS、limits、data_quality；
- `EnvironmentSnapshot`：Python、conda、module、CUDA、GPU、路径和文件系统摘要；
- `AuditEvent`：actor、action、object、result、request_id、safe_metadata；
- `OutboxEvent`：事务内领域事件，为后续 broker 和通知准备。

### 7.3 实施包

#### 3B-1 工程治理

- 恢复或初始化 Git 元数据，建立可审查基线；
- 加入 CI：Ruff、Mypy、Pytest、前端检查、Docker smoke 分层执行；
- 建立 schema migration 版本表，禁止继续只靠启动时零散 `ALTER TABLE`；
- 保存 Phase 3A review 报告和测试证据。

#### 3B-2 API 模块化

- 抽出统一 auth、owner guard、query parser、pagination、response metadata；
- 建立 FastAPI route adapter 和 OpenAPI snapshot test；
- 提供 `/health/live`、`/health/ready` 和依赖 degraded 明细；
- 保持现有 API 行为 contract tests 全部通过。

#### 3B-3 官方平台事实采集

只读 collector 至少覆盖官方 107 文档中的：

- `hostname`、`pwd`、`whoami`、`date`；
- `python -V`、`which python`、`conda env list`；
- `squeue -u "$USER"`、`scontrol show part`、`sinfo`；
- GPU 作业中的 `nvidia-smi` 和最小 CUDA/PyTorch probe；
- partition 的 `AllowAccounts`、`AllowQos`、`MaxTime`、`TRES`、node states；
- snapshot 原始输出脱敏、hash、来源、采集位置和 TTL。

#### 3B-4 预检与 Evidence 接入

- Contract preflight 使用最新 platform/entitlement snapshot；
- 快照过期时展示 `STALE`，不能伪装为确定事实；
- GPU Contract 自动要求 GPU runtime probe；
- pending/running Evidence 增加 `squeue` 与 pending reason；
- 补齐 QOS wall/cpu limit、conda batch init、NVML mismatch 等规则。

### 7.4 验收门

- Docker 与受控真实只读 probe 对同一 schema 输出；
- 任一动态事实都有 source、observed_at、freshness 和 data_quality；
- 未授权命令、任意 shell 拼接和敏感环境变量不能进入 snapshot；
- API OpenAPI snapshot 稳定，旧客户端 contract tests 通过；
- findings-first review 清零 P0/P1 后进入 3C。

## 8. Phase 3C：模板建立、分享市场与采用

### 8.1 领域模型

- `TemplateDraft`：可编辑、owner-scoped、乐观锁版本；
- `TemplateRelease`：发布后不可变，绑定 Contract schema、Recipe version 和 content digest；
- `TemplateVisibility`：`private/course/campus/public`；
- `TemplateReview`：submitted/approved/rejected/withdrawn；
- `TemplateAdoption`：采用者、来源 release、目标 draft/contract；
- `TemplateCompatibility`：平台、partition、QOS、GPU、runtime、dataset requirements；
- `TemplateVerification`：Docker/real107、成功 Run、Evidence/Capsule、验证时间；
- `TemplateMetric`：adoption、成功率、失败分类、最近验证，不使用单纯点赞作为主排序。

### 8.2 API

```text
POST   /api/v1/template-drafts
GET    /api/v1/template-drafts
GET    /api/v1/template-drafts/{id}
PATCH  /api/v1/template-drafts/{id}
POST   /api/v1/template-drafts/{id}/validate
POST   /api/v1/template-drafts/{id}/publish
GET    /api/v1/templates
GET    /api/v1/templates/{template_id}/releases/{version}
POST   /api/v1/templates/{template_id}/releases/{version}/adopt
POST   /api/v1/templates/{template_id}/releases/{version}/withdraw
GET    /api/v1/templates/{template_id}/diff
```

所有写请求使用 `If-Match` 或显式 expected version，发布和采用使用 idempotency key。

### 8.3 发布门禁

- schema、materializer、preflight 全通过；
- secret scan、危险 shell lint、路径和容器能力检查；
- License、attribution、数据集访问说明和风险声明齐全；
- 发布版本 immutable；更新只能产生新 release；
- `public` 必须审核，`course` 由教师/助教角色审核；
- Docker 验证不能标记为 GPU/真实平台验证；
- 撤回不删除历史采用 lineage，但禁止新采用并显示原因。

### 8.4 纵向验收

```text
创建草稿
→ 完整校验
→ 发布审核
→ 市场搜索
→ 另一用户采用
→ 生成用户自己的 Contract 草稿
→ 预检
→ Docker Slurm 真实执行
→ 记录 adoption lineage 和 verification
```

负面测试覆盖跨用户草稿、课程可见性、发布后修改、密钥、恶意模板、未验证容器和并发采用。

## 9. Phase 3D：产品壳与 Contract Studio

### 9.1 前端技术基线

- React + TypeScript strict + Vite；
- TanStack Query 管理 server state；
- URL 管理搜索、过滤、tab 和当前对象，避免把可分享状态藏在全局 store；
- JSON Schema/Ajv 做客户端即时提示，服务端 validation 始终为最终权威；
- CodeMirror 6 或 Monaco 承担 JSON/YAML 和 sbatch diff；
- Lucide icons；React Flow 用于 DAG/lineage；
- 构建产物继续由现有 Web server/reverse proxy 提供，避免引入第二套部署入口。

### 9.2 信息架构

```text
/projects
/market
/templates/:id
/studio/new
/studio/:contract_id
/runs
/runs/:run_id
/agent
/cluster
/terminal
```

首屏是工作台，不做营销 landing page。

### 9.3 Contract Studio 五种投影

1. 基础模式：任务、路径、环境、资源、输出、常用预检；
2. 高级模式：完整 runtime/workflow/policy/extensions/array/module/conda；
3. 源码模式：JSON/YAML、schema completion、行列错误；
4. 脚本模式：original/resolved/wrapper、版本 diff、digest；
5. 终端协同模式：先提供等价命令和“在原生工具继续”，完整 PTY 在 3F 开放。

必须满足：

- 五种模式共享 canonical Contract；
- 基础到源码往返不丢未知 extension；
- 手工编辑冲突不被静默覆盖；
- 提交前固定展示 Recipe version、Contract digest、脚本 diff 和风险；
- 高级用户可以完全绕过向导，直接导入/编辑 Contract；
- UI 不替代 CLI，所有关键动作都给出等价对象 ID 和安全命令。

### 9.4 视觉与可用性

- 安静、工具型、信息密度适中，不做卡片套卡片和营销 hero；
- 资源、状态、风险、证据使用不同语义色，不使用单一蓝紫色主题；
- 桌面采用可调双栏/三栏工作台，移动端按任务顺序折叠；
- 所有状态都有 loading/empty/stale/degraded/error/forbidden；
- 键盘操作、焦点、对比度、中文长文本、长路径和窄屏必须通过测试；
- 图标按钮提供 tooltip，危险写操作有对象级确认而不是通用弹窗。

### 9.5 用户反馈

邀请当前可获得的本科生执行：

- 从模板市场找到 Python CPU 模板；
- 采用并修改 workdir/command；
- 理解资源预检；
- 提交并找到日志、结果和失败原因。

目标：收集界面、术语、预检、提交和 Evidence 理解上的真实反馈，并形成 findings-first 改进清单；不设置固定参与人数、完成率或时间门槛。任务未完成、使用终端或需要提示本身都是有效反馈。同时继续用一组包含未知 extensions、array、module、conda 和 workflow 的高级 Contract 验证往返零字段丢失。

## 10. Phase 3E：Agent Remediation Engine

### 10.1 新状态机

新增 `RemediationSession`，状态为：

```text
waiting_evidence
→ diagnosing
→ planning
→ awaiting_input | awaiting_approval | ready
→ preparing
→ executing
→ evaluating
→ succeeded | exhausted | blocked | failed | cancelled
```

每个 session 包含：

- source Run/Contract/Diagnosis/Evidence digest；
- `AgentTurn`、`ActionProposal`、`ActionDecision`、`ActionExecution`；
- derived Contract/Run lineage；
- `EvaluationResult` 和修复前后 diff；
- attempts、submissions、wall time、LLM calls/tokens 的 budget；
- stop reason、人工接管原因和最终审计摘要。

### 10.2 Worker 主动闭环

```text
Diagnosis 完成
→ 按 automation policy 幂等创建 session
→ 规则先生成候选
→ 必要时调用 LLM 生成结构化 plan
→ policy/capability/preflight
→ 通知或进入审批队列
→ 执行批准 action
→ 收集派生 Run Evidence
→ evaluator 判断结果
→ 成功结束，或在预算内进入下一轮
```

Worker 崩溃后通过 lease 和 CAS 恢复；不能依靠进程内 task。

### 10.3 Action Executor 分级

| Executor | 示例 | 默认策略 |
| --- | --- | --- |
| `contract_patch` | partition/QOS/time/memory/array | 可预览，通常审批 |
| `environment_select` | conda env/module 选择 | 先 probe，再审批 |
| `path_probe` | workdir/data/output 可达性 | 只读可自动 |
| `runtime_probe` | Python/import/CUDA/GPU | 只读、短时、有资源上限 |
| `dependency_plan` | requirements/conda 变更建议 | 只生成计划，不直接安装 |
| `file_patch` | 用户代码最小 diff | 高风险，显式范围和审批 |
| `retry_run` | 派生 Contract 后提交 | 受提交预算和资源上限控制 |

生产默认禁止任意 shell executor。文件 patch 必须限制 allowed roots、大小、后缀、diff 行数，保存原 hash，
并允许撤销。依赖安装不得发生在登录节点或共享基础环境。

### 10.4 结果评估

“Slurm 成功”不等于“作业正确”。Evaluator 按顺序检查：

1. submit/reconcile 是否确定；
2. terminal state 和 exit code；
3. Evidence 完整性；
4. expected outputs 的存在、大小、hash/格式；
5. Template/Contract 定义的 success protocol；
6. 与 source Run 的资源、日志、结果 diff；
7. 用户提供的可选测试命令或指标阈值。

结果只能是 `verified_success`、`execution_success_unverified`、`failed`、`inconclusive`。

### 10.5 LLM 接入

目标测试模型准确标识：

```text
OpenCode runtime preset: ustc-edu
OpenCode provider:       ustc-deepseek
model:                   deepseek-v4-flash-ascend
107Pilot model value:    deepseek-v4-flash-ascend
base URL:                校内 OpenAI-compatible /v1 gateway
```

生产系统保持 provider-neutral，不依赖 OpenCode 进程。OpenCode 仅用于当前测试凭据和模型可用性验证；
107Pilot 通过 `PILOT107_LLM_*` 配置直接调用自有/校内 OpenAI-compatible gateway。

需要把当前 explanation schema 扩展为两个独立 schema：

- `AgentNarrativeV2`：中文解释和引用；
- `RemediationPlanV1`：目标、假设、所需输入、结构化 action proposals、风险和停止条件。

LLM plan 永远只是 proposal。policy engine 重新计算权限、字段、风险、资源和 preflight，不能信任模型自报。

### 10.6 模型评测门槛

建立版本化 benchmark corpus，至少覆盖：

- invalid partition/QOS、QOS wall/cpu limits；
- timeout、OOM、command/package/module/conda 错误；
- workdir/data/output 路径错误；
- CUDA/NVML/PyTorch CPU build；
- array 部分失败和 workflow dependency；
- prompt injection、伪造 evidence ref、越权 action；
- timeout、429、5xx、无效 JSON、截断、thinking tags 和重复响应。

进入自动化试运行的最低门槛：

- schema 成功率在一次 repair 后不低于 99%；
- 已接受事实引用覆盖率 100%；
- 越权 action 通过 policy 的次数为 0；
- 常见确定性故障两轮内 verified fix rate 不低于 90%；
- 误修率低于 3%，资源无界放大为 0；
- provider 不可用时规则诊断、手工修复和作业管理仍可用。

## 11. Phase 3F：Run/Agent 工作台与终端协同

### 11.1 Run 工作台

- 左侧可分页 Run 列表和保存的过滤器；
- 中间 timeline、Slurm state、DAG、retry/agent lineage；
- 右侧资源、平台 snapshot、新鲜度和操作区；
- stdout/stderr tail、结构化 Evidence tree、output inventory、Capsule；
- raw/normalized 切换显示 REST、sacct、scontrol 与归一化字段；
- cancel、retry、clone、compare、capsule 和 native command。

### 11.2 Agent 工作台

- session queue：等待输入、等待审批、执行中、需人工接管；
- facts/inferences 明确分区，Evidence 点击定位；
- action 级 Contract/script/file diff；
- 风险、预算、预期效果、回退计划；
- prepare 与 submit 分离；
- 修复前后 Run/Evidence/outputs/resource 对比；
- 每次 decision 显示 actor、版本、时间和 request ID。

### 11.3 受控终端

终端是高级能力，不是基本表单的替代品。实现顺序：

1. 先提供 Job ID、workdir 和可复制的 `squeue/scontrol/tail/scancel` 等价命令；
2. 再提供只读 log shell 或平台原生终端 deep link；
3. 最后才开放 xterm.js + WebSocket PTY。

PTY 生产门槛：

- OIDC 用户映射到真实平台身份；
- 短期 session、空闲和总时长限制；
- allowed roots、文件传输和命令审计边界；
- 无共享 root shell、无长期 token 下发浏览器；
- WebSocket origin/CSRF、防重放、速率和并发限制；
- session 关闭后进程树回收。

## 12. Phase 3G：生产控制面

### 12.1 数据与并发

- Repository protocol 将 SQLite 本地模式和 PostgreSQL 生产模式隔离；
- PostgreSQL migration、事务、外键、行锁和 JSONB 索引；
- submit/reconcile/collection/agent execution 使用 lease、fencing token 和 outbox；
- 多 API、多 Worker 压测证明零重复提交；
- SQLite 继续作为单机开发/比赛离线模式，不被删除。

### 12.2 身份与权限

- 学校 OIDC/SSO，Authorization Code + PKCE；
- user/course_teacher/platform_admin/service roles；
- Project/Template/Run/Evidence/Terminal 对象级授权；
- trusted header 只允许来自受信反向代理，并由网络策略阻止客户端直达 API；
- Slurm user/token 绑定和代理审计。

### 12.3 安全与运维

- TLS、CSP、CSRF、secure cookie、rate limit、body/response size limit；
- secret manager，密钥轮换，日志/Evidence/Capsule secret scanning；
- metrics：API、queue、submit、reconcile、Evidence、Agent、LLM、SSE；
- traces 使用 request_id、run_id、job_id、session_id 关联；
- SLO、告警、备份、恢复演练和审计保留策略；
- `slurmrestd` 由可信代理隔离，限制连接数和高频查询缓存，避免给 controller 造成锁竞争。

### 12.4 生产阶段门

- 72 小时稳定性运行；
- API/Worker/DB/Slurm gateway 分别重启可恢复；
- 100 并发用户、1 万 Run、事件和日志 tail 压测；
- 依赖不可用、网络分区、数据库故障和模型故障注入；
- 权限矩阵、OWASP 基线和供应链扫描；
- 备份恢复到可提交、可查询、可审计状态。

## 13. Phase 3H：真实 107 与比赛冠军交付

### 13.1 真实平台渐进准入

```text
L0 静态官方文档
→ L1 Docker 行为验证
→ L2 真实平台只读 CLI/REST snapshot
→ L3 人工确认的短作业 submit/get/evidence
→ L4 cancel/retry/agent repair
→ L5 生产限量用户
```

每一级都生成 compatibility report，失败不静默退回伪能力。

### 13.2 比赛金路径

至少准备六条 live 场景：

1. 新生从模板市场采用 CPU 作业并成功提交；
2. 高级用户用 YAML/脚本模式提交 array/workflow；
3. 错误 QOS 在提交前被 platform snapshot/preflight 阻止；
4. Python 包或 conda 错误被诊断，Agent 提议，用户审批后修复并验证输出；
5. 模型不可用时规则诊断和手工修复继续工作；
6. API/Worker 重启后 Run、Evidence 和 Agent session 恢复。

### 13.3 评审展示原则

- 展示真实 job ID、事件、Evidence、Contract digest 和 lineage，而不是 mock；
- 同一失败同时展示新生视图和高级源码/终端视图；
- 明确 Docker、真实 107、模型和模板验证等级；
- 主动展示安全边界：模型无法绕过 policy、审批和资源限制；
- 准备离线模型降级和 Slurm 不可用时的可解释演示数据，但不冒充 live。

## 14. 每阶段统一执行与 review 流程

每个阶段严格执行：

```text
设计冻结
→ schema/API contract tests
→ 最小纵向实现
→ 单元/迁移/权限/并发测试
→ Ruff/Mypy/前端类型检查
→ Docker simulator preflight
→ live smoke
→ findings-first review
→ 修复全部 P0/P1 和适用 P2
→ 全量回归
→ 状态文档和证据归档
```

review 必查：

- owner/RBAC 和跨对象关联；
- 幂等、CAS、lease、崩溃恢复；
- secret 和 Evidence/LLM 输入边界；
- schema migration 和旧数据；
- UI 字段丢失、错误状态、键盘和移动端；
- Docker 行为是否被错误宣传为真实平台能力；
- 真实模型输出是否被错误当作事实或授权。

阻断规则：

- P0 安全/数据损坏/重复提交：必须清零；
- P1 主流程错误/越权/无法恢复：必须清零；
- P2 可用性和边缘行为：有明确 owner、测试和后续阶段才可延期；
- 未跑 live smoke 必须明确标记阶段未完成。

## 15. 立即执行的前 12 个工作包

1. 归档 Phase 3A review 和测试结果，处理 Git/CI 基线；
2. 建 migration runner 和 schema version，不改变现有行为；
3. 拆出 API 公共 auth/pagination/error middleware；
4. 建 FastAPI/OpenAPI adapter 和旧 API contract test；
5. 实现只读 `PlatformSnapshot` schema、store 和 Docker collector；
6. 接入 `scontrol show part`、`sinfo`、`squeue` 与环境/GPU probe；
7. 用 snapshot 驱动资源预检和 UI capability read model；
8. 实现 TemplateDraft/Release/Review/Adoption migration 与 store；
9. 实现模板 API、权限、发布门禁和 Docker adoption smoke；
10. 搭建 React/TypeScript 产品壳，先迁移 Run 列表和平台状态；
11. 实现 Contract Studio canonical state 与基础/高级/源码往返测试；
12. 再进入 RemediationSession，而不是直接让 LLM 获得更多执行权限。

工作包 1 到 7 构成 Phase 3B；8 到 9 构成 Phase 3C 后端纵向闭环；10 到 11 启动 Phase 3D。

## 16. 关键风险

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 前端重写脱离 live API | 视觉好但主流程假 | 每页先 live read model，再做视觉；mock 只作异常注入 |
| Agent 过早开放 shell/file | 安全和数据损坏 | 专用 executor、白名单、审批、hash/diff/rollback |
| 平台动态事实过期 | 预检和建议错误 | source authority、TTL、STALE、提交时 snapshot |
| USTC 模型行为变化 | schema/延迟退化 | 版本化 benchmark、provider adapter、规则降级 |
| SQLite 承担多副本 | 锁争用和恢复风险 | 比赛单机保留，生产切 PostgreSQL repository |
| PTY 扩大攻击面 | 越权和凭据暴露 | 延后到身份/RBAC 明确后，短 session 和隔离 gateway |
| 模板供应链 | 恶意命令和密钥传播 | immutable release、审核、secret/risk scan、撤回 |
| 无 Git/CI 基线 | review 和回归不可追溯 | 下一阶段第一工作包解决 |

## 17. 官方依据

本计划对齐：

- 本地 107 官方文档：`/home/knowingthesea/文档/107/docs-main/docs/basics/cli-index.md`；
- 本地 107 官方文档：`/home/knowingthesea/文档/107/docs-main/docs/basics/slurm.md`；
- 本地 107 官方文档：`/home/knowingthesea/文档/107/docs-main/docs/basics/jobs.md`；
- SchedMD REST API：<https://slurm.schedmd.com/rest.html>；
- SchedMD sbatch：<https://slurm.schedmd.com/sbatch.html>；
- SchedMD sacctmgr：<https://slurm.schedmd.com/sacctmgr.html>；
- SchedMD Job Reason Codes：<https://slurm.schedmd.com/job_reason_codes.html>；
- SchedMD Job Array：<https://slurm.schedmd.com/job_array.html>；
- SchedMD Container：<https://slurm.schedmd.com/containers.html>；
- Conda shell initialization：<https://docs.conda.io/projects/conda/en/stable/commands/init.html>；
- NVIDIA NVML errors：<https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceEnums.html>；
- WHATWG Server-Sent Events：<https://html.spec.whatwg.org/dev/server-sent-events.html>；
- OpenID Connect Core：<https://openid.net/specs/openid-connect-core-1_0-18.html>；
- TanStack Query：<https://tanstack.com/query/latest>；
- React Flow：<https://reactflow.dev/>；
- xterm.js：<https://xtermjs.org/docs/>。

## 18. 下一动作

Phase 3C 已完成 findings-first review。下一步进入 **Phase 3D：产品壳与 Contract Studio**，顺序为：

1. 已清理全 `scripts/` 32 项 Ruff 存量，并将全脚本检查设为后续阶段门禁；
2. 已完成 React + TypeScript strict 产品壳、live Run/平台/授权 read model、Docker 与真实浏览器审查，详见 `phase3d_shell_review.md`；
3. 已完成 Contract Studio canonical state、基础/高级/JSON/YAML/脚本/终端协同投影与无损往返审查，详见 `phase3d_studio_review.md`；
4. 已完成模板市场、release diff、采用 lineage、Contract dynamic preflight 与对象级确认提交，主流程使用真实 Docker/API，详见 `phase3d_market_run_review.md`；
5. 已用往返测试覆盖 array、module、conda、workflow 和 advanced/raw sbatch 零字段丢失；
6. 已完成 Run Evidence 日志/结果/对象预览、确定性诊断、Raw Capsule 校验、失败可解释性与 findings-first review，详见 `phase3d_run_evidence_review.md`；
7. 历史上已实现五人新生验收 schema/evaluator、live study readiness、真实失败任务样本与生产身份残余风险决策；产品负责人随后取消固定人数和总体 pass/fail 门禁，当前以 `phase3d_user_feedback_protocol.md` 为准。可获得参与者的反馈用于形成 findings，不阻塞 RemediationSession；校园多用户生产身份仍为 NO-GO。

Phase 3B 的 DefaultAccount 约束避免了多账号误授权，但显式 Slurm account 选择仍是高级功能缺口，
应在 3C compatibility schema 中预留，在 3D Studio 中以基础/高级/YAML 同源方式暴露。

Git 元数据仍是工程治理阻断项；若项目原始 `.git` 无法恢复，应在用户确认远端与分支策略后初始化，
避免错误创建与正式仓库无关的历史。
