# VM-local Slurm、Pi Agent 与科学计算演示闭环设计

- 日期：2026-08-25
- 状态：设计已由用户逐节确认，等待书面规格审核
- 适用环境：107Pilot CPU-RC 单 VM 演示环境
- 部署边界：VM-local Docker Slurm、`fixed_user=alice`、CPU-only
- 模型：`qwen3.8-reasoner`，provider 失败最多尝试 3 次
- 首个科学任务：C/OpenMP 二维热扩散有限差分实验

## 1. 目标

本设计同时关闭三个相互关联但职责独立的缺口：

1. 集群事实、资源预检、Agent 和真实作业运行必须读取同一个 VM-local Slurm 权威对象；
2. 用户必须能从 Agent 页面用自然语言创建一个具有科学正确性检查的真实计算工程，并经批准提交到 Slurm；
3. `pi-agent-core` 的工具选择、错误展示、步骤预算和终止条件必须形成可解释、可恢复、不会空白超时的产品合同。

本轮不把 Docker Slurm 表述为校园 107 集群，不完成多用户身份、校园 SSH/MFA 或生产证书验收。Slurm REST 的完整生产化认证与版本治理保留为后续独立工作。

## 2. 已验证的现状与根因

### 2.1 平台事实没有读取到 Slurm

2026-08-25 的实际 Agent Turn 读取到一份 `fresh`、`partial` 的 login-node 快照，但其中节点和分区均为 0。根因不是 Slurm 不可用，而是采集链路不一致：

- API 容器未配置 `PILOT107_SLURM_TOKEN`，REST `/partitions` 与 `/nodes` 返回 HTTP 401；
- 临时签发用户 JWT 后，代码固定使用的 `v0.0.41` 与 VM 中 Slurm 23.11.4 不匹配；`v0.0.40` 路径又因 slurmdb 连接失败返回错误；
- CLI collector 请求 `scontrol show part`、`scontrol show nodes` 和详细节点格式的 `sinfo`；
- Command Gateway 的 allowlist 不接受这些参数，也不接受采集器使用的 `conda` 与 `df`，审计日志返回 HTTP 400；
- collector 把传输拒绝统一折叠成 return code 125；
- `platform_get_snapshot` 按 owner 读取全 scope 最新记录，因 CLI 记录稍晚生成，最终选择了内容更差的空快照。

在同一 VM 上直接进入 Command Gateway/Slurm 容器执行 `scontrol` 和 `sinfo` 均成功，证明故障位于采集合同而不是 Slurm 控制器。

### 2.2 Agent Turn 空白超时

实际 Session `session-c60e21fb-c580-4c98-ad58-f9e3356e1b8e` 的只读 Turn 先取得空平台快照，随后模型依次猜测 `alice`、`home`、`default` 作为 workspace。所有 workspace 调用失败，Turn 最终以 `provider_timeout` 结束。

根因包括：

- `hpc-readonly-v1` 的真实 task kind 是 `interactive_readonly`，事件却兼容投影为 `interactive`；
- Session `source={}`，没有绑定 workspace，但 Agent 仍看到 workspace 工具；
- workspace tool 需要授权的绝对 Git workspace，模型没有合法 workspace ID 或路径来源；
- Python Tool Gateway 的结构化错误在 TypeScript 层被转成通用异常；事件归一化又优先展示空 `details={}`，因此 UI 只显示空对象；
- reasoner 在 tool call 前后产生的 `\n\n` 被作为独立 Agent 回复展示；
- `MAX_PROVIDER_CALLS=3` 仅参与外层 retry 判断，没有终止 Pi 内部 tool-call loop；实际 Turn 因此持续调用工具直至总超时。

### 2.3 Pi 实现状态

`pilot-agentd` 已真实嵌入精确锁定的 `@earendil-works/pi-agent-core 0.84.1`，并使用 `new Agent(...)` 运行短时 Turn。Session、Turn、checkpoint、typed tool、事件持久化和恢复均已接线。

设计中的“Pi 进程”不是每个用户的常驻 OS 进程。正式合同是：每个活动 Turn 在应用侧临时创建一个 Pi Agent 实例，Turn 或 durable AgentTask 边界结束后释放；数据库保存持久状态。当前缺口属于工具/终止/展示合同未闭环，而不是没有使用 Pi 内核。

## 3. 方案选择

### 3.1 备选方案

1. 仅修快照：能恢复 Dashboard 节点/分区，但不能验证 Agent 或科学工作流；拒绝。
2. VM-local Slurm CLI 权威源 + Pi Turn 硬化 + Agent 原生科学演示：覆盖本轮全部目标，采用。
3. REST-first：先修 JWT、OpenAPI 版本和 slurmdb 连接，更接近部分生产部署，但会扩大范围且不能直接解决 Agent 工具合同；延后。

### 3.2 核心决策

CPU-RC 演示以经 Command Gateway 精确 allowlist 的 VM-local Slurm CLI 为唯一权威事实源。REST collector 可以保留用于诊断，但在通过独立健康门禁前不得覆盖或冒充 `vm-slurm` 权威快照。

## 4. 权威 Slurm 事实架构

```text
VM-local Slurm
  ├─ scontrol：节点、分区、配置
  ├─ sinfo：容量与状态
  ├─ squeue：活动作业
  └─ sacct：终态与资源事实
          │
          ▼
严格 allowlist 的 Command Gateway
          │
          ▼
vm-slurm 权威事实快照
  ├─ Resource Dashboard
  ├─ Contract / Preflight
  ├─ Pi platform tools
  └─ Run / Evidence / Runtime Watch
```

### 4.1 命令合同

Command Gateway 仅增加 collector 已固定定义的精确 argv，不开放自由 shell：

- `scontrol show part`
- `scontrol show nodes`
- `sinfo -h -o %N|%P|%t|%c|%m|%G|%E`
- owner 固定的 `squeue -h -u <owner> -o %i|%T|%R|%P|%j`
- collector 固定格式的 `sacct` 终态查询
- `conda env list --json`；命令不存在时记录 127，不影响 Slurm 健康
- `df -P -h /public <authorized-owner-root>`；两个路径均重新做 root 授权

所有 argv、owner、cwd、超时和输出大小继续由服务端约束。模型不直接选择命令。

### 4.2 健康与选择语义

权威 snapshot 带稳定 connection ID `vm-slurm`。满足以下条件才是 healthy：

- `scontrol show part` 成功且至少解析出 1 个分区；
- `scontrol show nodes` 或详细 `sinfo` 成功且至少解析出 1 个节点；
- 节点 CPU、内存、分区和状态字段通过 schema；
- snapshot 未过 TTL。

`conda`、文件系统容量或 GPU runtime 缺失可以令对应字段 partial，但不能把已经成功的 Slurm 容量判为失败。反之，节点/分区为空时不得标记为可用的 fresh 平台事实。

新采集失败时保存 degraded collection attempt 供运维查看，但产品 latest 指针继续引用最后一份 healthy snapshot，并根据 TTL 转为 stale/expired。不存在 healthy snapshot 时，Dashboard、Preflight 和 Agent 必须明确显示 unavailable，不能返回“0 个节点”作为正常容量。

### 4.3 消费一致性

以下消费者必须使用相同的 `connection_id + snapshot_id + content_sha256`：

- Resource Dashboard；
- Contract resource preflight；
- `platform_get_snapshot`/后续 observation tools；
- Agent 资源建议；
- Run/AgentTask 提交前的平台验证。

运行期作业由 `squeue` 关联数字 Job ID；终态由 `sacct` 关联 state、ExitCode、allocation/step accounting。Run、Evidence 和 Capsule 保存这些来源引用，不能用应用数据库状态替代 Slurm 事实。

## 5. Pi Turn 产品合同

### 5.1 上下文相关工具集

工具集由服务端 session profile 和 source bindings 决定，模型不获得无上下文的工具：

- 普通平台问答且 `source={}`：平台事实工具；
- Project Session：绑定 `project_id` 与 `workspace_id` 的 Project/Workspace/Sandbox/Validation 工具；
- Run 场景：绑定 owner/run context 的 Run、日志、Evidence、resource tools；
- 未绑定 Project 时不暴露 `workspace_list/search/read`。

Project 工具参数使用不透明 ID，不要求模型猜绝对路径。所有 owner、workspace 和 resource authority 继续由 capability token 与 Tool Gateway 复核。

### 5.2 错误合同

工具失败同时向模型和 UI 返回安全结构：

```json
{
  "code": "WORKSPACE_NOT_BOUND",
  "message": "This Agent Session has no bound Project Workspace.",
  "retryable": false
}
```

错误不得包含 token、内部 URL、宿主路径或其他 owner 信息。事件归一化不得用空 `details` 覆盖实际错误 content。UI 将工具失败与普通结果分开展示。

### 5.3 预算与终止

预算分层：

- provider transport/empty-response 尝试：最多 3 次；
- read-only Pi steps：最多 4；
- Project Pi steps：最多 12；
- read-only tool invocations：最多 8；
- Project tool invocations：最多 32；
- 长验证：转换为 durable AgentTask 后立即释放当前 Turn。

步骤预算必须由 `shouldStopAfterTurn` 或同等 Pi loop hook 真实执行，不能只存在于 retry 分支。达到预算时生成明确 `turn_failed` 和可恢复 checkpoint。

非 constrained 交互 Turn 在工具完成后必须产生至少一个非空白的最终自然语言总结。纯空白、只有工具调用或只含失败工具而无总结均不能标记 completed。对已经产生公共事件的 Turn 不进行可能重复工具副作用的透明重试。

### 5.4 事件与 UI

- 事件保留真实内部 task kind；UI 将 `interactive_readonly` 显示为“平台只读 Turn”；
- 同一 Assistant message 的流式 delta 聚合为一条回复；
- 纯空白 delta 不生成独立回复卡；
- tool requested/started/completed、checkpoint、completed/failed 使用不同状态；
- timeout、budget exhausted、tool error 显示机器码和面向用户的解释；
- 工程模式展示 Project、Workspace、ChangeSet、Contract、Approval、AgentTask、Run 与 Capsule 的关联。

## 6. 二维热扩散科学演示

### 6.1 用户入口

验收从外部浏览器 Agent 页面输入自然语言目标开始，不调用 smoke 专用入口，不预置成功 Project：

> 创建一个二维热扩散有限差分实验，验证空间二阶收敛，并比较 1、2、4 线程性能。

Agent 必须从该目标生成 Project Blueprint、隔离 Workspace 和完整工程。

### 6.2 计算模型

求解二维扩散方程：

```text
u_t = alpha * (u_xx + u_yy)
u(x,y,0) = sin(pi*x) * sin(pi*y)
u = 0 on the boundary
u_exact = sin(pi*x) * sin(pi*y) * exp(-2*pi^2*alpha*t)
```

空间采用中心二阶差分，时间采用显式步进。对等距网格，程序在运行前验证稳定性条件；不满足时以非零退出并解释参数错误。由于时间步按网格平方缩放，空间和时间误差在 refinement study 中保持同阶。

### 6.3 工程结构

Agent 生成的工程至少包含：

```text
heat-diffusion/
├── src/heat2d.c
├── Makefile
├── scripts/run_experiment.sh
├── scripts/analyze.py
├── tests/test_small_case.py
├── experiment.json
└── README.md
```

约束：

- 求解器使用 C 与 OpenMP；
- 聚合和 SVG 生成仅使用 Python 标准库；
- 无网络下载与运行时包安装；
- 编译命令必须实际启用 OpenMP；
- README 记录方程、离散格式、稳定性、复现命令和结果口径。

### 6.4 验证与正式运行

Sandbox 只执行编译、小网格正确性、非法稳定参数和输出 schema 测试。它不充当正式性能结果。

Sandbox 通过后生成 ChangeSet 和 Contract，页面要求用户明确批准。批准前不得发布 workspace 或提交 Slurm。

正式 Slurm Job 分配 4 CPU，并在 allocation 中用受控 `srun -c 1`、`srun -c 2`、`srun -c 4` 形成可由 accounting 观察的 job steps：

- convergence：网格 64、128、256，记录 L2/L-infinity error；
- scaling：固定较大网格，分别使用 1、2、4 OpenMP threads；
- analyze：聚合数值正确性和性能结果。

具体迭代规模由 Blueprint 在 4 CPU/10 GiB envelope 内确定，但必须让单线程基线达到可测量时长，并保持整次演示在批准的 walltime 内。

### 6.5 输出与科学判定

正式输出：

- `raw-results.csv`
- `convergence.json`
- `scaling.json`
- `report.md`
- `convergence.svg`
- `scaling.svg`

科学门禁：

- 小网格结果与解析解比较通过；
- refinement 的观测收敛阶在 1.8 至 2.2；
- 不稳定参数被拒绝；
- 输出数字均为有限值且带单位/定义；
- 1/2/4 线程只报告实测 wall time 与 speedup，不强制性能必须单调或线性；
- 报告区分数值正确性、性能观测和环境局限。

### 6.6 Evidence 与 Capsule

Evidence 至少包含：

- Project Blueprint、ChangeSet、Contract 与批准记录；
- source tree/digest；
- 编译器版本和 OpenMP build 输出；
- Sandbox 测试结果；
- Slurm Job ID、job steps、状态、ExitCode 与 `sacct`；
- stdout/stderr；
- 原始 CSV、收敛判定、性能 JSON 和报告；
- 平台 snapshot ID/digest 与 ResourceEnvelope。

Capsule 保存完整可复现输入、代码、配置、结果、manifest 和 digest，不包含服务凭据。

## 7. 状态与批准流程

```text
自然语言目标
→ Project Blueprint
→ 隔离 Workspace
→ 生成 C/OpenMP 工程
→ Sandbox 小规模验证
→ ChangeSet + Contract
→ waiting_approval
→ 用户批准
→ 正式 Slurm Run
→ squeue/sacct + Evidence
→ Capsule
→ Agent 解释科学结果与性能
```

正式 Run 之前只能自动执行已批准 ResourceEnvelope 内的小型 Sandbox/validation。ChangeSet 发布、Contract 固化和正式 Slurm 提交使用一次明确用户批准。批准绑定精确 digest；批准后内容变化必须重新批准。

## 8. 测试策略

### 8.1 单元与合同测试

- Command Gateway 对每个 collector argv 的正向测试；相邻变体和自由命令继续拒绝；
- platform collector 区分 transport failure、command unavailable 与部分非关键字段；
- healthy latest 不被更新但 degraded 的空 snapshot 覆盖；
- Dashboard、Preflight 与 Agent 返回相同 snapshot ref；
- 无 Project source 时 workspace 工具不进入模型 tool schema；
- Tool Gateway 错误在模型 content 与公共事件中保持安全 code/message；
- whitespace delta 不创建公共回复；
- read-only 4 步、Project 12 步预算真正终止 Pi loop；
- 工具后无最终非空文本时 fail closed；
- checkpoint 恢复不重复已经完成的工具。

### 8.2 科学测试

- OpenMP 编译和小网格解析解误差；
- 稳定与不稳定参数边界；
- CSV/JSON/SVG schema；
- 64/128/256 refinement 的收敛阶；
- 线程数与 `OMP_NUM_THREADS`、`srun -c` 一致；
- 聚合器不伪造缺失样本或 speedup。

### 8.3 VM live 验收

在同一发布 revision 上执行：

1. 平台采集得到 `CPU-RC`、`anode16`、6 CPU、10240 MiB；
2. 启动可观测时长作业，`squeue` 在运行期看到同一数字 Job ID；
3. 终态 `sacct` 看到 Job/Step、COMPLETED 与 `0:0`；
4. 浏览器自然语言创建热扩散工程；
5. reasoner 完成 Sandbox，UI 显示 ChangeSet/Contract/批准；
6. 用户批准后正式运行；
7. 科学门禁、Evidence 和 Capsule 通过；
8. Agent 给出带事实引用的结果解释；
9. API/worker/agentd 不删卷重启后仍能读取全过程；
10. 外部 Dashboard 与 Agent 引用相同 Slurm snapshot。

任一关键步骤失败，整体验收为 FAIL。不得用预置数据库记录、fake model 或 smoke 专用直接写入替代外部链路。

## 9. 部署与回滚

实现按三个可独立回滚的提交边界交付：

1. VM-local Slurm authoritative facts；
2. Pi Turn/tool/UI contract hardening；
3. Agent-created heat diffusion demo and acceptance tooling。

每个边界都先通过本地测试，再构建不可变 CPU-RC bundle。VM 部署使用 systemd 切换 WorkingDirectory；旧 bundle 和持久卷保留，回滚只切换服务目录，不迁移或删除用户数据。

## 10. 完成定义

只有同时满足以下条件才称为本轮闭环：

- 平台事实确实来自 VM-local Slurm CLI，且节点/分区/活动作业/终态 accounting 可验证；
- Agent 不再出现空白回复卡、无上下文 workspace 猜测或无界工具 timeout；
- Pi 的真实 task kind、工具错误、预算和终态对用户可解释；
- 用户从外部 Agent 页面自然语言创建热扩散工程；
- Sandbox、ChangeSet、Contract、批准、Slurm、Evidence、Capsule 和结果解释全部贯通；
- 数值结果通过解析解和收敛阶门禁；
- 所有证据绑定同一代码 revision、平台 snapshot 和数字 Slurm Job；
- 文档明确说明该环境不是校园 107 或多用户生产。
