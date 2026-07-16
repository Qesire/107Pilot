# 107Pilot 后续执行计划（环境边界重订版）

日期：2026-07-16  
状态：当前执行基线  
适用范围：Phase 3D 反馈收集、Phase 3E–3H 后续工程、测试 VM 与真实 107 的准入边界。

纯自动工程任务的详细切片、验证门和执行顺序见 [`automated_execution_plan_20260716.md`](automated_execution_plan_20260716.md)。

## 1. 重订结论

后续开发继续以本机为唯一主开发环境。现阶段不把当前镜像提前上传到 8 核 16GB CPU VM，也不把开发者个人临时 SSH 权限解释为 107Pilot 已经接入真实 107。

项目分成三条不同性质的轨道：

1. **本机产品工程主线**：代码、测试、Docker Slurm 行为验证、Phase 3E/3F 和可在本机完成的 3G 工作；
2. **独立验收环境线**：只有出现共享访问、冻结版本、真人远程测试、比赛演示或稳定性运行需求时，才把固定镜像部署到 CPU VM；
3. **真实 107 集成线**：当前只保留已完成的开发者辅助只读探测证据；完整接入等待新的明确授权、服务部署位置、网络、凭据和文件系统条件，不作为当前阶段完成条件。

## 2. 自动化与用户参与边界

### 2.1 标记

- **AUTO**：可由开发代理在本机或受控 VM 中实现、运行、审查和留证，不要求用户手工操作；
- **USER-USE**：需要用户或用户指定的本科生实际使用界面，提供主观体验和理解反馈；
- **USER-DECISION**：不要求手工测试，但需要产品负责人决定范围、风险或是否授权外部动作；
- **EXTERNAL**：依赖学校平台、服务器管理员、网络、凭据或正式准入，当前不能由本机自动化消除。

### 2.2 责任矩阵

| 工作 | 标记 | 是否需要用户实际使用 | 说明 |
| --- | --- | --- | --- |
| Git/CI、文档一致性、API 拆分 | AUTO | 否 | 自动实现、测试和 review |
| 前端类型、组件、路由、权限与浏览器功能回归 | AUTO | 否 | 使用自动化测试和 `pilot-browser` |
| 视觉层级、术语、信任感、流程是否顺手 | USER-USE | 是 | 机器可以发现错误，不能完全代替人的理解与偏好 |
| Phase 3E 状态机、存储、lease、policy、executor、evaluator | AUTO | 否 | 全部先在 Docker simulator 验证 |
| LLM schema、引用、越权、故障和修复 benchmark | AUTO | 否 | 以版本化 corpus 自动评测 |
| Agent 中文解释是否自然、审批信息是否足够 | USER-USE | 可选 | 在 UI 检查点做少量主观复核即可 |
| Phase 3F Run/Agent 工作台功能 | AUTO + USER-USE | 仅主观体验需要 | 功能正确性自动化，交互取舍由人反馈 |
| PostgreSQL、outbox、多 Worker、备份、负载、故障注入 | AUTO | 否 | 不要求用户手工检查日志或执行脚本 |
| VM 预检、部署、smoke、恢复和并发验证 | AUTO + USER-DECISION | 否 | 用户只需提供/授权 VM 条件，不需要亲自部署 |
| 校园身份、角色、安全策略和生产风险接受 | USER-DECISION + EXTERNAL | 否 | 属于组织和产品责任，不能由测试替代 |
| 真实 107 submit/cancel/evidence | USER-DECISION + EXTERNAL | 可能 | 只有获得授权后；若凭据只能由用户持有，则由用户运行准备好的最小脚本 |

结论：**当前本机工程几乎都可自动化。需要用户实际打开产品使用的部分，主要是前端视觉、交互、文案和 Agent 审批体验；其余少量人工参与是授权或决策，不是手工测试。**

### 2.3 用户实际使用检查点

将用户使用压缩到最多三个短检查点，不在每个工程切片打断开发：

1. **U1 基本流程反馈**：Market → Studio → preflight → Run → Evidence。可由现有本科生完成，用户本人不必重复；
2. **U2 Agent 交互反馈**：Phase 3E 后端和 Phase 3F Agent UI 稳定后，检查事实/推断、diff、风险、预算、审批和修复结果是否容易理解；
3. **U3 发布候选复核**：比赛演示或共享部署前，快速检查整体视觉、核心文案和金路径。若没有外部发布需求，可延后。

每个检查点只提交观察和偏好；复现、日志、接口、回归和修复验证由自动化承担。

## 3. 环境与能力声明

| 等级 | 环境 | 当前状态 | 可以证明 | 不可以证明 |
| --- | --- | --- | --- | --- |
| D0 | 本机单元/契约测试 | 可用 | 领域逻辑、权限、迁移和接口契约 | Slurm live 行为 |
| D1 | 本机 Docker competition profile | 可用 | Web/API/Worker/模拟 Slurm 的真实命令链、Evidence/Capsule | 真实 107 兼容性和生产能力 |
| S1 | 8C/16G CPU 测试 VM | 尚未部署，按需启用 | 干净宿主机、共享访问、固定镜像、持续运行 | 真实 107 接入 |
| R0 | 开发者个人 SSH 辅助只读 probe | 部分完成 | 观察到的版本、分区、节点和 REST 异常语义 | 107Pilot 服务端 submit/get/cancel/evidence |
| R1 | 107Pilot 真实平台集成 | 不具备条件 | 仅在未来准入完成后定义 | 当前禁止宣称已验证 |
| P1 | 校园多用户生产 | NO-GO | 无 | OIDC、RBAC、用户映射和生产安全 |

所有报告、页面和演示必须标注证据等级。Docker job ID、模拟 GPU/分区和 command gateway 结果不得表述为真实 107 结果。

## 4. Phase 3D 反馈策略

### 4.1 不设置参与人数门禁

Phase 3D 工程切片的自动化和本地 live 验证已经完成。后续不再设置“至少 5 人”、中位时间或任务全通过等真人验收门槛，也不以参与人数阻塞 Phase 3E。

- **工程准入门**：本地全量测试、competition smoke、身份负面测试通过，P0/P1 清零；
- **用户反馈**：作为发现和排序问题的产品输入，不是样本量合规研究，也不是 pass/fail 门禁；
- **反馈处理门**：收到的反馈必须形成 findings，逐项标记严重度、决定和验证方式；P0/P1 在相关功能成为默认路径前清零，P2/P3 可记录 owner 后进入 backlog。

参与者没有完成任务、用了终端、耗时较长或理解错误，都是有效反馈，不应让记录本身变成“无效”。原 `pilot107.novice-acceptance/v1` 五人 schema、evaluator 和 pending artifact 仅作为历史实验工具保留，不再是阶段准入事实源。

### 4.2 三名本科生的使用方式

现有三人作为一轮轻量产品反馈来源：

1. 在本机 `fixed_user` competition 部署上顺序执行统一任务；
2. 每人使用匿名 participant ID；若产生 Contract/Run，则记录其 ID 以便复现；
3. 不提供 SSH、Docker 或真实 107 凭据；
4. 记录停顿、误解、干预、任务结果和可复现 Evidence；计时可以保留为诊断信息，但不设硬阈值；
5. 汇总为 findings：问题、影响、严重度、证据、处理决定、修复版本和复核结果；
6. 修复重大问题后可请可用参与者复核，不要求固定人数或重新凑齐样本。

反馈可以在开发者本机现场完成，不要求提前部署 VM。只有需要学生远程访问或需要冻结一个不受开发影响的共享环境时，才启用 VM。具体记录方式见 [`phase3d_user_feedback_protocol.md`](phase3d_user_feedback_protocol.md)。

## 5. 本机工程执行顺序

### Workstream A：工程治理与扩展边界

在继续扩大系统前先处理：

1. 建立可用的 Git 仓库、受控分支和最小 CI；
2. 更新当前状态文档中的测试数量、代码规模和阶段结论；
3. 拆分不断增长的 `api/http_app.py`，至少分出 Agent、Run/Evidence、Template 和平台事实路由；
4. 为关键前端流程补充自动化测试，优先覆盖 Market → Studio → Run → Evidence、身份切换/固定身份和错误状态；
5. 固定每个评审切片的 schema/API contract、迁移和回归基线。

退出门：Git/CI 可重复运行；全量测试继续通过；API 拆分不改变旧契约；文档不再把 Docker 能力写成真实平台能力。

### Workstream B：Phase 3E Remediation Engine

全部在本机 Docker simulator 上完成，按五个可独立评审的切片推进：

1. **3E-1 状态与存储**：`RemediationSession`、turn/proposal/decision/execution/evaluation、预算、停止原因、lineage、迁移与 owner scope；
2. **3E-2 规则闭环**：Diagnosis 后幂等创建 session，lease/CAS 恢复，先只使用确定性规则，不接 LLM 执行；
3. **3E-3 受控 Action Executor**：优先实现只读 probe、Contract patch、人工审批和有提交预算的 retry；继续禁止任意 shell；
4. **3E-4 Evaluator**：区分 `verified_success`、`execution_success_unverified`、`failed`、`inconclusive`，验证输出、Evidence 和前后差异；
5. **3E-5 LLM proposal**：模型只生成结构化建议，policy/preflight 重新计算；建立故障、越权、注入和 provider failure benchmark。

每个切片执行 contract tests、迁移/权限/并发测试、Docker live smoke、findings-first review 和全量回归。3E 未完成前不得称为自主修复系统。

### Workstream C：Phase 3F 工作台

顺序为：

1. Run timeline、DAG/lineage、raw/normalized Evidence、retry/clone/compare 和保存筛选器；
2. Agent session queue、Evidence 定位、action diff、预算、审批、回退和前后 Run 比较；
3. 提供可复制的原生命令和平台终端 deep link；
4. 暂不实现生产 PTY。xterm.js/WebSocket PTY 等到可信身份、短期 session、审计和进程回收条件具备后再评审。

退出门：基础作业流程仍无需终端；所有 Agent 写操作都经过对象级审批和 policy；模型不可用时 Run 管理、规则诊断和手工修复仍可用。

### Workstream D：可在本机完成的 Phase 3G

先做与真实平台授权无关的控制面能力：

1. Repository protocol 与 PostgreSQL 生产实现；
2. lease、fencing token、outbox 和多 Worker 零重复提交验证；
3. metrics、trace correlation、结构化审计、备份和恢复；
4. 限流、响应大小、secret scanning 和供应链基线；
5. 本机/后续 VM 的故障注入与稳定性测试。

OIDC、校园目录角色、subject → Slurm user 映射和真实 token 生命周期只做接口设计与 mock contract；没有校方接入条件时保持 NO-GO，不以模拟结果代替生产验收。

## 6. CPU VM 的延迟部署计划

### 6.1 部署触发条件

满足以下任一条件才部署：

- 已确定远程真人测试或比赛演示日期；
- 需要一个数日不受本机开发影响的冻结版本；
- 需要验证目标 VM 的 Docker/cgroup/网络/TLS/volume 行为；
- 需要进行持续运行、重启恢复或小规模并发测试。

在此之前只确认 VM 的 OS/架构、Docker、privileged container、磁盘、端口、固定 IP/域名、TLS 和重置策略，不上传项目镜像。

### 6.2 部署前本机工作

1. 建立与 8C/16G 对齐的 CPU-only Slurm profile；
2. 移除/隐藏 GPU 分区、GPU QoS 和 GPU 模板；
3. 不再向 Slurm 声明当前的双节点各 32 CPU/模拟 A100 配置；
4. 为计算节点和应用容器设置 CPU/内存限制，给宿主机保留资源；
5. 固定镜像 digest、迁移版本、初始化数据和回滚方法；
6. 更换默认密码、gateway token 和自签名证书策略。

### 6.3 VM 验收门

```text
VM preflight
→ 导入固定镜像
→ 启动 CPU-only competition profile
→ 全链 smoke
→ study readiness
→ 重启与 volume 恢复
→ 小规模并发
→ 冻结版本
```

服务器只运行发布候选，不直接开发。VM 验收通过只能标记为 `S1 staging/competition`，不能升级为真实 107 或校园生产能力。

## 7. Phase 3H 拆分

### 7.1 比赛交付轨

比赛金路径依赖本机 Docker 和按需 VM，不依赖真实 107：新生 CPU 成功、高级 Contract、预检阻断、Agent 审批修复、模型降级、服务重启恢复。所有场景明确展示其验证环境。

### 7.2 真实 107 集成轨

只有以下前置条件同时成立才重新启动：

1. 明确允许 107Pilot 服务访问的平台授权，而不只是开发者个人临时 SSH；
2. 确定 API/Worker 部署位置和到 `slurmrestd` 的网络路径；
3. 获得可合规使用的短期凭据机制；
4. 明确 subject/操作者到 Slurm 用户、association、QoS 的映射；
5. 明确 `/home`、`/public` 或文件 API 的访问与授权模型；
6. 平台方允许最小 submit/get/cancel/evidence smoke，并确定审计与停止办法。

满足后仍按只读 snapshot → 人工确认短作业 → cancel/retry → Agent repair → 限量用户逐级准入。任一级失败都保持原等级，不静默降级或外推。

## 8. 近期工作包顺序与责任

1. **AUTO**：归档环境边界决策并修正文档漂移；
2. **AUTO**：建 Git/CI 基线；
3. **AUTO**：拆分 Agent 将要进入的 API/存储边界并补契约测试；
4. **USER-USE U1**：参与者时间允许时执行一次本机基本流程反馈；不阻塞后端工作；
5. **AUTO**：实现并评审 3E-1 状态、迁移和 owner-scoped store；
6. **AUTO**：实现并评审 3E-2 规则闭环与崩溃恢复；
7. **AUTO**：实现 3E-3/3E-4 受控动作和结果评价；
8. **AUTO**：完成模型 benchmark 后接 3E-5，不让 LLM 获得额外授权；
9. **AUTO**：基于 3E 实体实现 Phase 3F Agent/Run 工作台及浏览器功能回归；
10. **USER-USE U2**：Agent UI 稳定后进行一次交互与解释反馈，随后自动修复和回归；
11. **AUTO**：推进 PostgreSQL/outbox/observability 等 3G 通用能力；
12. **USER-DECISION**：有明确共享验收需求时授权制作 CPU-only RC；部署和验收继续自动化；
13. **USER-USE U3，可选**：外部演示前快速复核发布候选；
14. **USER-DECISION + EXTERNAL**：外部准入条件成立后另行启动真实 107 集成 review。

## 9. 当前停止条件

- 不因拥有 VM 而提前部署未冻结镜像；
- 不因个人 SSH probe 而实现或宣称真实平台接入；
- 不把轻量用户反馈宣传为统计性研究或真实平台验收；
- 不在身份与审计条件缺失时实现生产 PTY；
- 不让 LLM 绕过 policy、preflight、审批、预算或 Evidence 事实边界；
- 任一切片出现 P0/P1 时停止扩展，修复、回归、review 后再继续。
