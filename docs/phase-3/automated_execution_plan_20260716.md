# 107Pilot 自动执行任务计划

日期：2026-07-16  
状态：当前自动工程主线  
目标：只列出开发代理可以在本机和受控 Docker 环境中独立完成、验证、评审和归档的任务。

## 1. 范围

本计划包括：

- 代码、schema、迁移、API、前端和部署资产实现；
- 单元、契约、权限、并发、迁移和故障测试；
- 本机 Docker Slurm live smoke；
- 使用 `pilot-browser` 的浏览器功能、错误状态和基础可访问性回归；
- findings-first review、P0/P1 修复和阶段证据归档；
- 本地 CPU-only 发布候选和离线部署包制作。

本计划不包括：

- 真人对视觉、术语、信任感和流程顺手程度的反馈；
- 8C/16G VM 的实际上传和部署；
- 真实 107 的 submit/cancel/evidence 操作；
- 学校 OIDC、目录角色、真实用户映射和生产安全批准；
- 需要真实密钥、证书、平台账户或组织授权的外部动作；
- 生产 PTY。

这些排除项不阻塞本计划中的本机工程，分别由用户反馈或外部准入轨处理。

## 2. 统一自动质量门

每个实现切片严格执行：

```text
设计/schema 冻结
→ 最小纵向实现
→ 定向测试
→ 全量静态检查和回归
→ Docker live smoke
→ UI 切片的 pilot-browser 回归
→ findings-first review
→ 修复 P0/P1 和适用 P2
→ 再次全量回归
→ 更新状态与证据
```

常规验证集合：

```bash
uv run ruff check src tests scripts
uv run mypy src
uv run pytest
npm run typecheck
npm test -- --run
bash scripts/check-sim-core.sh
```

具体切片再增加 migration、permission、concurrency、competition smoke 和浏览器用例。任何 P0/P1 未清零、迁移不可回放、重复提交或 owner 越权都阻断下一切片。

## 2.1 2026-07-16 当前执行检查点

已自动完成并形成 Git 基线：

- A1–A4：事实收敛、Git/CI、路由边界、前端金路径测试；
- 3E-1 与规则闭环核心：persistent session/turn/proposal/decision/execution/evaluation、预算、lease/CAS、Worker 推进和四态 evaluator；
- 3E API 第一版：owner-scoped create/list/detail/advance/approve/reject/execute/cancel 与 OpenAPI contract；
- 3F Agent 第一版：失败 Run 入口、session queue/detail、预算、等待输入提示、批准/拒绝/取消/执行和审计展示；
- 本机 D1 回归：最新应用镜像、Web interaction smoke、Agent `awaiting_input → cancelled` 浏览器实测无页面错误。

剩余自动主线按依赖收敛为六个工作包：

1. **R1 — 3E contract completion**：keyset/ETag/events、input/takeover、并发幂等和专用 action 边界；
2. **R2 — LLM safety plane**：provider-neutral schema/adapter、fake/replay corpus、安全 benchmark；
3. **R3 — 3F completion**：Run timeline/lineage/compare、安全命令、Agent Evidence/diff/前后结果对比；
4. **R4 — local 3G**：repository parity、多实例/outbox、observability/recovery/security；
5. **R5 — CPU-only RC**：8C/16G profile、资源限制、离线包、SBOM、空目录恢复；
6. **R6 — final gate**：全量回归、故障/负载/浏览器金路径、findings 与未上传 VM readiness manifest。

## 2.2 2026-08-25 Agent 生命周期封版检查点

- Task 11–20 已完成 leased observation、资源评价、持久 AgentTask、异步 Slurm validation、artifact-aware recovery、批准发布、正式 Run/Watch/result、repair/Market 生命周期统一及 PostgreSQL runtime wiring。
- Task 21 使用四个环境专用入口封版；D0 source 与 D1 Docker runtime 必须在一个 Git SHA 同时通过，报告写入 `artifacts/acceptance/agent-lifecycle/<sha>/`。
- D1 固定覆盖十二个生命周期场景和 100 idle Sessions、10 concurrent Turns、100 active Watches、connection command/byte budgets；模型不可用只阻塞对应生成式 Project，Run/Evidence/Watch 保持确定性可用。
- S1 需要确认的 8C/16G VM、同 revision bundle 和公开 URL；R1 需要 target、owner、批准 root、authorization ID、确认 flag 与现存 ControlMaster。缺少条件记录 `not_run`，不得推断批准。
- S1/R1 `not_run` 不否定 D0/D1 本机候选，但两者和校园身份/运维批准未通过前，校园生产始终 NO-GO。完整判定见 [`agent_lifecycle_acceptance_matrix.md`](agent_lifecycle_acceptance_matrix.md)。

### 2026-07-18 恢复检查点

- R1 已完成：Remediation keyset/ETag/events、input/takeover、幂等与专用 action 边界；
- R2 已完成：provider-neutral `RemediationPlanV1`、Evidence-bound input、secret/prompt-injection fail closed 和 fake/replay benchmark；
- R3 已完成并通过 findings-first review：Run keyset/保存筛选、timeline/lineage/compare、对象级写操作、Agent diff/预算/接管、安全命令与 Terminal deep link；
- R3 review：[phase3f_run_agent_workbench_review.md](phase3f_run_agent_workbench_review.md)；
- 当前进入 R4：PostgreSQL repository parity、多实例/outbox/fencing、可观测性/恢复和本地安全基线。

### 2026-07-18 R4 控制面 Repository 检查点

- 已固定 backend-neutral `ControlRepository`：lease acquire/renew/release、单调 fencing token、幂等 outbox、topic claim、退避和 dead-letter；
- SQLite 参考实现与 PostgreSQL 实现共用同一套契约测试；PostgreSQL 使用 advisory migration lock、事务、JSONB、`FOR UPDATE` 和 `SKIP LOCKED`；
- 真实 PostgreSQL 16 临时实例完成 16/16 双后端契约，40 条消息由 4 个并发 worker 恰好领取一次；
- PostgreSQL 非 UTF-8 配置明确 fail closed；迁移 checksum 与重复初始化已在真实实例验证；
- review：[phase3g_control_repository_review.md](phase3g_control_repository_review.md)；
- R4 尚未完成：下一切片将 Run submit/reconcile/collection 和 Agent execution 接入该 substrate，再做多进程 crash/reclaim 与外部副作用零重复证明。

### 2026-07-18 R4 Run Submit Outbox 检查点

- Run submit 生产 builder 已接入 durable outbox、持久化 submission owner/fencing token 和 Worker dispatcher；
- 每个 Run 使用稳定且唯一的 Slurm job name，reconciliation 不再混淆同用户并发提交；
- 模糊 transport 结果只重试 reconciliation，永不自动发送第二次外部 submit；预算耗尽进入 `SUBMISSION_UNCERTAIN`；
- 两线程、两个 spawn 进程、enqueue-only crash、外部 submit 后 crash、DB write 后 ack 前 crash 契约通过；
- Docker demo 跨容器接管通过：`run_live_outbox_recovery_fixed_20260718` 由独立 Worker 提交并完成 Evidence collection；
- review：[phase3g_submission_outbox_review.md](phase3g_submission_outbox_review.md)；
- Run submit 子切片提交：`b2e1789 feat: add fenced submission outbox`。

### 2026-07-18 R4 Collection Outbox 检查点

- production Worker 的 Evidence collection 已改由确定性 `collection.execute` outbox 驱动；旧任务表保留为业务状态源；
- collection task 持久化单调 fencing token，租约 owner 名被复用时，旧 token 仍不能写回；
- task generation 随 runtime task 再激活递增，避免已成功 message ID 阻断下一轮采集；
- 两线程、两个 spawn 进程竞争均证明每个任务只执行一次；任务成功写库后 ack 前崩溃只补 ack；
- Docker 跨容器金路径 `run_4c0ac2cde0c340cb872b7f60024467cf` 完成 7/7 fenced collection outbox 和 20 个 Evidence objects；
- review：[phase3g_collection_outbox_review.md](phase3g_collection_outbox_review.md)；
- collection 子切片提交：`1686aa4 feat: add fenced collection outbox`。

### 2026-07-18 R4 Agent Execution Outbox 检查点

- production API/Worker 已使用 `agent.execute` durable outbox；prepare/submit 采用独立确定性 phase message；
- execution 行持久化 phase、dispatcher owner 与 fencing token，同 phase reclaim 和跨 phase stale writer 均被拒绝；
- Agent 派生合同/Run 使用确定性 ID，真正外部提交继续由嵌套 `run.submit` outbox 保护；
- 两线程、两个 spawn 进程、enqueue-only crash、execution write 后 ack crash 与 terminal replay 契约通过；
- Run/Agent dispatcher 同步修复 batch 预领取租约过期窗口，改为完成一条再领取下一条；
- Docker 跨容器接管：execution `agentexec_a7f75f2668a36d762af1d519bd87e0ec` 派生 Run `run_agent_55f45c5355509bdfb80b0b1517c2352e`，最终 SUCCEEDED；
- review：[phase3g_agent_execution_outbox_review.md](phase3g_agent_execution_outbox_review.md)；
- R4-2 完成；下一步进入 R4-3 PostgreSQL 业务接线、可观测性、恢复演练与本地安全基线。

### 2026-07-18 R4 控制面恢复检查点

- 新增 integrity-bound 冷备/验证/空目录恢复入口，覆盖 SQLite、Evidence、Capsule 与可选 PostgreSQL custom dump；
- create/restore 强制显式 quiesce，组件缺失、symlink、特殊文件、递归路径、篡改和非空覆盖均 fail closed；
- PostgreSQL restore 的 reset 确认下沉到核心 API，DSN 不进入 manifest 或子进程 argv，失败文本脱敏；
- 本机停止 API/Worker 后完成 552 文件冷备与隔离恢复；28 张业务表/1,209 行、394 个 Evidence 和 155 个 Capsule 文件一致，服务恢复 healthy；
- 全量门禁：565 passed、10 PostgreSQL integration skipped、2 subtests；Ruff 与 strict mypy 通过；
- review：[phase3g_recovery_review.md](phase3g_recovery_review.md)；
- 恢复子切片完成；R4-3 继续 PostgreSQL 业务接线、长期可观测性和其余本地安全基线。

### 2026-07-18 R4 控制面可观测性底座检查点

- API/stdlib 双 transport 共享 Prometheus scrape；route label 替换对象 ID，queue 暴露 topic/state、due、expiry、attempt/reclaim；
- Worker 按稳定 ID 原子持久累计 reconcile、submit、Evidence、diagnosis、Agent 与 remediation，flock 防同 ID 丢计数；
- graceful stop/硬崩溃通过 active tombstone 区分，Compose healthcheck 强制 freshness、telemetry、active 和 schema；
- health、outbox last_error、Run/remediation audit event 统一 secret redaction，并保留 fencing/LLM 计数审计字段；
- 五条告警覆盖 metric source、stale Worker、expired lease、dead letter 与 API 5xx；stdlib Docker live 和 Worker restart 连续性通过；
- 全量门禁：578 passed、11 PostgreSQL integration skipped、2 subtests；Ruff、strict mypy、Compose config 通过；
- review：[phase3g_observability_review.md](phase3g_observability_review.md)；
- 可观测性底座完成；R4-3 继续 LLM/SSE 专项、持久 trace、安全基线与 PostgreSQL 业务接线。

### 2026-07-18 R4 控制面安全与供应链基线检查点

- Web→API 身份头改为 HMAC-authenticated forwarding，绑定 method/target/user/body/timestamp/request ID，
  过期、篡改和 freshness 窗口内重放 fail closed；
- stdlib/FastAPI/Web/HTTPS proxy 全部增加请求/响应上限，API/Web 增加 429 + Retry-After 的进程内限流；
- fixed identity 的跨站 simple POST 已关闭：write 只接受 JSON、same-origin、无 Cookie，并统一 CSP、
  frame、nosniff、referrer、permissions、opener headers；
- Compose secret 不进 environment/Git，以 host-group-only 文件供非 root API/Web 读取；首次 live 权限失败
  已通过 supplemental GID 修复并重新验证；
- CI 新增 Python/Node audit、candidate secret scan、Trivy source/config/image HIGH/CRITICAL gate；考虑
  2026-03 上游事件，Trivy v0.36.0 固定完整 commit SHA；
- 全量门禁：587 passed、11 PostgreSQL integration skipped、2 subtests；Web 64 tests/build、Ruff、strict
  mypy 72 source files、四种 Compose config 和候选 secret scan 通过；
- Docker live：直连伪造 identity 403、BFF 签名 200、CSRF 负面通过，纵向模拟 Run SUCCEEDED 并收集
  20 个 Evidence objects；在线漏洞和 image CVE 等待真实 CI run，不误报本机 offline 0 findings；
- review：[phase3g_security_review.md](phase3g_security_review.md)，运维契约：
  [control_plane_security.md](../operations/control_plane_security.md)；
- 安全基线子切片完成；R4-3 继续持久 trace、LLM/SSE 专项与 PostgreSQL 业务 Store 接线。

### 2026-07-18 R4-3 trace / LLM / SSE 与控制仓库运行时接线

- 新增持久 control trace，关联 request/run/job/session；API 普通请求自动写入，SSE 外层连接只写一次；
- 新增 LLM attempt 成败、时延、input/output token，以及 SSE active、完成原因、时延、事件数 metrics；
- API/Worker 支持通过 `PILOT107_CONTROL_POSTGRES_DSN` 选择 PostgreSQL control repository，默认仍为 SQLite；
- 全量门禁：590 passed、13 PostgreSQL integration skipped、2 subtests；Ruff、strict mypy（73 source files）通过；
- R4-3 仍继续完整 PostgreSQL 业务 Store parity；不得把 control repository 接线表述为全领域业务数据库迁移完成。

### 2026-07-18 R5 CPU-only 发布候选功能检查点

- 新增 8C/16G 目标 CPU profile，仅暴露 `CPU-RC`/`qos_cpu_rc`，单作业 4 CPU/6 GiB/4 小时；GPU partition、QoS 和 recipe 均不进入运行候选；
- Compose 固定一个 Slurm worker 和 7 CPU/约 11.6 GiB 容器上限，保留宿主余量；启动生成随机本地凭据并拒绝 placeholder；
- 本机成功/失败/取消 Evidence/Capsule 两轮通过，跨整栈重启可恢复；20 路轻量并发和 4 路完整 workflow 无错误；
- 浏览器 live 回归确认 CPU-only capability 与本地/真实 107 边界，console/errors 为空；
- 全量门禁：594 passed、13 PostgreSQL integration skipped、2 subtests；Web 64 tests/build、Ruff、strict mypy 通过；
- review：[cpu_rc_release_review.md](cpu_rc_release_review.md)；下一步固定 revision 并完成离线包独立目录导入/启动/停止/恢复验收。

真人使用不进入这些工作包的通过条件。可选的 U2/U3 只用于视觉、文案、信任感和交互取舍；VM 上传、真实 107 操作和生产身份仍需另行授权。

## 3. A 轨：工程基线与扩展边界

### A1 当前事实与文档收敛

- 重新生成代码规模、测试数量、服务和路由清单；
- 修正仍把历史 `0/5`、Docker GPU 或真实 107 探测写成当前能力的文档；
- 建立单一阶段状态索引，历史 review 保留但明确是否被后续决策取代；
- 生成自动验证命令清单和最新通过记录。

退出条件：当前状态、自动计划、用户反馈协议和历史评审之间不存在未标注冲突。

### A2 本地 Git 与 CI 基线

- 在项目目录建立本地 Git 基线和适用的 `.gitignore`；
- 不提交数据库、Evidence、Capsule、密钥、证书和构建缓存；
- 添加最小 CI workflow：Python 静态检查/测试、前端类型/测试、Compose config 和镜像契约检查；
- 在本机运行 CI 等价命令，确保 workflow 不依赖未声明的个人环境；
- 不创建远端仓库、不 push、不发 PR。

退出条件：本地变更可审计，CI 配置可解析，等价命令全通过，secret/large artifact 不进入版本控制候选集。

### A3 API 领域拆分

- 为现有 API 建立 golden contract tests；
- 从 `api/http_app.py` 分离公共 auth/error/pagination、Run/Evidence、Template/Market、Platform 和 Agent 路由；
- 保持 URL、状态码、错误 envelope、owner scope、ETag、request ID 和 SSE 行为兼容；
- 将 ASGI/OpenAPI adapter 与领域 handler 的职责固定下来；
- 在拆分完成前不向单体 handler 继续堆叠 3E 路由。

退出条件：旧契约测试零变化；跨 owner 负面测试通过；模块边界可独立测试；Docker API/Web smoke 通过。

### A4 前端自动化基线

- 为 Market → Studio → prepare/confirm/submit → Run → Evidence 建组件和集成测试；
- 覆盖 demo/fixed identity、403、404、stale、degraded、empty、loading 和 retry；
- 增加关键页面的键盘焦点、长文本、窄屏和错误边界检查；
- 建立稳定的 `pilot-browser` smoke，不使用脆弱的视觉坐标定位；
- 不把自动化结果当作主观 UI 反馈。

退出条件：核心金路径和主要错误状态都有自动回归；前端测试数量与核心页面风险相匹配。

## 4. B 轨：Phase 3E 状态、存储与规则闭环

### 3E-1 领域模型与迁移

- 定义 `RemediationSession` 状态机；
- 定义 `AgentTurn`、`ActionProposal`、`ActionDecision`、`ActionExecution` 和 `EvaluationResult`；
- 固定 source/derived Run、Contract、Diagnosis、Evidence digest 和 lineage；
- 定义 attempts、submissions、wall time、LLM calls/tokens budget；
- 实现 schema migration、旧数据兼容和回放测试。

退出条件：状态迁移非法路径被拒绝；迁移可重复执行；旧数据库可升级；owner scope 和序列化稳定。

### 3E-2 Store、API 与事件

- 实现 session/turn/proposal/decision/execution/evaluation store；
- 增加 owner-scoped list/detail、keyset pagination、ETag 和事件 read model；
- 增加创建、取消、输入、审批和拒绝的幂等 API；
- owner/body/query 不能覆盖已认证身份；
- 增加 OpenAPI/contract/permission/concurrency tests。

退出条件：跨 owner 全部拒绝；重复请求不产生重复 decision/action；事件可补读；API contract 稳定。

### 3E-3 规则优先的 session orchestration

- Diagnosis 完成后按 automation policy 幂等创建 session；
- 实现 waiting_evidence → diagnosing → planning → awaiting_*/ready 的确定性状态推进；
- 先使用规则候选，不调用 LLM；
- 实现 lease、CAS、超时、取消、崩溃恢复和 stop reason；
- Worker 重启后从持久状态继续，不依赖进程内 task。

退出条件：故障注入下不重复建 session、不重复执行 action；超预算和缺证据确定停止；Worker 重启可恢复。

## 5. C 轨：Phase 3E 受控动作与结果评价

### 3E-4 Policy 与 Action Executor

按风险从低到高实现：

1. `path_probe`、`runtime_probe` 等只读受限 probe；
2. `contract_patch` 和 `environment_select` 的 preview；
3. 明确审批后的 derived Contract；
4. 受 submissions budget 限制的 `retry_run`；
5. 高风险 `file_patch` 只做到受限范围、hash、diff、撤销和显式审批；
6. `dependency_plan` 只生成计划，不在登录节点或共享基础环境安装。

继续禁止任意 shell executor。所有 action 重新计算 policy、capability 和 preflight，不信任 proposal 自报风险。

退出条件：未批准写操作为零；allowed roots/大小/后缀/diff/resource budget 全部 fail closed；重复执行为零。

### 3E-5 Evaluator 与多轮闭环

- 依次评估提交确定性、terminal state、Evidence 完整性、expected outputs、success protocol 和 source/derived diff；
- 结果限定为 `verified_success`、`execution_success_unverified`、`failed`、`inconclusive`；
- verified failure 在预算内进入下一轮；不确定结果停止或请求人工输入；
- 保存前后 Contract、资源、日志、输出和规则结论差异；
- 增加成功、失败、取消、Evidence 缺失、输出错误和 evaluator 超时 live cases。

退出条件：Slurm success 不会自动等于业务成功；预算耗尽必定停止；每轮 lineage 和审计完整。

## 6. D 轨：Phase 3E LLM proposal

### 3E-6 Provider-neutral LLM adapter

- 拆分 `AgentNarrativeV2` 与 `RemediationPlanV1` schema；
- 实现 OpenAI-compatible adapter、timeout、retry、结构化解析和一次 repair；
- 只向模型发送经过 allowlist、截断和 secret scan 的 Evidence；
- 模型输出只进入 proposal，不直接进入 executor；
- provider 不可用时规则诊断、手工审批和 Run 管理保持可用。

退出条件：无 API key 时全套 fake-provider 测试可运行；真实 key 不写入 DB/Evidence/Capsule/log；模型不能扩大权限。

### 3E-7 版本化 benchmark

- 建立 partition/QoS、timeout、OOM、command/package/module/conda、path、CUDA/PyTorch、array/workflow 故障 corpus；
- 增加 prompt injection、伪造 Evidence、越权 action、429/5xx、invalid JSON、截断和重复响应；
- 自动计算 schema success、引用覆盖、policy escape、verified fix、误修和资源放大指标；
- 输出按模型、prompt、schema、规则版本绑定的报告；
- 没有真实 provider 凭据时只标记 fake/replay 验证等级，不阻塞其他工程。

退出条件：越权 action 通过 policy 为零；引用可追溯；指标报告可重复；不把 fake/replay 宣称为真实模型表现。

## 7. E 轨：Phase 3F Run/Agent 工作台

### 3F-1 Run 工作台补全

- 分页 Run 列表和保存筛选器；
- timeline、Slurm state、DAG、retry/agent lineage；
- stdout/stderr tail、Evidence tree、outputs、Capsule；
- raw/normalized 视图；
- cancel、retry、clone、compare 和安全 native command。

### 3F-2 Agent 工作台

- session queue：等待输入、审批、执行、接管和终态；
- facts/inferences 分区和 Evidence 点击定位；
- action 级 Contract/script/file diff；
- 风险、预算、预期效果、回退和 actor/request ID；
- prepare 与 execute 分离；
- source/derived Run、Evidence、outputs 和资源对比。

### 3F-3 终端协同的安全子集

- 提供 Job ID、workdir 和可复制的 `squeue/scontrol/tail/scancel` 等价命令；
- 支持配置化的平台终端 deep link；
- 不实现 xterm.js/WebSocket PTY；
- 不向浏览器下发长期 token。

### 3F-4 自动 UI 验证

- 组件、query/cache、权限和错误状态测试；
- `pilot-browser` 验证基本流、失败诊断、Agent 审批、拒绝、预算耗尽和模型降级；
- 键盘、焦点、窄屏、长路径、长中文和危险确认检查；
- 自动截图只作为回归证据，不代替人的视觉偏好。

退出条件：UI 与 API 事实一致；所有写操作对象级确认；基础作业不依赖终端；模型不可用路径完整。

## 8. F 轨：可本机完成的 Phase 3G

### 3G-1 PostgreSQL Repository

- 固定 Repository protocol；
- 实现 PostgreSQL migrations、事务、外键、锁和必要索引；
- 保留 SQLite 本机/离线模式；
- 建 SQLite/PostgreSQL contract parity suite；
- 验证迁移、回滚策略和旧数据导入。

### 3G-2 多实例一致性

- submit/reconcile/collection/agent execution 使用 lease、fencing token 和 outbox；
- 多 API、多 Worker 并发与进程崩溃测试；
- 验证零重复提交、零重复 action 和事件最终可达；
- 对 gateway/Slurm/DB 慢响应和网络中断实施退避与熔断。

### 3G-3 可观测性与恢复

- metrics 覆盖 API、queue、submit、reconcile、Evidence、Agent、LLM 和 SSE；
- trace 使用 request/run/job/session ID 关联；
- 结构化审计和 secret redaction；
- SQLite/PostgreSQL、Evidence 和 Capsule 备份恢复脚本；
- 自动重启、断点恢复和故障注入报告。

### 3G-4 本地安全基线

- CSP、CSRF、cookie、rate/body/response size 和 proxy trust contract；
- dependency/secret/image/config 扫描；
- header spoof、跨 owner、session fixation mock、过期凭据和撤销负面测试；
- 不把 mock OIDC 测试升级为校园生产身份结论。

退出条件：本地多实例和恢复目标通过；安全负面测试无 P0/P1；真实身份与平台准入仍明确保持外部未验证。

## 9. G 轨：本地发布候选与比赛金路径

### G1 CPU-only competition profile

- 建立符合 8C/16G 目标 VM 的 CPU-only profile；
- 移除 GPU partitions/QoS/templates 的可见能力；
- 将模拟节点 CPU/内存声明和容器限制对齐；
- 保留宿主机、DB、API/Worker 的资源余量；
- 在本机验证轻量并发和资源预检真实性。

### G2 可重复发布资产

- 固定镜像 digest、migration version、初始化数据和配置 schema；
- 生成离线镜像包、SHA256、SBOM/依赖清单、启动/停止/回滚脚本；
- 确保默认 secret 不能进入发布模式；
- 自动验证从空目录导入、启动、smoke、停止和恢复。

### G3 自动比赛金路径

在本机 Docker 环境自动验证：

1. CPU 模板采用和成功作业；
2. 高级 Contract 的 array/workflow；
3. 错误 QoS 在提交前阻断；
4. 规则/Agent proposal → 审批 → derived Run → output evaluation；
5. 模型不可用降级；
6. API/Worker/DB/gateway 重启恢复；
7. 成功、失败、取消、重试的 Evidence/Capsule 完整性。

所有证据标记为 D1 或本地 CPU profile，不冒充 VM、真实模型或真实 107。

### G4 最终自动 review

- 运行全量测试、静态检查、Docker smoke、浏览器回归、负载和故障套件；
- 生成能力矩阵、已知限制、发布 manifest 和 findings-first 报告；
- P0/P1 清零，P2/P3 有 owner、证据和处理决定；
- 产出可供未来 VM 部署的固定发布候选，但不执行上传。

## 10. 执行顺序

```text
A1 文档事实
→ A2 Git/CI
→ A3 API 拆分
→ A4 前端自动化基线
→ 3E-1/2/3 状态、API、规则闭环
→ 3E-4/5 动作与评价
→ 3E-6/7 LLM proposal 与 benchmark
→ 3F-1/2/3/4 工作台与自动 UI 回归
→ 3G-1/2/3/4 本地生产控制面
→ G1/2/3/4 CPU 发布候选与金路径
```

用户反馈可以在任意稳定 UI 节点并行发生，但不改变上述自动主线的依赖关系。收到反馈后产生的具体 UI 修复可插入当前切片，仍由同一自动质量门验证。

## 11. 自动执行停止条件

只有以下情况暂停并请求用户决定：

- 需要新增真实外部权限、付费资源、凭据或对外写操作；
- 两种产品行为都合理，但会实质改变用户流程或风险接受；
- 需要删除或不可逆迁移用户数据；
- 需要部署 VM、访问真实 107 或启用生产身份；
- 发现现有用户改动与计划任务不可安全合并。

普通实现困难、测试失败、Docker 故障、依赖问题、代码重构和本地数据迁移不构成用户阻塞；开发代理应先自动诊断、修复和复验。
