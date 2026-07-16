# 107Pilot 当前状态与 Phase 3 实施计划

日期：2026-07-15

> 状态说明：本文记录 Phase 2 结束时的计划基线。Phase 3A 已于 2026-07-15 完成；
> 当前实际状态和后续执行顺序以
> `docs/phase-3/current_actual_and_execution_plan.md` 为准。

## 1. 当前结论

Phase 2 已完成一个可验证的受控执行闭环：

```text
ContractV2
→ Recipe 版本锁定
→ 安全物化为 sbatch
→ Slurm dependency / bounded retry
→ Evidence + Diagnosis
→ Agent 建议
→ 用户审批
→ 派生 Contract
→ 幂等派生 Run
→ 真实 Slurm 执行
→ Run / Contract 谱系与审计事件
```

这意味着 Agent 的“批准后执行”能力已经闭环，但不意味着全部 Agent 设计目标已经完成。

尚未完成的关键能力：

- Worker 在 Evidence/Diagnosis 就绪后主动创建建议、通知用户并驱动审批队列；
- 多诊断、多动作、多轮修复的统一任务状态机与总预算；
- 对 conda、module、数据路径、代码补丁、容器等高风险动作的分级执行器；
- 真实 USTC `deepseek-v4-flash-ascend` 的质量、延迟、结构化输出与故障降级评测；
- Agent 建议、审批、执行、结果对比和回滚的完整 UI；
- 终端协同与高级 Contract/脚本编辑；
- 生产身份、RBAC、PostgreSQL/高可用、提交租约恢复和告警。

因此，当前 Agent 后端可视为“受控单动作闭环完成”，整体目标完成度仍不能判为 100%。

## 2. Phase 2 已实现

### 2.1 Contract 与 Recipe

- ContractV2 规范化、摘要、结构化校验和旧数据迁移；
- Recipe Registry 持久化、不可变版本、打包 YAML 导入；
- `generic_command` 与 `sbatch_template_v1` 物化器；
- runtime module、environment、conda 激活和模板严格变量检查；
- 容器能力未验证时拒绝伪映射，避免把镜像名错误转换为 Slurm OCI 参数；
- 派生 Contract 保留 parent/advice/action 来源和字段级来源。

### 2.2 Workflow 与 Run

- Run 间依赖解析为 Slurm `afterok:<job_id>`；
- 跨用户、失败、未提交和自依赖被拒绝；
- 有界自动重试、退避时间、确定性子 Run ID 和重试谱系；
- 多 worker 提交使用 SQLite CAS 抢占，避免同一 Run 重复 `sbatch`；
- API 将提交中并发冲突返回为 `409 submission_in_progress`。

Slurm 语义依据：

- `sbatch --dependency=afterok:<job_id>`：<https://slurm.schedmd.com/sbatch.html>
- array `%N` 并发上限：<https://slurm.schedmd.com/job_array.html>
- 原生 `--container` 依赖管理员配置的 OCI runtime：<https://slurm.schedmd.com/containers.html>

### 2.3 Agent 执行闭环

- 建议只基于已持久化 Diagnosis 与 Evidence 引用；
- 候选 Contract 必须重新通过确定性 preflight；
- 只有审批中明确选择的 action 可以执行；
- Advice/Action 唯一执行记录、并发 CAS 和陈旧执行回收；
- prepare 与 submit 可分离，重复调用返回同一派生 Contract/Run；
- source Run、derived Run、source Contract、derived Contract 形成可追溯谱系；
- HTTP API 已提供 action execute 入口和执行审计读模型。

### 2.4 验证基线

- `ruff`：通过；
- `mypy`：41 个源文件通过；
- 全量测试：354 项通过；
- Slurm 25.11 simulator OpenAPI 实测确认提交字段为 `dependency`；
- Docker Phase 2 smoke：模板物化、真实 `afterok`、失败、审批、Agent 修复、派生谱系全部通过。

## 3. 当前产品实际差距

### 3.1 Web 仍是演示控制台

当前 Web 是单页 vanilla JavaScript MVP，主要问题：

- Contract 固定使用 `recipe_python_cpu@1.0.0`，没有 Recipe 选择、详情和版本切换；
- 只显示 workdir、command 和少量资源字段；
- runtime、workflow、outputs、policy、extensions、array、module、conda 等高级字段不可编辑；
- 没有 Contract/Run 历史列表、搜索、过滤、收藏、复制和对比；
- 只展示 Agent Explain，没有 advice、审批、执行和修复结果界面；
- 没有工作流 DAG、重试谱系、依赖原因和作业时间线；
- 没有模板草稿、发布、分享、采用、评分或兼容性信息；
- 没有终端、命令复制、脚本高级编辑和本地文件工作流；
- UI 文案仍带 `Phase 0B Web MVP`、`Docker Slurm 演示控制台` 等开发标签；
- Playwright 主要依赖 mock API，尚缺完整 live API 视觉和交互回归。

### 3.2 Agent 仍偏“解释器 + 单补丁执行器”

- LLM 只负责 narrative/recommendations，不负责受约束的工具规划；
- policy engine 只允许少量 dotted field patch；
- `runtime.conda_env`、module、环境探针、数据同步、文件补丁等动作尚无专用执行器；
- 没有统一 remediation session、总尝试次数、成本/时间预算和停止条件；
- 没有修复前后 Evidence 差异、结果质量判定和自动回退；
- `bounded_auto` 当前只覆盖原样重试，不等同于自动修复。

### 3.3 生产设施仍缺基础设施门禁

- SQLite 适合单机比赛主线，但不适合作为多副本生产控制面的最终数据库；
- trusted header 当前依赖上游可信边界，尚未落地学校 SSO/OIDC 与角色模型；
- SUBMITTING CAS 可防并发重复提交，但进程崩溃后的租约恢复仍需完善；
- Web/API 仍缺 SSE/WebSocket 事件推送、操作审计查询、指标与告警；
- 模板市场缺签名、审核、撤回、可见范围和供应链扫描；
- 真实 107 平台的提交/取消/文件读取仍需受控探针逐项确认。

## 4. 不牺牲高级能力的交互原则

界面采用“同一 Contract，多种投影”，而不是用表单取代配置和终端：

1. **基础模式**：面向新生，只显示任务、环境、资源、输出和常见预检。
2. **高级模式**：完整编辑 ContractV2 的 runtime/workflow/policy/extensions 等字段。
3. **源码模式**：JSON/YAML 编辑器，保留未知 extension 和字段来源。
4. **脚本模式**：只读展示最终 sbatch，支持 diff、复制和下载，但提交内容来自同一 Contract digest。
5. **终端模式**：提供用户身份下的受控 PTY/SSH 会话；可复制等价命令，也可继续纯终端工作流。

五种模式必须共享同一个 canonical Contract，并做到：

- 表单修改能投影到源码；
- 源码修改经 schema 校验后回填表单；
- 未识别高级字段不丢失；
- 最终提交前固定显示 Contract digest、Recipe version 和 sbatch diff；
- UI 永不静默覆盖用户手工脚本或 extension。

## 5. Phase 3 分阶段计划

### Phase 3A：产品读模型与事件基础

目标：先让 UI 有稳定、可分页、可审计的数据面，而不是继续在单页中拼临时请求。

实施项：

- `GET /runs`：owner 强制绑定、分页、状态/时间/recipe/contract 过滤；
- `GET /contracts`：分页、recipe/version/digest/derived 过滤；
- `GET /runs/{id}/events`：稳定 cursor 和事件类型过滤；
- `GET /runs/{id}/lineage`：重试、Agent 修复和依赖边；
- Advice 列表、待审批列表和 execution 列表；
- 服务端统一错误 envelope、cursor、ETag 和 request id；
- SSE 只推送 run/advice/evidence 状态摘要，断线可通过 cursor 补读。

验收：

- 1 万 Run 数据下分页无全表扫描；
- owner/RBAC 负面测试覆盖所有列表；
- SSE 断连重连不丢关键终态；
- review 重点检查越权、分页稳定性和事件重复。

### Phase 3B：模板建立、分享市场与采用

数据模型：

- TemplateDraft：用户可编辑草稿；
- TemplateRelease：发布后不可变，绑定 Recipe version 与内容摘要；
- TemplateVisibility：private/course/campus/public；
- TemplateReview：审核、拒绝、撤回和原因；
- TemplateAdoption：采用时复制为用户 Contract 草稿，不修改原发布版本；
- TemplateCompatibility：平台、分区、QoS、GPU、数据集和运行时要求；
- TemplateMetrics：采用次数、成功率、最近验证时间，不以单纯点赞排序。

实施项：

- 草稿 CRUD、发布、撤回、复制、fork 和版本比较；
- 发布前 schema/materializer/preflight/secret scan；
- 课程教师和平台管理员审核流；
- 市场搜索、标签、兼容性过滤和可信等级；
- 采用后记录来源版本，后续更新只提示 diff，不自动覆盖。

验收：

- 发布版本不可变；
- 私有/课程模板权限隔离；
- 恶意模板、明文密钥、未验证容器均不能发布；
- 从市场采用到真实 Slurm 成功执行有 Docker smoke。

### Phase 3C：双轨 Contract Studio

目标：首先完成真正可用的作业创建工具，再进行视觉精修。

基础模式字段：

- 任务入口、workdir、预期输出；
- 环境选择：conda、module、环境变量、数据根目录；
- 资源：partition、QoS、CPU、GPU、memory、time、array；
- 工作流：依赖选择、重试次数、退避、审批策略；
- 策略：解释、建议、审批执行、有界自动化。

高级能力：

- JSON/YAML 编辑器、schema completion、字段级错误定位；
- 物化 sbatch 与上一版本 diff；
- Recipe 来源、版本、签名、兼容性和风险声明；
- Contract 导入/导出与 CLI 等价命令；
- user-scoped 终端入口，终端与 UI 共享 run/contract 引用而不是共享隐式状态。

验收：

- 基础→高级→源码往返不丢字段；
- 超出基础表单能力的 Contract 可完整保留和提交；
- 键盘、移动端、长路径和中文内容不溢出；
- Playwright 覆盖桌面/移动、错误/警告/加载/空状态和 live API。

### Phase 3D：Run、Evidence 与 Agent 工作台

实施项：

- Run 列表和详情双栏工作台；
- DAG、Slurm job、retry/agent lineage 和时间线；
- 日志 tail、结构化 Evidence、输出清单、Capsule 与校验状态；
- Diagnosis 的事实、证据引用、修复/预防/自动化建议；
- Agent advice diff、风险等级、审批 action 选择、prepare/submit 分离；
- 修复前后 Contract、脚本、资源、Evidence 和结果对比；
- cancel、retry、approve、reject、execute 均要求明确确认并显示审计主体。

验收：

- 用户可从失败 Run 在一个工作台完成诊断、审批、修复、复跑和结果验证；
- 自动修复不能绕过审批或 policy；
- 任何 UI 操作均可在事件时间线中追溯；
- 终端仍能完成同等高级操作。

### Phase 3E：真实 USTC 模型与 Agent 编排

目标模型：`deepseek-v4-flash-ascend`，通过 OpenAI-compatible 自有/校内网关调用。

保持的安全边界：

- LLM 不直接调用 Slurm、文件系统或数据库；
- LLM 只生成符合 schema 的 plan/advice；
- facts 必须来自 Evidence/Diagnosis；
- 每个 action 由确定性 policy、preflight 和 capability gate 再验证；
- 高风险动作必须审批；
- API key 只来自部署 secret，不进入 Contract、Evidence、日志或 prompt。

评测矩阵：

- 正常解释、缺字段、无效 JSON、超时、429、5xx、截断和重复响应；
- prompt injection、Evidence 中恶意指令、伪造引用和越权 action；
- timeout/OOM/QoS/partition/package/data path/shell/array 典型故障；
- 与 deterministic baseline 比较准确率、可执行率、误修率和平均修复轮数；
- provider 不可用时退化到规则解释和手工修复，不阻断作业管理。

验收：

- 真实网关 smoke 强制模式通过；
- 结构化响应成功率和故障降级达到预设门槛；
- 不存在 LLM 绕过 action policy 的路径；
- 不把模型 narrative 当作事实或执行授权。

### Phase 3F：生产与比赛阶段门

- PostgreSQL repository 与 SQLite 本地模式契约测试；
- 学校 SSO/OIDC、course role、admin role 和 service identity；
- 提交 lease、崩溃恢复、幂等对账和多 worker 压测；
- HTTPS、反向代理、CSRF/CSP、审计保留和 secret 管理；
- 真实 107 只读探针先行，再逐项开放 submit/cancel/file read；
- 模板供应链扫描、签名与撤回；
- 可观测性：队列、提交、对账、Evidence、Agent 延迟/错误率；
- 比赛演示脚本、离线降级、故障注入和恢复演练。

阶段门：

- 全量单元/API/迁移/安全测试；
- Docker 真实 Slurm 成功/失败/取消/依赖/重试/Agent 修复矩阵；
- 多 worker 不重复提交；
- live UI 视觉回归和无重叠检查；
- 真实模型评测报告；
- 每个阶段 findings-first review，阻断 finding 清零后才能进入下一阶段。

## 6. 推荐执行顺序

```text
Phase 3A 产品读模型
→ Phase 3B 模板市场后端
→ Phase 3C Contract Studio
→ Phase 3D Run/Agent 工作台
→ Phase 3E 真实模型与编排
→ Phase 3F 生产阶段门
```

比赛优先级遵循：

1. 先保证真实闭环和可证明的可靠性；
2. 再把关键流程做成新生可理解的工作台；
3. 同时保留源码、脚本、CLI 和终端入口；
4. 最后扩展市场规模、生产高可用和平台治理。

Phase 3 的第一项实施应是 3A，而不是直接重写前端。当前后端缺少列表、事件和谱系读模型，直接做视觉重构会再次产生 mock 驱动的孤立界面。
