# 107Pilot：低登录节点负载的 HPC 编码 Agent 架构研究

- 日期：2026-08-10
- 研究类型：工程架构调研与问题重构
- 状态：已完成；结论已进入正式设计
- 约束：远程 VM 当前不可用；所有实现假设必须先在本地 Docker Slurm simulator 验证
- 对应规格：`docs/superpowers/specs/2026-08-10-pi-hpc-agent-core-design.md`

## 1. 研究问题

学校反馈表明，Slurm 登录节点不能承受所有学生各自常驻 Claude、Hermes 或同类大型编码 Agent；如果仅要求学生在个人电脑运行 Agent，再通过 SSH 对集群进行大量细碎操作，又会产生延迟、认证、文件同步和环境差异问题。

本研究回答六个问题：

1. 107Pilot Agent 应定位为远程 shell、通用编码 Agent，还是 HPC 原生控制面？
2. 模型循环、代码工作区、Slurm 控制和真实计算分别应放在哪里？
3. 是否能采用成熟 Agent harness，而不在登录节点运行完整 Agent？
4. 如何支持从零创建实验工程、修复代码和异步验证，同时保持低资源占用？
5. 现有 107Pilot 的 SSH relay、Worker、Evidence、Runtime Watch、文件传输和修复状态机可以复用到什么程度？
6. Template Market 如何成为工程创建、成功 Run 复用和后续运行验证的完整支线，而不是静态模板列表？

## 2. 研究范围与方法

调研优先采用官方文档、官方项目仓库和一手系统论文，覆盖：

- HPC 中心对登录节点、交互开发和编码 Agent 的规则；
- Open OnDemand、JupyterHub 等 HPC Web 门户的执行位置；
- Slurm allocation/job/step 生命周期；
- Pi Agent Harness 的核心、SDK、工具、状态、模型提供方和隔离边界；
- OpenHands 的 Agent/工作区分离模式；
- OpenSSH 连接复用和 rsync 增量同步；
- MCP Tasks 对长任务、人工输入和断线恢复的抽象。

本研究不对具体校内模型质量、真实应用节点规格和真实 107 文件系统挂载作未经验证的假设；这些内容进入本地 benchmark 或后续 capability probe。

## 3. 证据矩阵

| 问题 | 一手证据 | 可支持的结论 | 限制 |
|---|---|---|---|
| 登录节点能否常驻大 Agent | [NERSC Resource Usage Policies](https://docs.nersc.gov/policies/resource-usage/) 明确登录节点是共享资源，禁止计算/内存密集型任务并实施 cgroup 限制 | 不能把每用户常驻 Agent 设计成平台默认路径 | 具体学校限额仍需本校确认 |
| 编码 Agent 在 HPC 的适用边界 | [NERSC AI Coding Tools](https://docs.nersc.gov/development/coding-agents/) 强调用户控制、workspace-write、真实证据和对 MPI/GPU/文件系统建议的验证 | Agent 应基于事实、隔离修改并验证，不能仅靠语言模型猜测 | NERSC 指南不是 107 校规 |
| 交互服务应在哪里运行 | [Open OnDemand 架构论文](https://openondemand.org/sites/default/files/documents/PEARC%2024%20Paper%20210805.pdf) 和 [Batch Connect 资料](https://openondemand.org/sites/default/files/documents/SUG%20App%20Development.pdf) 将门户放在服务侧，将应用作为 batch job 放到计算节点 | Web 控制面与调度执行面分离是成熟 HPC 模式 | 107Pilot 不需要照搬反向代理交互桌面 |
| Jupyter/IDE 如何避免登录节点重载 | [NERSC Jupyter Reference](https://docs.nersc.gov/services/jupyter/reference/) 和 [Yale Open OnDemand VS Code](https://docs.ycrc.yale.edu/clusters-at-yale/access/ood-vscode/) 将较重会话提交为计算作业 | 编译、环境验证和长时交互应进入 allocation | 小型本地检查仍可在受限应用沙箱中执行 |
| Slurm 的执行抽象 | [Slurm Job Launch Design](https://slurm.schedmd.com/job_launch.html) 区分 allocation、batch step 和 job step | Agent 的重计算应映射为 Slurm job/step，不应成为登录节点后台进程 | 排队延迟需要异步恢复协议 |
| Pi 是否可嵌入 | [Pi SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md) 支持嵌入、事件、会话、自定义工具、禁用内建工具和自定义资源加载 | 可以把 Pi 用作可裁剪 harness，而不是直接部署 CLI | SDK 上层会话语义可能与 107Pilot 重叠 |
| Pi core 能否只承担 turn loop | [Pi agent-core](https://github.com/earendil-works/pi/tree/main/packages/agent) 提供工具循环、状态注入、事件、`beforeToolCall`/`afterToolCall` 和终止钩子 | `pi-agent-core` 适合作为短时 Turn 内核 | 107Pilot 必须自行实现持久编排和策略 |
| Pi 是否自带权限边界 | [Pi 仓库权限说明](https://github.com/earendil-works/pi#permissions--containerization) 明确默认继承进程全部权限，没有内建文件/进程/网络/凭据限制 | 不能直接运行原版 Pi CLI；必须由 107Pilot Tool Gateway 和沙箱实施权限 | 仅靠 system prompt 不构成安全边界 |
| Pi 能否接校内模型 | [Pi Custom Providers](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/custom-provider.md) 支持私有端点、代理、OpenAI-compatible API 和自定义 streaming | 可通过固定 ModelProfile 接入校内自部署模型 | 真实模型必须验证 tool calling 和 context 行为 |
| Agent 与执行工作区能否分离 | [OpenHands Agent Server](https://docs.openhands.dev/sdk/guides/agent-server/overview) 将客户端、Agent Server 和隔离 Workspace 分开，文件/命令经 API | 推理服务不必挂载用户工作区；工具可以代理到隔离环境 | OpenHands runtime 不是 HPC 调度器 |
| 高延迟链路如何减少文件传输 | [rsync 技术报告](https://rsync.samba.org/tech_report/) 通过块匹配只传输差异，并为高延迟、低带宽链路设计 | 代码镜像可使用增量同步；大数据不应随 Agent 工作区复制 | rsync 不一定在真实平台可用，SFTP 必须是基线 |
| SSH 频繁建连如何优化 | [OpenSSH ssh_config](https://man.openbsd.org/ssh_config) 的 `ControlMaster`/`ControlPersist` 支持连接复用 | 107Pilot 可复用已认证连接，避免每个工具重新握手 | 连接复用不能把登录节点变成 Agent runtime |
| 长工具调用如何恢复 | [MCP Tasks](https://modelcontextprotocol.io/extensions/tasks/overview) 使用 durable task handle、状态、TTL、输入请求和取消处理长任务 | Slurm 验证应立即返回 AgentTask，Turn 释放后由事件恢复 | MCP Tasks 仍在演进，不应成为首版硬运行依赖 |

## 4. 方案空间

### 4.1 方案 A：每用户在登录节点运行完整编码 Agent

```text
Browser/SSH → login node Pi/Claude/Hermes → shell/files/jobs
```

优点是工具与远端工作区距离近，环境一致；缺点是 Agent 进程、上下文内存、索引、子进程和轮询全部落在共享登录节点。学生规模增加后，资源消耗近似随用户数线性增长，并且任意 shell 权限难以集中治理。

结论：拒绝作为 107Pilot 默认或备用生产架构。

### 4.2 方案 B：学生本地运行完整 Agent，再通过 SSH 操作集群

```text
student PC agent → repeated SSH/SFTP → login node → Slurm
```

优点是学校无需承载 Agent；缺点是：

- 每个学生自行配置模型、密钥、SSH、MFA 和环境；
- Agent tool loop 会产生大量高延迟往返；
- 本地文件和集群真源容易漂移；
- 学生电脑下线后难以保持长任务与会话状态；
- 107Pilot 无法统一做 Evidence、审批、预算、审计和恢复。

结论：可作为高级用户个人选择，不能作为比赛产品的主要能力。

### 4.3 方案 C：完整 Agent 作为 Slurm 作业运行

```text
portal → sbatch agent runtime → compute node agent → model/tools
```

该方案保护登录节点，也能获得真实软件环境，但存在：

- 每次对话都可能等待队列；
- Agent 空闲思考或等待用户时占用 allocation；
- 计算节点未必能访问校内模型 API；
- 模型凭据、会话恢复和交互通道更复杂；
- 为轻量搜索、规划和文本编辑申请计算节点成本过高。

结论：不作为 Agent brain；只把环境相关验证、构建和实验执行放入 Slurm。

### 4.4 方案 D：集中式短时 Agent Turn + 应用工作区 + Slurm 异步执行

```text
107Pilot app node:
  durable session + short Pi turn + workspace mirror
                       │
                typed cluster relay
                       │
Slurm:
  validation/build/experiment jobs on compute nodes
```

优势：

- 登录节点没有 Agent 常驻进程；
- 空闲会话只占数据库记录；
- 代码搜索和补丁不产生细碎 SSH 往返；
- 真实环境验证仍由 Slurm 约束；
- 107Pilot 可以统一身份、预算、审批、Evidence 和恢复；
- 校内模型服务集中复用，不为每个用户加载模型。

代价：需要 WorkspaceSnapshot、AgentTask、Tool Gateway 和 Python/TypeScript 内部协议。

结论：采用。

## 5. Pi 接入层级比较

| 接入层级 | 可复用能力 | 主要风险 | 结论 |
|---|---|---|---|
| 每 Turn 启动完整 Pi CLI/RPC | CLI 会话、工具、RPC、compaction | 默认权限过宽、进程监督、私有 JSONL 协议、功能过多 | 仅用于兼容性 spike |
| 嵌入 `pi-coding-agent` SDK | 会话、资源加载、skills、compaction、自定义工具 | 会话与 107Pilot durable state 重叠；自动发现需彻底关闭 | 可作为本地原型 |
| 嵌入 `pi-agent-core` | 最小 Agent loop、状态、事件、tool hooks | 需要 107Pilot 自己管理 session/context/policy | 正式采用 |

推荐把 Pi 固定在“单个 Agent Turn 的推理与工具循环”范围内：

- 107Pilot 数据库是会话真源；
- 107Pilot 决定 profile、上下文、工具、预算与审批；
- Pi 不持有 SSH/MFA/Slurm 凭据；
- Pi 不读取宿主机或用户远端工作区；
- Pi state 在 Turn 完成时写回，进程可以立即释放。

## 6. 负载模型

定义：

- `U`：平台注册或持久会话用户数；
- `A`：同时活跃的 Agent Turn 数；
- `R`：每 Turn 的远端工具往返数；
- `C`：同时执行的 Slurm AgentTask 数；
- `M_turn`：一个活动 Pi Turn 的上下文和运行时内存；
- `M_idle`：一个休眠会话的数据库状态。

每用户常驻架构的 Agent 内存近似：

```text
M ≈ U × M_agent
```

短时 Turn 架构近似：

```text
M ≈ A × M_turn + U × M_idle
```

其中 `M_idle` 只是持久记录，不对应进程。由于平台可以让 `A << U`，并通过队列限制 `A`，应用节点负载可以被显式控制。

远端操作方面，直接 SSH tool loop 近似产生 `A × R` 次登录面操作。应用侧索引、日志 cursor、PlatformSnapshot 和批量同步把多数读操作移到本地或已有采集层，登录面只保留有界的控制和同步请求。

该模型不预测具体 CPU/内存数值，也不覆盖模型服务自身成本。它的可证伪条件是：本地压测发现 Pi Turn 无法在给定应用节点预算内受控排队，或工作区同步产生的集群 I/O不低于直接远端 Agent。两者都必须通过 benchmark 验证。

## 7. 工作区与创建能力结论

只支持“修复已有工程”会迫使初学者先在本地创建项目，削弱比赛要求中的一键运行和 Agent 价值。因此需要支持四种统一入口：

```text
blank | template | existing | failed_run
```

但从零创建必须是受界的“可运行最小实验工程”，而不是无限制软件外包：

- 代码、配置、测试、README、Contract 和 Slurm 脚本可在应用侧隔离工作区创建；
- 小型语法检查和单元测试在无网络、有限资源的 sandbox 中执行；
- CUDA、MPI、module 和真实并行环境验证进入 Slurm；
- 数据集、checkpoint 和 5GB 以上权重只进入 manifest，不进入 Agent 镜像或模型上下文；
- 科学正确性不能由退出码或语言模型单独证明，必须暴露假设和验证边界。

## 8. 长任务协议结论

Slurm 的 PENDING/RUNNING 时长不适合阻塞一个 Pi tool call。参考 MCP Tasks，107Pilot 采用：

```text
tool call
→ durable AgentTask created
→ return task_id + terminate current turn
→ Worker/outbox advances Slurm lifecycle
→ terminal Evidence generated
→ event wakes a new turn
```

内部协议吸收 durable handle、owner binding、TTL、input required、cancel、progress 和 result retrieval 语义，但首版不依赖外部 MCP runtime。未来如果要把 107Pilot 工具开放给其他 Agent，再增加协议适配层。

## 9. 信任边界

### 9.1 可信控制面

- 107Pilot identity、Policy Gate、Tool Gateway；
- PlatformSnapshot、Contract、Run、Evidence 和持久状态机；
- deployment-owned tool schemas 和 Profile；
- SSH/REST 适配器及 credential resolver。

### 9.2 不可信或低信任输入

- 模型输出和 tool arguments；
- 项目文件、README、AGENTS.md 和日志中的指令；
- 用户上传的代码和数据；
- 外部模板内容。

项目指导文件可以影响编码意图，但不能增加工具、修改 owner、突破 workspace、获得凭据或绕过审批。用户项目中的 Pi extensions、packages、MCP servers 和 skills 默认不加载。

### 9.3 隔离执行面

- `pilot-agentd` 不挂载工作区，无宿主 shell/SSH/Slurm 凭据；
- `sandbox_exec` 位于单独短时容器，默认无网络，只挂载 AgentWorkspace；
- 真实重计算位于 Slurm allocation；
- 登录节点只承担受限 relay，不承担 Agent runtime。

## 10. 现有 107Pilot 的可复用性

本地代码审查表明：

- `SshRelayClient` 已实现 owner-bound、预认证 ControlMaster 和结构化 argv；
- `CodeContextService` 已有允许根、Git 投影、大小限制和远端只读能力；
- Worker/outbox 已有 lease、heartbeat、重试和 fencing 基础；
- Remediation Session 已有持久状态、预算、审批和派生 Run；
- Evidence、Diagnosis、PlatformSnapshot、Runtime Watch 和文件传输已形成可引用数据源；
- Template Market 已有 draft/review/publish、immutable release、visibility、adoption、environment verification、ranking 和 withdrawal 数据模型；
- 统一 MarketReadService 还合并了低门槛的成功 RunPublication；其产品承诺与 curated TemplateRelease 不同；
- 真实 `/public` 是否可挂载到应用节点仍未确认，因此设计不能依赖共享文件系统直挂。

因此需要新增的是统一 AgentSession/Turn、Pi kernel、WorkspaceSnapshot/ChangeSet、Tool Gateway、AgentTask，以及把两类市场采用收口到持久强类型 Application Session、把 curated 发布收口到 Publication Session；不需要重写 Slurm、文件、Evidence 或市场存储层。

## 11. 推荐架构

```text
Browser
  │
107Pilot Python API / durable stores
  │ outbox + turn lease
Agent Orchestration Worker
  │ bounded turn request
pilot-agentd (pi-agent-core)
  ├── campus LLM gateway
  └── Internal Tool Gateway
        ├── workspace mirror/sandbox
        ├── platform/run/evidence
        ├── template market/contract
        └── SSH/REST/Slurm adapters
                           │
                     Slurm AgentTask
                           │
                     compute nodes
```

系统的产品定位不是“远程托管 Claude/Hermes”，而是：

> 面向 HPC 实验生命周期的集中式、证据驱动、可恢复 Agent 控制面。它把自然语言目标编译为受身份、文件边界、资源预算和 Slurm 调度约束的工程变更与实验运行。

## 12. 局限与待验证事项

1. 校内模型是否稳定支持 Pi 所需的 tool calling、streaming 和上下文长度，尚需真实兼容测试。
2. 应用节点 CPU、内存、磁盘和并发上限未知，不能预先承诺学生规模。
3. 真实 107 是否允许应用节点访问 `/public` 未确认；SFTP 是基线，rsync 和直挂只能 capability-gated。
4. 真实计算节点能否访问校内模型不应成为依赖；正式架构把模型调用留在应用侧。
5. `sandbox_exec` 的具体容器运行时和生产部署权限需要确认；即使不可用，结构化文件创建和 Slurm 验证仍可工作。
6. Pi 上游演进较快，必须精确锁版并维护跨版本契约测试。
7. 自动运行成功不等于科学有效，领域假设、数据质量和结果解释仍需要用户判断。

## 13. 验证与证伪计划

本地 simulator 至少验证：

- 空闲会话不对应常驻 Pi 进程；
- 从自然语言创建可运行最小实验工程；
- Pi 仅能调用 allowlisted tools；
- 5GB 文件只进入 metadata；
- AgentTask 等待期间 Turn 已释放；
- agentd/Worker/浏览器断线后恢复；
- Slurm 提交不确定时不重复提交；
- 源工作区变化时发布阻塞；
- 多 owner 不串 workspace/task/evidence；
- 项目文件中的提示注入不能扩大权限；
- 模型不可用时确定性能力继续，代码创建明确阻塞；
- 自然语言目标能够发现并应用兼容模板；没有合适模板时明确进入空白创建流程；
- curated 与 RunPublication 的采用都不能绕过 Agent，且 reference_only 不继承验证保证；
- 成功 Run 默认不分享，ShareManifest 未授权字段和 Contract 不可被采用者获得；
- 成功 Run 能经过严格脱敏、参数化、复现验证和审核形成 immutable release；
- exact、metadata-only 与等价成功 Run 不会产生重复 release；
- 模板采纳、环境验证、新版本建议和撤回决策均可绑定 Run/Evidence 并追溯；
- 登录节点不存在 Pi/Node Agent 常驻进程。

如果下列任一条件成立，应重新评估架构：

- 应用侧镜像的文件系统 I/O 对集群产生不可接受负载；
- 校内模型无法可靠执行结构化工具调用；
- Pi 状态无法在 Turn 边界稳定序列化和恢复；
- 学校禁止应用节点运行必要的 Agent Worker；
- 真实身份/凭据模式无法让 durable Worker 在不保存长期凭据的情况下完成操作。

## 14. Template Market 领域支线补充

初版总体抽象若只提供 `template_application` 和 `template_publication` 两个工具型 Profile，会遗漏市场的核心网络效应：模板需要被发现、适配、运行、验证、修订和治理，才能从一次成功作业变成可信的复用资产。

### 14.1 消费支线

```text
user intent
→ market search / compare
→ compatibility + verification ranking
→ MarketApplicationSession
  ├─ TemplateApplicationSession(curated)
  └─ ReferenceAdaptationSession(reference_only)
→ user-specific ApplicationPlan
→ private Contract / ChangeSet
→ Run
```

Agent 必须参与两类市场采用。curated release 只表达可复用不变量、参数 schema 和环境约束，不能直接代表当前学生；RunPublication 更只证明一次成功运行，必须以 reference_only 重新检查路径、依赖和资源。即使用户已经手选条目，Agent 也要形成一次完整确认计划；自然语言入口则先完成搜索与比较。

既有 `adopt_release()` 与 `RunPublicationStore.adopt()` 的授权、幂等和 lineage 写入应提取为强类型 finalizer 的内部 transaction helper；当前 copy-only 行为不足以应用已确认计划，也不能作为公共入口。没有可采用 Contract 的 RunPublication 只能作为说明性参考。若没有兼容候选，系统返回显式的 `no_suitable_template`，再进入空白工程或已有工程创建路径。

### 14.2 生产支线

```text
successful Run
├─ default: do not share
├─ optional ShareManifest → RunPublication
└─ TemplatePublicationSession
→ extract invariants
→ strict sanitization
→ parameterization
→ semantic duplicate check
→ private draft
→ reproduction validation
→ review
→ immutable release
```

成功 Run 默认不分享；普通分享由用户逐字段授权。发布 curated template 不是“复制一次成功作业”：Agent 需要区分领域不变量、用户参数、平台参数、运行时派生值和禁止发布值，并执行严格脱敏。生成的 draft 必须先保持私有，经语义查重、独立复现 Run 和审核后才能发布。

### 14.3 运行反馈与版本治理

模板采纳不等于成功。一次可信验证至少绑定 release、ApplicationSession、Run、PlatformSnapshot 和 Evidence digest，并区分 Docker、本地模拟、真实 CPU 与真实 GPU 环境。验证结果以 append-only 记录累积，过期或失败会影响推荐排序，但不会自动修改或撤回 release。

Agent 可以基于失败验证和 Runtime Watch 证据提出新版本、降权、弃用或撤回建议；最终发布、弃用和撤回仍由模板发布者或审核者决定。release 保持 immutable，修复通过新 draft/version 完成，旧版本谱系、历史 Contract 和既有 Run 保留可追溯性。

查重使用提取前 family fingerprint 与 sanitized bundle content fingerprint。exact 或 metadata-only 差异不创建新 release；等价的新成功 Run 优先增加既有 release 的 verification。LLM 只解释 near-duplicate 的结构化 diff，不能成为唯一阻塞依据。

### 14.4 复用原则

该支线复用现有 Template Market 与 RunPublication 的存储模型，也复用 Workspace、AgentTask、Run 和 Evidence。Pi 只能调用 `market_discover`、`market_application_*`、`template_publication_*` 等高层工具，不能直接调用底层 publish、adopt 或 withdraw 操作。完整契约见 `docs/superpowers/specs/2026-08-10-template-market-agent-detailed-design.md`。

## 15. 最终结论

采用 `pi-agent-core` 能减少自研通用 Agent loop 的成本，但性能收益来自整体架构而不只是库本身：共享校内模型、短时 Turn、休眠零进程、应用侧上下文缓存、异步 Slurm AgentTask 和严格的执行位置分类共同消除了登录节点常驻 Agent 的主要负担。

正式实现应从本地只读 Pi Turn 开始，再依次完成从零创建、隔离验证、异步 Slurm、ChangeSet 发布、Template Market 双向循环和 Run 闭环。完整 Pi CLI、任意 SSH shell、每用户常驻容器以及计算节点上的 Agent brain 均不进入正式路径。
